"""
dom_collector.py — Raw DOM (Depth of Market) data collector.

Subscribes to cTrader depth quotes and buffers minute-level top-5 bid/ask
snapshots for storage in the dom_raw table. Separate from dom_fetcher.py
which feeds the existing dom_snapshots table used by the 30-min trading cycle.
"""
import logging
import sys
import os
from datetime import datetime, timezone

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

# Tracks per-symbol order book state (incremental updates from cTrader)
_bids: dict[int, dict] = {}  # quote_id -> {price, volume}
_asks: dict[int, dict] = {}  # quote_id -> {price, volume}

# Latest top-5 snapshot ready to flush
_latest_snap: dict | None = None


def on_depth_quotes_received(raw_message) -> None:
    """Handle a ProtoOADepthEvent: update book, rebuild top-5 snapshot."""
    global _latest_snap
    from ctrader_open_api import Protobuf
    evt = Protobuf.extract(raw_message)

    for qid in evt.deletedQuotes:
        _bids.pop(qid, None)
        _asks.pop(qid, None)
    for q in evt.newQuotes:
        if q.bid:
            _bids[q.id] = {"price": q.bid / 100_000, "volume": q.size}
        elif q.ask:
            _asks[q.id] = {"price": q.ask / 100_000, "volume": q.size}

    sorted_bids = sorted(_bids.values(), key=lambda x: x["price"], reverse=True)[:5]
    sorted_asks = sorted(_asks.values(), key=lambda x: x["price"])[:5]

    best_bid = sorted_bids[0]["price"] if sorted_bids else None
    best_ask = sorted_asks[0]["price"] if sorted_asks else None
    spread_pips = (best_ask - best_bid) * 100 if (best_bid and best_ask) else None

    snap: dict = {
        "time": datetime.now(timezone.utc),
        "spread_pips": spread_pips,
    }
    for i, b in enumerate(sorted_bids, 1):
        snap[f"bid{i}_price"] = b["price"]
        snap[f"bid{i}_volume"] = b["volume"]
    for i, a in enumerate(sorted_asks, 1):
        snap[f"ask{i}_price"] = a["price"]
        snap[f"ask{i}_volume"] = a["volume"]

    _latest_snap = snap


def flush_dom_to_db(engine) -> None:
    """Write latest DOM snapshot to dom_raw and clear it."""
    global _latest_snap
    snap = _latest_snap
    if not snap:
        return
    _latest_snap = None

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO dom_raw (
                        time, symbol,
                        bid1_price, bid1_volume, bid2_price, bid2_volume,
                        bid3_price, bid3_volume, bid4_price, bid4_volume,
                        bid5_price, bid5_volume,
                        ask1_price, ask1_volume, ask2_price, ask2_volume,
                        ask3_price, ask3_volume, ask4_price, ask4_volume,
                        ask5_price, ask5_volume,
                        spread_pips
                    ) VALUES (
                        :time, :symbol,
                        :bid1_price, :bid1_volume, :bid2_price, :bid2_volume,
                        :bid3_price, :bid3_volume, :bid4_price, :bid4_volume,
                        :bid5_price, :bid5_volume,
                        :ask1_price, :ask1_volume, :ask2_price, :ask2_volume,
                        :ask3_price, :ask3_volume, :ask4_price, :ask4_volume,
                        :ask5_price, :ask5_volume,
                        :spread_pips
                    )
                    ON CONFLICT DO NOTHING
                """),
                {
                    "time": snap["time"],
                    "symbol": "USDJPY",
                    **{f"bid{i}_price": snap.get(f"bid{i}_price") for i in range(1, 6)},
                    **{f"bid{i}_volume": snap.get(f"bid{i}_volume") for i in range(1, 6)},
                    **{f"ask{i}_price": snap.get(f"ask{i}_price") for i in range(1, 6)},
                    **{f"ask{i}_volume": snap.get(f"ask{i}_volume") for i in range(1, 6)},
                    "spread_pips": snap.get("spread_pips"),
                },
            )
        log.debug("dom_collector.flush_done")
    except Exception as exc:
        log.error("dom_collector.flush_error", extra={"error": str(exc)})


async def subscribe_dom(session, symbol_id: int) -> None:
    """Send ProtoOASubscribeDepthQuotesReq and register the raw callback."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeDepthQuotesReq
    from twisted.internet import reactor
    import asyncio

    loop = asyncio.get_running_loop()
    done = loop.create_future()

    req = ProtoOASubscribeDepthQuotesReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
    req.symbolId.append(symbol_id)

    def _on_sub(resp):
        if not done.done():
            loop.call_soon_threadsafe(done.set_result, True)

    def _send():
        session.client.send(req).addCallback(_on_sub)

    reactor.callFromThread(_send)
    await asyncio.wait_for(done, timeout=15)
    session.register_message_callback(2155, on_depth_quotes_received)
    log.info(f"dom_collector.subscribed symbol_id={symbol_id}")


def get_latest_dom_snapshot(engine) -> dict | None:
    """Return the most recent row from dom_raw (for future signal use)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM dom_raw ORDER BY time DESC LIMIT 1")
            ).fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        log.error("dom_collector.get_latest_error", extra={"error": str(exc)})
        return None
