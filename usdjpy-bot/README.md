# USD/JPY Algorithmic Trading Bot

An automated trading bot for USD/JPY that combines:
- **COT (Commitment of Traders)** — hedge fund positioning from CFTC weekly reports
- **Retail Sentiment** — contrarian signal from IG Markets / Myfxbook
- **Technical Analysis** — ATR, RSI, round-number proximity
- **Rollover Bias** — USD interest rates > JPY rates → long-side structural edge
- **Decision Tree ML** — learns which feature combinations lead to profitable trades

Execution via cTrader Open API (IC Markets EU demo/live), monitored via Grafana + Telegram.

---

## Strategy Overview

| Signal | Source | Logic |
|--------|--------|-------|
| COT | CFTC weekly ZIP | Net non-commercial long → bullish; short → bearish |
| Sentiment | IG Markets / Myfxbook | Contrarian: >65% retail long → short signal |
| Technical | cTrader candles | RSI zones, ATR sizing, round-number proximity |
| Rollover | Static | USD rates > JPY → long bias (+1) |
| VIX | FRED VIXCLS | Risk-off (>25) → JPY strength; calm (<15) → JPY weakness |
| Nikkei 225 | Yahoo Finance ^N225 | Up day → JPY weakness (risk-on); down day → JPY strength |
| ML | DecisionTree (depth 6) | Combines features; target = ≥20 pip move in 4H |

A trade is placed only when composite confidence ≥ 0.65. Max risk: 1% per trade, €50 daily drawdown cap.

---

## Prerequisites

- Docker & Docker Compose v2
- API credentials (see below)
- ~2GB disk for TimescaleDB historical data
- **ARM64 (Raspberry Pi 5) is supported natively** — no emulation needed; all images have `linux/arm64` variants

### Required Credentials

