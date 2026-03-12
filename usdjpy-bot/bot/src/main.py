"""
main.py — Entry point and APScheduler orchestration for the USD/JPY trading bot.
"""
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import structlog
import sqlalchemy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ── Bootstrap path ───────────────────────────────────────────
sys.path.insert(0, "/app/src")

import config
from data.db_writer import get_engine, is_table_empty, apply_schema
from data.cot_fetcher import download_cot_history, fetch_latest_cot, save_cot_to_db
from data.rollover_fetcher import (
    download_interest_rate_history, save_interest_rates_to_db,
    fetch_swap_rates_from_ctrader, compute_rollover_signal,
    save_rollover_to_db, get_latest_rates, get_today_rollover,
)
from data.candle_fetcher import fetch_candles, compute_indicators, save_candles_to_db
from data.sentiment_fetcher import get_sentiment, save_sentiment_to_db, get_sentiment_count
from strategy.signal_engine import compute_signal
from strategy.decision_tree import retrain_if_needed, load_model
from execution.ctrader_client import get_session, disconnect
from execution.risk_manager import calculate_position_size, check_daily_drawdown, has_open_position
from execution.order_manager import place_market_order, check_signal_reversal
from monitoring.telegram_alert import (
    send_bot_started, send_bot_stopped, send_signal,
    send_drawdown_warning, send_error, send_weekly_summary, send_phase2_ready,
)
from monitoring.health_check import start_health_server, stop_health_server

# ── Structured logging ────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

scheduler: AsyncIOScheduler | None = None
_shutdown = False
_phase2_alerted: bool = False


# ─────────────────────────────────────────────────────────────
# Scheduled jobs
# ─────────────────────────────────────────────────────────────

async def cycle_job():
    """Run every SENTIMENT_INTERVAL_MIN minutes: fetch data → compute signal → execute."""
    global _phase2_alerted
    log.info("cycle_job.start")
    engine = get_engine()

    # 1. Fetch candles (best-effort — cTrader may be unavailable)
    try:
        session = await get_session()
        df = await fetch_candles(session, symbol=config.SYMBOL_NAME,
                                 timeframe=config.CANDLE_TIMEFRAME, count=100)
        if not df.empty:
            save_candles_to_db(df, engine)
    except Exception as exc:
        log.warning("cycle_job.candles_unavailable", error=str(exc))

    # 2. Fetch sentiment (always runs, independent of cTrader)
    try:
        sentiment = get_sentiment(config.MYFXBOOK_EMAIL, config.MYFXBOOK_PASSWORD)
        save_sentiment_to_db(sentiment, engine)
    except Exception as exc:
        log.warning("cycle_job.sentiment_unavailable", error=str(exc))

    # 3. Compute signal + execute (requires candle data)
    try:
        sig = compute_signal(engine)
        await send_signal(sig)

        # 4. Check Phase 2 readiness (alert once per process lifetime)
        if config.TRAINING_PHASE == 1 and not _phase2_alerted:
            count = get_sentiment_count(engine)
            if count >= config.MIN_SENTIMENT_ROWS:
                _phase2_alerted = True
                await send_phase2_ready(count)

        # 5. Execute if actionable
        if sig["action"] != "HOLD":
            ok = check_daily_drawdown(engine)
            if not ok:
                await send_drawdown_warning(0.0)
                return

            session = await get_session()
            atr_pips = sig["features"].get("atr_1h", 10.0)

            balance_eur = 10_000.0
            lots = calculate_position_size(balance_eur, atr_pips, sig["direction"])
            sl_pips = config.ATR_MULTIPLIER_SL * atr_pips
            tp_pips = config.ATR_MULTIPLIER_TP * atr_pips

            has_position = await has_open_position(session.client)
            if not has_position:
                await place_market_order(
                    session.client, engine,
                    direction=sig["direction"],
                    lots=lots,
                    stop_loss_pips=sl_pips,
                    take_profit_pips=tp_pips,
                    signal_confidence=sig["confidence"],
                )
            else:
                await check_signal_reversal(session.client, engine, sig)

    except Exception as exc:
        log.error("cycle_job.error", error=str(exc))
        await send_error("Cycle job failed", str(exc))


