"""
candle_5m_collector.py — 5-minute candle collector.

Backfills 90 days of M5 OHLCV history from cTrader on startup (if the table
is empty) and fetches the latest closed bar every 5 minutes. Stores in the
candles_5m table. Does not affect the 30-min trading cycle.
"""
import logging
import sys
import os
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log = logging.getLogger(__name__)

BACKFILL_DAYS = 90
BATCH_DAYS = 17
MS_PER_DAY = 86_400_000
BAR_MS = 300_000  # 5 minutes in milliseconds


async def backfill_5m_candles(session, symbol_id: int, engine) -> None:
    """Fetch 90 days of M5 candles into candles_5m if the table is empty."""
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM candles_5m")).scalar()
    if count > 0:
        log.info("candle_5m_collector.backfill_skipped", extra={"existing_rows": count})
        return

    log.info("candle_5m_collector.backfill_start", extra={"days": BACKFILL_DAYS})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - BACKFILL_DAYS * MS_PER_DAY

    total_saved = 0
    cursor_ms = start_ms
    while cursor_ms < now_ms:
        batch_end_ms = min(cursor_ms + BATCH_DAYS * MS_PER_DAY, now_ms)
        try:
            bars = await session.fetch_trendbars_range(symbol_id, cursor_ms, batch_end_ms, "M5")
            if bars:
                total_saved += _save_candles(bars, engine)
        except Exception as exc:
            log.warning("candle_5m_collector.batch_error", extra={"error": str(exc)})
        cursor_ms = batch_end_ms

    log.info("candle_5m_collector.backfill_complete", extra={"saved": total_saved})


async def fetch_latest_5m_candle(session, symbol_id: int, engine) -> None:
    """Fetch the last 2 M5 bars and save any that are fully closed."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    from_ms = now_ms - 2 * BAR_MS
    try:
        bars = await session.fetch_trendbars_range(symbol_id, from_ms, now_ms, "M5")
        cutoff = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
        closed = [b for b in bars if b["time"] <= cutoff]
        if closed:
            _save_candles(closed, engine)
    except Exception as exc:
        log.error("candle_5m_collector.fetch_latest_error", extra={"error": str(exc)})


def _save_candles(bars: list[dict], engine) -> int:
    """Write candle dicts to candles_5m. Returns number of rows inserted."""
    if not bars:
        return 0
    rows_saved = 0
    with engine.begin() as conn:
        for bar in bars:
            result = conn.execute(
                text("""
                    INSERT INTO candles_5m (time, open, high, low, close, volume)
                    VALUES (:time, :open, :high, :low, :close, :volume)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "time": bar["time"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar.get("volume", 0),
                },
            )
            rows_saved += result.rowcount
    return rows_saved
