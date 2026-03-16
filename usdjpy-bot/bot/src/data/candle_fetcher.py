"""
candle_fetcher.py — Fetch USD/JPY OHLCV candles via cTrader Open API.

The cTrader Open API uses protobuf over TCP (not REST).
Library: ctrader-open-api (official Spotware Python client)
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
from sqlalchemy import text
from twisted.internet import reactor, ssl
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOAGetTrendbarsReq,
    ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

# ── Twisted reactor daemon thread ─────────────────────────────
# ctrader-open-api uses Twisted which has its own event loop. We run the
# Twisted reactor in a background daemon thread and bridge to asyncio via
# reactor.callFromThread() (Twisted→asyncio direction) and
# loop.call_soon_threadsafe() (Twisted→asyncio Future resolution).
_reactor_thread: threading.Thread | None = None


def _ensure_reactor_running() -> None:
    """Start the Twisted reactor in a daemon thread if not already running."""
    global _reactor_thread
    if reactor.running:
        return
    if _reactor_thread is not None and _reactor_thread.is_alive():
        deadline = time.monotonic() + 2.0
        while not reactor.running and time.monotonic() < deadline:
            time.sleep(0.05)
        return
    _reactor_thread = threading.Thread(
        target=lambda: reactor.run(installSignalHandlers=False),
        daemon=True,
        name="twisted-reactor",
    )
    _reactor_thread.start()
    deadline = time.monotonic() + 2.0
    while not reactor.running and time.monotonic() < deadline:
        time.sleep(0.05)


# cTrader timeframe mapping
TIMEFRAME_MAP = {
    "M1":  ProtoOATrendbarPeriod.M1,
    "M5":  ProtoOATrendbarPeriod.M5,
    "M15": ProtoOATrendbarPeriod.M15,
    "M30": ProtoOATrendbarPeriod.M30,
    "H1":  ProtoOATrendbarPeriod.H1,
    "H4":  ProtoOATrendbarPeriod.H4,
    "D1":  ProtoOATrendbarPeriod.D1,
}

# Duration of one bar in milliseconds, per timeframe
TIMEFRAME_MS = {
    "M1":       60_000,
    "M5":      300_000,
    "M15":     900_000,
    "M30":   1_800_000,
    "H1":    3_600_000,
    "H4":   14_400_000,
    "D1":   86_400_000,
}

# Pip value for USD/JPY: 1 pip = 0.01 JPY (3rd decimal)
USDJPY_PIP = 0.01


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range in pips."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return (tr.ewm(alpha=1 / period, min_periods=period).mean() / USDJPY_PIP)


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _session_label(hour_utc: int) -> str:
    if 0 <= hour_utc < 8 or hour_utc >= 22:
        return "tokyo"
    elif 8 <= hour_utc < 12:
        return "london"
    elif 12 <= hour_utc < 17:
        return "new_york"
    else:
        return "overlap"


def _distance_to_round(price: float) -> float:
    """Pips to nearest 0.50 round number (e.g. 149.00, 149.50, 150.00)."""
    nearest = round(price * 2) / 2  # nearest 0.50
    return abs(price - nearest) / USDJPY_PIP


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ATR (1H, 4H), RSI-14, session, distance-to-round, and near_round."""
    df = df.copy().sort_values("time").reset_index(drop=True)

    # ATR on native timeframe data
    df["atr_1h"] = _compute_atr(df, period=14)

    # ATR on H4 (resample from native timeframe)
    df_h4 = (
        df.set_index("time")
        .resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )
    df_h4["atr_4h"] = _compute_atr(df_h4, period=14)
    df_h4 = df_h4[["time", "atr_4h"]]

    # Merge 4H ATR back (forward-fill)
    df = df.merge(df_h4, on="time", how="left")
    df["atr_4h"] = df["atr_4h"].ffill()

    # RSI
    df["rsi_14"] = _compute_rsi(df["close"], period=14)

    # Session label
    df["session"] = df["time"].dt.hour.map(_session_label)

    # Distance to round number
    df["distance_to_round"] = df["close"].apply(_distance_to_round)

    # Near round number flag
    df["near_round"] = df["distance_to_round"] <= 20

    return df