async def friday_cot_update():
    """Every Friday 22:00 UTC — download latest COT + retrain if needed."""
    log.info("friday_cot_update.start")
    engine = get_engine()
    try:
        df = download_cot_history(start_year=datetime.utcnow().year)
        save_cot_to_db(df, engine)
        retrained = retrain_if_needed(engine, config.TRAINING_PHASE)
        log.info("friday_cot_update.done", retrained=retrained)
    except Exception as exc:
        log.error("friday_cot_update.error", error=str(exc))
        await send_error("Friday COT update failed", str(exc))


async def sunday_weekly_refresh():
    """Every Sunday 00:00 UTC — full COT refresh, retrain, weekly summary."""
    log.info("sunday_weekly_refresh.start")
    engine = get_engine()
    try:
        df = download_cot_history(start_year=2010)
        save_cot_to_db(df, engine)
        retrain_if_needed(engine, config.TRAINING_PHASE)
        stats = _compute_weekly_stats(engine)
        await send_weekly_summary(stats)
    except Exception as exc:
        log.error("sunday_weekly_refresh.error", error=str(exc))
        await send_error("Sunday refresh failed", str(exc))


async def daily_rollover_refresh():
    """Every day 06:00 UTC — re-fetch swap rates from cTrader + update rollover_data."""
    log.info("daily_rollover_refresh.start")
    engine = get_engine()
    try:
        swap_data = {"swap_long_pts": 0.0, "swap_short_pts": 0.0}
        try:
            session = await get_session()
            from sqlalchemy import text as _text
            with engine.connect() as conn:
                sym_row = conn.execute(_text(
                    "SELECT 1"  # placeholder — symbol ID lookup via cTrader
                )).fetchone()
            swap_data = fetch_swap_rates_from_ctrader(session.client, 0)
        except Exception as exc:
            log.warning("daily_rollover_refresh.swap_unavailable", error=str(exc))

        rates = get_latest_rates(engine)
        rollover = compute_rollover_signal(
            swap_long_pts=swap_data["swap_long_pts"],
            swap_short_pts=swap_data["swap_short_pts"],
            fed_rate_pct=rates["fed_rate_pct"],
            boj_rate_pct=rates["boj_rate_pct"],
        )
        save_rollover_to_db(rollover, engine)
        log.info("daily_rollover_refresh.done",
                 rate_diff=rollover["rate_differential"],
                 carry_strength=rollover["carry_strength"])
    except Exception as exc:
        log.error("daily_rollover_refresh.error", error=str(exc))
        await send_error("Daily rollover refresh failed", str(exc))


async def monthly_rate_refresh():
    """1st of month 07:00 UTC — re-download FRED interest rate data."""
    log.info("monthly_rate_refresh.start")
    engine = get_engine()
    try:
        from sqlalchemy import text as _text
        # Get previous differential for comparison
        prev_rates = get_latest_rates(engine)
        prev_diff = prev_rates["rate_differential"]

        rate_df = download_interest_rate_history(config.FRED_API_KEY)
        if not rate_df.empty:
            save_interest_rates_to_db(rate_df, engine)
            new_rates = get_latest_rates(engine)
            new_diff = new_rates["rate_differential"]
            change = abs(new_diff - prev_diff)
            if change > 0.25:
                log.warning("monthly_rate_refresh.significant_change",
                            prev_diff=prev_diff, new_diff=new_diff, change=change)
            log.info("monthly_rate_refresh.done",
                     fed_rate=new_rates["fed_rate_pct"],
                     boj_rate=new_rates["boj_rate_pct"],
                     rate_diff=new_diff)
    except Exception as exc:
        log.error("monthly_rate_refresh.error", error=str(exc))
        await send_error("Monthly rate refresh failed", str(exc))