| Service | Where to get it |
|---------|----------------|
| cTrader Open API | [openapi.ctrader.com](https://openapi.ctrader.com) — create an app, get Client ID + Secret |
| IG Markets (optional, for sentiment) | [labs.ig.com](https://labs.ig.com) — register for API key |
| FRED API key (for VIX + interest rates) | [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html) — free, instant |
| Telegram bot | Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy token |
| Telegram chat ID | Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID |
| Nikkei 225 | No key needed — fetched via `yfinance` (Yahoo Finance `^N225`) |

---

## Quick Start

```bash
# 1. Clone and enter directory
cd usdjpy-bot

# 2. Configure environment
cp .env.example .env
# Edit .env — fill in CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, etc.

# 3. Build and start all services
docker compose up --build -d

# 4. Run connection tests
docker compose exec bot python src/test_connections.py

# 5. View logs
docker compose logs -f bot
```

Grafana dashboard: http://localhost:3000 (admin / value of GF_SECURITY_ADMIN_PASSWORD)

---

## Development Workflow

The `bot/src/` directory is volume-mounted into the container, so code changes take effect immediately without rebuilding:

```bash
# Edit any file in bot/src/
# Then restart just the bot service
docker compose restart bot

# Watch live logs
docker compose logs -f bot
```

---

## Production Deployment (VPS)

```bash
# On your VPS (London region recommended for low latency to IC Markets)
git clone <your-repo> usdjpy-bot
cd usdjpy-bot
cp .env.example .env && nano .env   # fill in production credentials

# Use production compose override (removes live-reload mount, adds resource limits)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Resource limits (production): bot=512MB RAM, DB=1GB RAM.

---

## Raspberry Pi 5 Deployment

The bot runs natively on Pi OS 64-bit (no QEMU emulation). Steps below assume a fresh Pi OS Bookworm (64-bit) install.

### One-time OS + Docker setup

```bash
# Install Docker (official script works on Pi OS 64-bit)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in after this
```

### Clone and configure

```bash
git clone <repo-url> usdjpy-bot
cd usdjpy-bot
cp .env.example .env
nano .env   # fill in credentials
```

### Start services (staggered — DB must be healthy before bot)

```bash
docker compose up -d timescaledb grafana
# Wait ~30 s for TimescaleDB to finish initialising
docker compose up -d bot
```

### Verify containers are running natively (not emulated)

```bash
docker inspect usdjpy-bot-timescaledb-1 | grep -i '"Architecture"'
# Expected: "Architecture": "arm64"
```

### Off-device backup (recommended — NVMe is reliable but not immune)

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure a remote (interactive — choose B2, GDrive, S3, etc.)
rclone config

# Enable in .env
BACKUP_REMOTE=b2:usdjpy-backups   # or gdrive:backups/usdjpy

# Test manually
./scripts/backup.sh
# Should print: "[backup] Remote copy OK"
```

See the [Backup & Restore](#backup--restore) section for the full schedule and restore procedure.

### Set up host cron (Pi-specific path)

```bash
cd ~/usdjpy-bot
PROJECT_DIR=$(pwd)
(crontab -l 2>/dev/null; echo "0 3 * * * $PROJECT_DIR/scripts/backup.sh >> $PROJECT_DIR/backups/backup.log 2>&1") | crontab -
crontab -l   # verify
```

---

## Upgrading an existing deployment

These steps apply when pulling a new version into an environment that is already running. The goal is zero data loss — existing tables and historical data are never touched.

### What changes in this version

| Added | Description |
|-------|-------------|
| `dom_raw` table | 1-minute top-5 bid/ask depth snapshots (raw microstructure) |
| `candles_5m` table | 5-minute OHLCV candles |
| `tick_volume_1m` table | 1-minute tick count, bid/ask min/max, VWAP |
| `dom_collector.py` | Raw DOM subscriber — separate from the existing `dom_snapshots` collector |
| `candle_5m_collector.py` | M5 backfill on startup, then every-5-min refresh |
| `tick_collector.py` | Spot event subscriber, per-minute flush |

The 30-minute trading cycle, all existing tables, and all stored data are untouched.

### Migration steps

**1. Back up the database first (strongly recommended)**

```bash
cd usdjpy-bot
./scripts/backup.sh
ls -lh backups/*.sql.gz   # confirm backup exists
```

**2. Pull the new code**

```bash
git pull origin main
```

**3. Rebuild and restart only the bot container**

The database container does not need to be touched — TimescaleDB keeps running and retains all data.

```bash
docker compose up --build -d bot
```

**4. Verify the new tables were created**

The bot calls `apply_schema()` on every startup, which runs `init.sql` with `CREATE TABLE IF NOT EXISTS` and `create_hypertable(..., if_not_exists => TRUE)`. The 3 new tables are created automatically without affecting existing ones.

```bash
docker compose exec timescaledb psql -U botuser -d usdjpy_bot -c "\dt"
# Should now show: dom_raw, candles_5m, tick_volume_1m alongside the existing tables
```

**5. Check startup logs for the new collectors**

```bash
docker compose logs bot --tail=40
```

Expected lines (in order):

```
startup.dom_subscribed                  ← existing dom_snapshots feed (unchanged)
candle_5m_collector.backfill_start      ← begins fetching 90 days of M5 bars
candle_5m_collector.backfill_complete   ← typically takes 30–120 s
startup.dom_raw_subscribed              ← raw DOM collector active
startup.ticks_subscribed                ← tick collector active
startup.collectors_initialized
```

If cTrader is not yet approved for live data, `startup.dom_raw_subscription_failed` and `startup.tick_subscription_failed` will appear instead — this is non-fatal; the 30-min cycle continues normally.

**6. Confirm data is flowing (after ~2 minutes)**

```bash
docker compose exec timescaledb psql -U botuser -d usdjpy_bot -c "
SELECT 'candles_5m'     AS t, COUNT(*) FROM candles_5m
UNION ALL SELECT 'dom_raw',        COUNT(*) FROM dom_raw
UNION ALL SELECT 'tick_volume_1m', COUNT(*) FROM tick_volume_1m;"
```

| Table | Expected |
|-------|----------|
| `candles_5m` | thousands of rows (90-day backfill) |
| `dom_raw` | grows by 1 row per minute |
| `tick_volume_1m` | grows by 1 row per minute |

### Rolling back

If something goes wrong, restore the backup and revert to the previous image:

```bash
git checkout HEAD~1 -- usdjpy-bot/bot/src usdjpy-bot/database
docker compose up --build -d bot
./scripts/restore.sh backups/<latest>.sql.gz
```

---

## Monitoring

- **Grafana**: http://localhost:3000 → "Trading" folder → "USD/JPY Trading Bot"
- **Telegram**: Alerts for every signal, trade, error, and weekly summary
- **Health check**: http://localhost:8080/health (returns `OK`)
- **Bot logs**: `docker compose logs -f bot`

### Inspect database contents

```bash
docker cp database/show_tables.sql usdjpy-bot-timescaledb-1:/tmp/show_tables.sql \
  && docker compose exec timescaledb psql -U botuser -d usdjpy_bot -f /tmp/show_tables.sql
```

Shows the latest 20 rows of every table (candles, cot_data, sentiment_data, signals, trades, model_performance) plus a row-count summary.

---

## Verifying Data Fetching

### Run the connection test suite

```bash
docker compose exec bot python src/test_connections.py
```

Expected output (all green):

```
[PASS] Database connection OK          — TimescaleDB reachable, all 12 tables present
[PASS] COT download OK                 — CFTC ZIP downloaded, JPY rows found
[PASS] Myfxbook sentiment OK           — retail long/short % fetched
[PASS] Telegram OK                     — alert message delivered
[PASS] VIX Data:                       — FRED VIXCLS downloaded (needs FRED_API_KEY)
         Latest VIX close: 18.42  (as of 2025-01-10)
         Regime:           NORMAL
[PASS] Nikkei 225:                     — Yahoo Finance ^N225 (no key needed)
         Latest close: 39,894  (as of 2025-01-10)
         Regime:       FLAT
[PASS] Rollover data OK
```

Any `[SKIP]` means the optional API key isn't set (VIX skips if `FRED_API_KEY` is blank).
Any `[FAIL]` needs investigation.

### Check seeding progress in bot logs

```bash
docker compose logs -f bot | grep -E 'seeding|seed_failed|rows'
```

Look for lines like:

```
startup.seeding_vix
startup.seeding_nikkei
startup.seeding_cot
rollover_fetcher.vix_saved rows=3800
rollover_fetcher.nikkei_saved rows=3800
```

Seeding runs at startup; may take 30–90 seconds for large tables.

### Inspect row counts in the database

```bash
docker cp database/show_tables.sql usdjpy-bot-timescaledb-1:/tmp/show_tables.sql \
  && docker compose exec timescaledb psql -U botuser -d usdjpy_bot -f /tmp/show_tables.sql
```

Healthy state (after seeding):

| Table | Expected rows |
|-------|--------------|
| `candles` | grows every 30 min |
| `candles_5m` | ~17 000 after 90-day backfill, grows every 5 min |
| `cot_data` | 200–400 (weekly since 2010) |
| `interest_rates` | 300–600 (monthly FRED series) |
| `vix_data` | ~3 800 (daily since 2010) |
| `nikkei_data` | ~3 800 (daily since 2010) |
| `rollover_data` | 1 row updated daily |
| `dom_snapshots` | grows every 30 min |
| `dom_raw` | grows every 1 min (0 if cTrader not approved) |
| `tick_volume_1m` | grows every 1 min (0 if cTrader not approved) |

---

## Backup & Restore

### Automatic schedule
- **Host cron** (primary): runs `scripts/backup.sh` every day at 03:00 UTC
- **APScheduler** (safety net): runs the same script at 03:15 UTC from inside the bot container

Backups land in `backups/usdjpy_YYYY-MM-DD_HH-MM.sql.gz`. Files older than 14 days are pruned automatically.

### Install host cron job (one-time setup)
```bash
cd /home/christel/repos/trading/usdjpy-bot
PROJECT_DIR=$(pwd)
(crontab -l 2>/dev/null; echo "0 3 * * * $PROJECT_DIR/scripts/backup.sh >> $PROJECT_DIR/backups/backup.log 2>&1") | crontab -
crontab -l   # verify
```

### Manual backup
```bash
./scripts/backup.sh
```

### List backups
```bash
ls -lh backups/*.sql.gz
```

### Restore
```bash
# Restore latest backup (prompts for confirmation)
./scripts/restore.sh

# Restore a specific file
./scripts/restore.sh backups/usdjpy_2025-01-15_03-00.sql.gz
```

### Backup logs
```bash
tail -f backups/backup.log        # host cron log
docker compose logs bot | grep backup  # APScheduler log
```

---

## Risk Disclaimer

This bot trades real money on a live account when configured with live credentials.
Past performance of any strategy does not guarantee future results.
Forex trading involves significant risk of loss.
The €50 daily drawdown cap and 1% per-trade risk limit are minimums — review them carefully before enabling live trading.
The authors accept no responsibility for financial losses.

