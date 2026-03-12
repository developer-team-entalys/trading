# TODO

- add also the DoM data from ctraders
- get VIX (Fear Index) data from FRED
- Nikkei daily data from returnYahoo Finance (free, no key)

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
| ML | DecisionTree (depth 6) | Combines features; target = ≥20 pip move in 4H |

A trade is placed only when composite confidence ≥ 0.65. Max risk: 1% per trade, €50 daily drawdown cap.

---

## Prerequisites

- Docker & Docker Compose v2
- API credentials (see below)
- ~2GB disk for TimescaleDB historical data

### Required Credentials

| Service | Where to get it |
|---------|----------------|
| cTrader Open API | [openapi.ctrader.com](https://openapi.ctrader.com) — create an app, get Client ID + Secret |
| IG Markets (optional, for sentiment) | [labs.ig.com](https://labs.ig.com) — register for API key |
| Telegram bot | Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy token |
| Telegram chat ID | Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID |

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