def run_daily_backup():
    """Safety-net backup job — runs backup.sh via subprocess. No-op if script absent."""
    script = "/app/scripts/backup.sh"
    if not os.path.exists(script):
        log.info("run_daily_backup.skipped", reason="script_not_found")
        return
    try:
        result = subprocess.run(
            [script],
            timeout=120,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("run_daily_backup.success", stdout=result.stdout.strip())
        else:
            log.error("run_daily_backup.failed",
                      returncode=result.returncode,
                      stderr=result.stderr.strip())
    except subprocess.TimeoutExpired:
        log.error("run_daily_backup.timeout")
    except Exception as exc:
        log.error("run_daily_backup.error", error=str(exc))


def _compute_weekly_stats(engine) -> dict:
    from sqlalchemy import text
    from datetime import date, timedelta
    week_ago = date.today() - timedelta(days=7)
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    COUNT(*) AS n_trades,
                    COALESCE(SUM(pnl_eur), 0) AS weekly_pnl,
                    COALESCE(MAX(pnl_eur), 0) AS best_trade,
                    COALESCE(MIN(pnl_eur), 0) AS worst_trade,
                    COALESCE(
                        SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0),
                        0
                    ) AS win_rate
                FROM trades
                WHERE closed_at::date >= :week_ago
                  AND pnl_eur IS NOT NULL
            """),
            {"week_ago": week_ago},
        ).fetchone()
        model_row = conn.execute(
            text("SELECT accuracy FROM model_performance ORDER BY trained_at DESC LIMIT 1")
        ).fetchone()

    return {
        "n_trades": int(row[0]) if row else 0,
        "weekly_pnl": float(row[1]) if row else 0.0,
        "best_trade": float(row[2]) if row else 0.0,
        "worst_trade": float(row[3]) if row else 0.0,
        "win_rate": float(row[4]) if row else 0.0,
        "model_accuracy": float(model_row[0]) if model_row else 0.0,
    }


# ─────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────

async def startup():
    """Validate config, seed DB, train initial model, start scheduler."""
    global scheduler

    log.info("startup.begin")

    # 1. Validate config (non-fatal for optional credentials)
    try:
        config.validate_required()
    except EnvironmentError as exc:
        log.warning("startup.config_warning", warning=str(exc))

    # 2. Wait for DB to be ready (retry 5x with 10s delay)
    engine = None
    for attempt in range(1, 6):
        try:
            engine = get_engine()
            config.validate_db()
            log.info("startup.db_connected")
            apply_schema(engine)
            log.info("startup.schema_applied")
            break
        except Exception as exc:
            log.warning(f"startup.db_not_ready", attempt=attempt, error=str(exc))
            if attempt == 5:
                raise RuntimeError("Database not available after 5 attempts") from exc
            await asyncio.sleep(10)

    # 3. Connect to cTrader (best-effort; bot can still collect data without it)
    try:
        await get_session()
        log.info("startup.ctrader_connected")
    except Exception as exc:
        log.warning("startup.ctrader_failed", error=str(exc))

    # 3b. Initialize rollover & interest rate data
    _startup_rollover = None
    try:
        from sqlalchemy import text as _text
        with engine.connect() as conn:
            rate_count = conn.execute(_text("SELECT COUNT(*) FROM interest_rates")).scalar()

        if rate_count == 0:
            log.info("startup.downloading_rate_history")
            rate_df = download_interest_rate_history(config.FRED_API_KEY)
            if not rate_df.empty:
                save_interest_rates_to_db(rate_df, engine)

        # Fetch live swap rates (will be 0.0 if cTrader not yet approved)
        swap_data = {"swap_long_pts": 0.0, "swap_short_pts": 0.0}
        try:
            _session = await get_session()
            swap_data = fetch_swap_rates_from_ctrader(_session.client, 0)
        except Exception as exc:
            log.warning("startup.swap_rates_unavailable", error=str(exc))

        rates = get_latest_rates(engine)
        _startup_rollover = compute_rollover_signal(
            swap_long_pts=swap_data["swap_long_pts"],
            swap_short_pts=swap_data["swap_short_pts"],
            fed_rate_pct=rates["fed_rate_pct"],
            boj_rate_pct=rates["boj_rate_pct"],
        )
        save_rollover_to_db(_startup_rollover, engine)
        log.info("startup.rollover_initialized",
                 rate_diff=_startup_rollover["rate_differential"],
                 carry_strength=_startup_rollover["carry_strength"])
    except Exception as exc:
        log.warning("startup.rollover_init_failed", error=str(exc))

    # 4. Seed COT history if DB is empty
    if is_table_empty("cot_data"):
        log.info("startup.seeding_cot")
        try:
            df = download_cot_history(start_year=2010)
            save_cot_to_db(df, engine)
        except Exception as exc:
            log.error("startup.cot_seed_failed", error=str(exc))

    # 5. Seed candle history if DB is empty (last 2 years of M30 = ~35,040 bars)
    if is_table_empty("candles"):
        log.info("startup.seeding_candles", timeframe=config.CANDLE_TIMEFRAME)
        try:
            session = await get_session()
            df = await fetch_candles(
                session, symbol=config.SYMBOL_NAME,
                timeframe=config.CANDLE_TIMEFRAME, count=35040
            )
            if not df.empty:
                save_candles_to_db(df, engine)
        except Exception as exc:
            log.error("startup.candle_seed_failed", error=str(exc))

    # 6. Train initial model if none exists
    if load_model() is None:
        log.info("startup.initial_training", phase=config.TRAINING_PHASE)
        try:
            retrain_if_needed(engine, config.TRAINING_PHASE)
        except Exception as exc:
            log.warning("startup.initial_training_failed", error=str(exc))

    # 7. Start scheduler — cycle fires every SENTIMENT_INTERVAL_MIN minutes
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_job,
        CronTrigger(minute="0,30") if config.SENTIMENT_INTERVAL_MIN == 30
        else IntervalTrigger(minutes=config.SENTIMENT_INTERVAL_MIN),
        id="cycle",
        max_instances=1,
    )
    scheduler.add_job(friday_cot_update, CronTrigger(day_of_week="fri", hour=22), id="friday_cot")
    scheduler.add_job(sunday_weekly_refresh, CronTrigger(day_of_week="sun", hour=0), id="sunday_refresh")
    scheduler.add_job(daily_rollover_refresh, CronTrigger(hour=6, minute=0), id="daily_rollover")
    scheduler.add_job(monthly_rate_refresh, CronTrigger(day=1, hour=7, minute=0), id="monthly_rates")
    scheduler.add_job(
        run_daily_backup,
        CronTrigger(hour=3, minute=15, timezone="UTC"),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.start()

    next_run = datetime.now(timezone.utc) + timedelta(minutes=config.SENTIMENT_INTERVAL_MIN)
    await send_bot_started(next_run, rollover=_startup_rollover)
    log.info("startup.complete", next_signal=next_run.isoformat(),
             phase=config.TRAINING_PHASE, timeframe=config.CANDLE_TIMEFRAME)

    # 8. Start health check HTTP server
    start_health_server()


# ─────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────

async def shutdown():
    global _shutdown
    if _shutdown:
        return
    _shutdown = True

    log.info("shutdown.begin")

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    await disconnect()
    stop_health_server()
    await send_bot_stopped()

    log.info("shutdown.complete")


def _handle_signal(sig, frame):
    log.info("shutdown.signal_received", sig=sig)
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())
    loop.stop()


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

async def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    await startup()

    # Keep the event loop alive
    try:
        while not _shutdown:
            await asyncio.sleep(1)
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