def save_candles_to_db(df: pd.DataFrame, engine) -> int:
    """Upsert candles into TimescaleDB. Returns rows affected."""
    rows_affected = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            def _f(col):
                v = row.get(col)
                return v if v is not None and pd.notna(v) else None

            near_round_val = bool(row["near_round"]) if pd.notna(row.get("near_round")) else None

            result = conn.execute(
                text("""
                    INSERT INTO candles (
                        time, open, high, low, close, volume,
                        atr_1h, atr_4h, rsi_14, session, distance_to_round, near_round
                    ) VALUES (
                        :time, :open, :high, :low, :close, :volume,
                        :atr_1h, :atr_4h, :rsi_14, :session, :distance_to_round, :near_round
                    )
                    ON CONFLICT DO NOTHING
                """),
                {
                    "time": row["time"],
                    "open": _f("open"),
                    "high": _f("high"),
                    "low": _f("low"),
                    "close": _f("close"),
                    "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
                    "atr_1h": _f("atr_1h"),
                    "atr_4h": _f("atr_4h"),
                    "rsi_14": _f("rsi_14"),
                    "session": row.get("session"),
                    "distance_to_round": _f("distance_to_round"),
                    "near_round": near_round_val,
                },
            )
            rows_affected += result.rowcount
    log.info(f"Upserted {rows_affected} candle rows into DB")
    return rows_affected


# ─────────────────────────────────────────────────────────────
# Async cTrader connection helpers
# ─────────────────────────────────────────────────────────────

