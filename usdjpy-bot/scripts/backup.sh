#!/usr/bin/env bash
# backup.sh — Daily pg_dump backup of the usdjpy-bot TimescaleDB database.
# Safe to run from cron: resolves paths relative to this script's location.
set -euo pipefail

# ── Resolve project root ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Load .env ─────────────────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        export "$line"
    done < <(grep -E '^(POSTGRES_|TELEGRAM_|BACKUP_REMOTE)' "$ENV_FILE")
fi

# ── Config ────────────────────────────────────────────────────────────────────
BACKUP_DIR="$PROJECT_DIR/backups"
TIMESTAMP="$(date -u '+%Y-%m-%d_%H-%M')"
BACKUP_FILE="$BACKUP_DIR/usdjpy_${TIMESTAMP}.sql.gz"
RETAIN_DAYS=14

POSTGRES_USER="${POSTGRES_USER:-botuser}"
POSTGRES_DB="${POSTGRES_DB:-usdjpy_bot}"

# ── Helpers ───────────────────────────────────────────────────────────────────
send_telegram() {
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=${msg}" \
            --max-time 10 > /dev/null 2>&1 || true
    fi
}

# ── Auto-detect container name ────────────────────────────────────────────────
CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'timescaledb' | head -1 || true)"
if [[ -z "$CONTAINER" ]]; then
    echo "[backup] ERROR: timescaledb container not found" >&2
    send_telegram "❌ USDJPY backup FAILED: timescaledb container not running (${TIMESTAMP})"
    exit 1
fi

# ── Run backup ────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

echo "[backup] Starting backup → $BACKUP_FILE (container: $CONTAINER)"
if docker exec "$CONTAINER" \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-password \
    | gzip > "$BACKUP_FILE"; then
    SIZE="$(du -sh "$BACKUP_FILE" | cut -f1)"
    echo "[backup] Success — $BACKUP_FILE ($SIZE)"
else
    echo "[backup] ERROR: pg_dump failed" >&2
    rm -f "$BACKUP_FILE"
    send_telegram "❌ USDJPY backup FAILED: pg_dump error (${TIMESTAMP})"
    exit 1
fi

# ── Off-device copy (optional) ────────────────────────────────────────────────
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
    if command -v rclone &>/dev/null; then
        echo "[backup] Copying to remote: $BACKUP_REMOTE"
        if rclone copy "$BACKUP_FILE" "$BACKUP_REMOTE" --contimeout 60s --timeout 300s; then
            echo "[backup] Remote copy OK"
            send_telegram "☁️ USDJPY off-device copy OK → ${BACKUP_REMOTE} (${BACKUP_FILE##*/})"
        else
            echo "[backup] WARNING: remote copy failed — local backup still intact" >&2
            send_telegram "⚠️ USDJPY off-device copy FAILED → ${BACKUP_REMOTE} (local backup still OK)"
        fi
    else
        echo "[backup] WARNING: BACKUP_REMOTE is set but rclone is not installed — skipping remote copy" >&2
    fi
fi

# ── Prune old backups ─────────────────────────────────────────────────────────
find "$BACKUP_DIR" -name "usdjpy_*.sql.gz" -mtime "+${RETAIN_DAYS}" -delete
echo "[backup] Pruned backups older than ${RETAIN_DAYS} days"

# ── Notify ────────────────────────────────────────────────────────────────────
send_telegram "✅ USDJPY backup OK — ${BACKUP_FILE##*/} (${SIZE})"
echo "[backup] Done"
