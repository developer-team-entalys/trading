"""
tick_collector.py — Tick/spot event collector for USD/JPY.

Subscribes to cTrader ProtoOASpotEvent (msg_type 2131), accumulates per-minute
tick statistics, and flushes aggregates to the tick_volume_1m table.
"""
import logging
import sys
import os
from datetime import datetime, timezone

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

# Module-level accumulators — reset on each flush
_tick_count: int = 0
_bid_min: float | None = None
_bid_max: float | None = None
_ask_min: float | None = None
_ask_max: float | None = None
_bid_sum: float = 0.0
_ask_sum: float = 0.0


def on_quote_received(raw_message) -> None:
    """Handle a ProtoOASpotEvent: accumulate tick bid/ask stats."""
    global _tick_count, _bid_min, _bid_max, _ask_min, _ask_max, _bid_sum, _ask_sum
    from ctrader_open_api import Protobuf
    evt = Protobuf.extract(raw_message)

    bid = evt.bid / 100_000 if evt.bid else None
    ask = evt.ask / 100_000 if evt.ask else None

    _tick_count += 1
    if bid:
        _bid_min = min(_bid_min, bid) if _bid_min is not None else bid
        _bid_max = max(_bid_max, bid) if _bid_max is not None else bid
        _bid_sum += bid
    if ask:
        _ask_min = min(_ask_min, ask) if _ask_min is not None else ask
        _ask_max = max(_ask_max, ask) if _ask_max is not None else ask
        _ask_sum += ask


def flush_ticks_to_db(engine) -> None:
    """Snapshot accumulators, reset them, then write a row to tick_volume_1m."""
    global _tick_count, _bid_min, _bid_max, _ask_min, _ask_max, _bid_sum, _ask_sum

    count = _tick_count
    if count == 0:
        return

    bid_min, bid_max = _bid_min, _bid_max
    ask_min, ask_max = _ask_min, _ask_max
    bid_sum, ask_sum = _bid_sum, _ask_sum

    # Reset before any awaiting I/O so we don't lose new ticks
    _tick_count = 0
    _bid_min = _bid_max = _ask_min = _ask_max = None
    _bid_sum = _ask_sum = 0.0

    vwap_bid = bid_sum / count if count > 0 else None
    vwap_ask = ask_sum / count if count > 0 else None

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO tick_volume_1m
                        (time, symbol, tick_count, bid_min, bid_max,
                         ask_min, ask_max, vwap_bid, vwap_ask)
                    VALUES
                        (:time, :symbol, :tick_count, :bid_min, :bid_max,
                         :ask_min, :ask_max, :vwap_bid, :vwap_ask)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "time": datetime.now(timezone.utc).replace(second=0, microsecond=0),
                    "symbol": "USDJPY",
                    "tick_count": count,
                    "bid_min": bid_min,
                    "bid_max": bid_max,
                    "ask_min": ask_min,
                    "ask_max": ask_max,
                    "vwap_bid": vwap_bid,
                    "vwap_ask": vwap_ask,
                },
            )
        log.debug("tick_collector.flush_done", extra={"ticks": count})
    except Exception as exc:
        log.error("tick_collector.flush_error", extra={"error": str(exc)})


async def subscribe_ticks(session, symbol_id: int) -> None:
    """Send ProtoOASubscribeQuotesReq and register the tick callback."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq as ProtoOASubscribeQuotesReq
    from twisted.internet import reactor
    import asyncio

    loop = asyncio.get_running_loop()
    done = loop.create_future()

    req = ProtoOASubscribeQuotesReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
    req.symbolId.append(symbol_id)

    def _on_sub(resp):
        if not done.done():
            loop.call_soon_threadsafe(done.set_result, True)

    def _send():
        session.client.send(req).addCallback(_on_sub)

    reactor.callFromThread(_send)
    await asyncio.wait_for(done, timeout=15)
    session.register_message_callback(2131, on_quote_received)
    log.info(f"tick_collector.subscribed symbol_id={symbol_id}")