class CTraderSession:
    """Manages a cTrader Open API async session."""

    def __init__(self):
        self.client = None
        self._symbol_id = None
        self._dom_bids: dict[int, dict] = {}  # id -> {price, volume}
        self._dom_asks: dict[int, dict] = {}  # id -> {price, volume}
        self._message_callbacks: dict[int, list] = {}

    async def connect(self) -> "CTraderSession":
        _ensure_reactor_running()

        loop = asyncio.get_running_loop()
        self.client = Client(config.CTRADER_HOST, config.CTRADER_PORT, TcpProtocol)
        authed = loop.create_future()

        def on_connected(client):
            req = ProtoOAApplicationAuthReq()
            req.clientId = config.CTRADER_CLIENT_ID
            req.clientSecret = config.CTRADER_CLIENT_SECRET
            client.send(req)

        def on_message(client, message):
            msg_type = message.payloadType
            if msg_type == 2101:  # ProtoOAApplicationAuthRes
                req = ProtoOAAccountAuthReq()
                req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
                req.accessToken = config.CTRADER_ACCESS_TOKEN
                client.send(req)
            elif msg_type == 2103:  # ProtoOAAccountAuthRes
                if not authed.done():
                    loop.call_soon_threadsafe(authed.set_result, True)
            elif msg_type == 2142:  # ProtoOAErrorRes
                from ctrader_open_api import Protobuf
                err = Protobuf.extract(message)
                err_msg = f"{err.errorCode}: {err.description}"
                if not authed.done():
                    loop.call_soon_threadsafe(
                        authed.set_exception, ConnectionError(err_msg)
                    )
            elif msg_type == 2155:  # ProtoOADepthEvent
                from ctrader_open_api import Protobuf
                evt = Protobuf.extract(message)
                for qid in evt.deletedQuotes:
                    self._dom_bids.pop(qid, None)
                    self._dom_asks.pop(qid, None)
                for q in evt.newQuotes:
                    if q.bid:
                        self._dom_bids[q.id] = {"price": q.bid / 100_000, "volume": q.size}
                    elif q.ask:
                        self._dom_asks[q.id] = {"price": q.ask / 100_000, "volume": q.size}

            for cb in self._message_callbacks.get(msg_type, []):
                try:
                    cb(message)
                except Exception as e:
                    log.error(f"message_callback_error msg_type={msg_type} error={e}")

        def on_disconnected(client, reason):
            log.warning(f"cTrader disconnected: {reason}")
            if not authed.done():
                loop.call_soon_threadsafe(
                    authed.set_exception, ConnectionError(str(reason))
                )

        self.client.setConnectedCallback(on_connected)
        self.client.setMessageReceivedCallback(on_message)
        self.client.setDisconnectedCallback(on_disconnected)

        reactor.callFromThread(self.client.startService)
        await asyncio.wait_for(authed, timeout=30)
        log.info("cTrader authenticated successfully")
        return self

    async def get_symbol_id(self, symbol: str = "USDJPY") -> int:
        if self._symbol_id:
            return self._symbol_id

        loop = asyncio.get_running_loop()
        result_future = loop.create_future()

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.includeArchivedSymbols = False

        def on_symbols(resp):
            from ctrader_open_api import Protobuf
            resp = Protobuf.extract(resp)
            if not hasattr(resp, 'symbol'):
                if not result_future.done():
                    loop.call_soon_threadsafe(
                        result_future.set_exception,
                        ConnectionError(f"cTrader error response: {type(resp).__name__}")
                    )
                return
            for sym in resp.symbol:
                if sym.symbolName == symbol:
                    if not result_future.done():
                        loop.call_soon_threadsafe(result_future.set_result, sym.symbolId)
                    return
            if not result_future.done():
                loop.call_soon_threadsafe(
                    result_future.set_exception, ValueError(f"Symbol {symbol} not found")
                )

        def _send():
            self.client.send(req).addCallback(on_symbols)

        reactor.callFromThread(_send)
        self._symbol_id = await asyncio.wait_for(result_future, timeout=15)
        return self._symbol_id

    async def fetch_candles(
        self,
        symbol: str = "USDJPY",
        timeframe: str = None,
        count: int = 500,
    ) -> pd.DataFrame:
        if timeframe is None:
            timeframe = config.CANDLE_TIMEFRAME
        symbol_id = await self.get_symbol_id(symbol)
        period = TIMEFRAME_MAP.get(timeframe, ProtoOATrendbarPeriod.M30)

        loop = asyncio.get_running_loop()
        result_future = loop.create_future()

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        bar_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["M30"])
        from_ms = now_ms - (count + 100) * bar_ms

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = period
        req.fromTimestamp = from_ms
        req.toTimestamp = now_ms
        req.count = count

        def on_bars(resp):
            from ctrader_open_api import Protobuf
            resp = Protobuf.extract(resp)
            bars = []
            for bar in resp.trendbar:
                ts = datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, tz=timezone.utc)
                low = bar.low / 100_000
                bars.append({
                    "time": ts,
                    "open": (bar.low + bar.deltaOpen) / 100_000,
                    "high": (bar.low + bar.deltaHigh) / 100_000,
                    "low": low,
                    "close": (bar.low + bar.deltaClose) / 100_000,
                    "volume": bar.volume,
                })
            if not result_future.done():
                loop.call_soon_threadsafe(result_future.set_result, bars)

        def _send():
            self.client.send(req).addCallback(on_bars)

        reactor.callFromThread(_send)
        bars = await asyncio.wait_for(result_future, timeout=30)
        df = pd.DataFrame(bars)
        log.info(f"Fetched {len(df)} {timeframe} candles for {symbol}")
        return df

    async def subscribe_dom(self, symbol_id: int) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeDepthQuotesReq
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        req = ProtoOASubscribeDepthQuotesReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId.append(symbol_id)

        def _on_sub(resp):
            if not done.done():
                loop.call_soon_threadsafe(done.set_result, True)

        def _send():
            self.client.send(req).addCallback(_on_sub)

        reactor.callFromThread(_send)
        await asyncio.wait_for(done, timeout=15)
        log.info(f"DOM subscription active for symbol_id={symbol_id}")

    def get_dom_snapshot(self, symbol: str = "USDJPY") -> dict:
        """Return a dict with current order book microstructure metrics."""
        from datetime import datetime, timezone
        bids = sorted(self._dom_bids.values(), key=lambda x: x["price"], reverse=True)[:10]
        asks = sorted(self._dom_asks.values(), key=lambda x: x["price"])[:10]

        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None

        if best_bid is not None and best_ask is not None:
            spread_pips = (best_ask - best_bid) / USDJPY_PIP
        else:
            spread_pips = None

        bid_depth_total = float(sum(q["volume"] for q in bids)) if bids else None
        ask_depth_total = float(sum(q["volume"] for q in asks)) if asks else None

        if bid_depth_total is not None and ask_depth_total is not None:
            total = bid_depth_total + ask_depth_total
            order_imbalance = bid_depth_total / total if total > 0 else 0.5
        else:
            order_imbalance = 0.5

        levels_count = len(self._dom_bids) + len(self._dom_asks)

        return {
            "time": datetime.now(timezone.utc),
            "symbol": symbol,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pips": spread_pips,
            "bid_depth_total": bid_depth_total,
            "ask_depth_total": ask_depth_total,
            "order_imbalance": order_imbalance,
            "levels_count": levels_count,
        }

    def register_message_callback(self, msg_type: int, fn) -> None:
        """Register a callback invoked for every inbound message of msg_type."""
        self._message_callbacks.setdefault(msg_type, []).append(fn)

    async def fetch_trendbars_range(
        self,
        symbol_id: int,
        from_ms: int,
        to_ms: int,
        period_str: str,
    ) -> list[dict]:
        """Fetch candles between from_ms and to_ms (epoch milliseconds).

        Uses the same Twisted bridge as fetch_candles. Returns a list of dicts
        with correct delta-decoded OHLCV values.
        """
        period = TIMEFRAME_MAP.get(period_str, ProtoOATrendbarPeriod.M5)
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = period
        req.fromTimestamp = from_ms
        req.toTimestamp = to_ms

        def on_bars(resp):
            from ctrader_open_api import Protobuf
            resp = Protobuf.extract(resp)
            bars = []
            for bar in resp.trendbar:
                ts = datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, tz=timezone.utc)
                low = bar.low / 100_000
                bars.append({
                    "time": ts,
                    "open": (bar.low + bar.deltaOpen) / 100_000,
                    "high": (bar.low + bar.deltaHigh) / 100_000,
                    "low": low,
                    "close": (bar.low + bar.deltaClose) / 100_000,
                    "volume": bar.volume,
                })
            if not result_future.done():
                loop.call_soon_threadsafe(result_future.set_result, bars)

        def _send():
            self.client.send(req).addCallback(on_bars)

        reactor.callFromThread(_send)
        bars = await asyncio.wait_for(result_future, timeout=30)
        log.debug(f"fetch_trendbars_range: {len(bars)} {period_str} bars [{from_ms}..{to_ms}]")
        return bars

    def disconnect(self):
        if self.client:
            if reactor.running:
                reactor.callFromThread(self.client.stopService)
            else:
                self.client.stopService()


async def connect_ctrader() -> CTraderSession:
    """Establish authenticated connection to cTrader demo server."""
    session = CTraderSession()
    await session.connect()
    return session


async def fetch_candles(
    session: CTraderSession,
    symbol: str = "USDJPY",
    timeframe: str = None,
    count: int = 500,
) -> pd.DataFrame:
    """Fetch OHLCV candles and compute indicators."""
    if timeframe is None:
        timeframe = config.CANDLE_TIMEFRAME
    df = await session.fetch_candles(symbol=symbol, timeframe=timeframe, count=count)
    if not df.empty:
        df = compute_indicators(df)
    return df
