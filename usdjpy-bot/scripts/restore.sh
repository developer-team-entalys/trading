#!/usr/bin/env bash
# restore.sh — Restore usdjpy-bot database from a backup file.
# Usage: ./scripts/restore.sh [path/to/backup.sql.gz]
# If no argument given, restores the most recent backup automatically.
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
    done < <(grep -E '^(POSTGRES_|TELEGRAM_)' "$ENV_FILE")
fi

POSTGRES_USER="${POSTGRES_USER:-botuser}"
POSTGRES_DB="${POSTGRES_DB:-usdjpy_bot}"
BACKUP_DIR="$PROJECT_DIR/backups"

# ── Pick backup file ──────────────────────────────────────────────────────────
if [[ $# -ge 1 ]]; then
    BACKUP_FILE="$1"
else
    BACKUP_FILE="$(ls -t "$BACKUP_DIR"/usdjpy_*.sql.gz 2>/dev/null | head -1 || true)"
    if [[ -z "$BACKUP_FILE" ]]; then
        echo "[restore] ERROR: no backup files found in $BACKUP_DIR" >&2
        exit 1
    fi
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "[restore] ERROR: backup file not found: $BACKUP_FILE" >&2
    exit 1
fi

echo "[restore] Backup file : $BACKUP_FILE"
echo "[restore] Database    : $POSTGRES_DB @ $POSTGRES_USER"
echo ""
echo "WARNING: This will DROP and recreate all tables in $POSTGRES_DB."
read -rp "Type 'yes' to continue: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "[restore] Aborted."
    exit 0
fi

# ── Auto-detect containers ────────────────────────────────────────────────────
DB_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'timescaledb' | head -1 || true)"
BOT_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'bot' | grep -v timescaledb | head -1 || true)"

if [[ -z "$DB_CONTAINER" ]]; then
    echo "[restore] ERROR: timescaledb container not running" >&2
    exit 1
fi

# ── Stop bot ──────────────────────────────────────────────────────────────────
if [[ -n "$BOT_CONTAINER" ]]; then
    echo "[restore] Stopping bot container: $BOT_CONTAINER"
    docker stop "$BOT_CONTAINER"
fi

# ── Restore ───────────────────────────────────────────────────────────────────
echo "[restore] Restoring $BACKUP_FILE → $DB_CONTAINER ($POSTGRES_DB)..."
gunzip -c "$BACKUP_FILE" \
    | docker exec -i "$DB_CONTAINER" \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-password

echo "[restore] Restore complete."

# ── Restart bot ───────────────────────────────────────────────────────────────
if [[ -n "$BOT_CONTAINER" ]]; then
    echo "[restore] Restarting bot container: $BOT_CONTAINER"
    docker start "$BOT_CONTAINER"
fi

echo "[restore] Done."
