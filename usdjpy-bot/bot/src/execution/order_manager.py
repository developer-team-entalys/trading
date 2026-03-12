"""
order_manager.py — Place, modify, and close orders via cTrader Open API.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from monitoring.telegram_alert import send_order_placed, send_order_closed, send_error

log = logging.getLogger(__name__)

USDJPY_PIP = 0.01
LOT_UNITS = 100_000  # 1 lot = 100,000 units


def _pips_to_price(direction: int, entry: float, pips: float) -> float:
    """Convert pips offset to absolute price."""
    return entry + direction * pips * USDJPY_PIP


async def _get_usdjpy_symbol_id(client) -> int:
    """Dynamically look up USDJPY symbol ID from cTrader."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListReq

    result_future = asyncio.get_event_loop().create_future()

    req = ProtoOASymbolsListReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
    req.includeArchivedSymbols = False

    def on_symbols(resp):
        for sym in resp.symbol:
            if sym.symbolName == "USDJPY":
                if not result_future.done():
                    result_future.set_result(sym.symbolId)
                return
        if not result_future.done():
            result_future.set_exception(ValueError("USDJPY symbol not found"))

    deferred = client.send(req)
    deferred.addCallback(on_symbols)

    return await asyncio.wait_for(result_future, timeout=15)


async def _get_account_balance(client) -> float:
    """Fetch current account balance in account currency."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderReq

    result_future = asyncio.get_event_loop().create_future()

    req = ProtoOATraderReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID

    def on_trader(resp):
        if not result_future.done():
            balance = resp.trader.balance / 100  # cTrader returns cents
            result_future.set_result(balance)

    deferred = client.send(req)
    deferred.addCallback(on_trader)

    return await asyncio.wait_for(result_future, timeout=10)


async def place_market_order(
    client,
    engine,
    direction: int,
    lots: float,
    stop_loss_pips: float,
    take_profit_pips: float,
    signal_confidence: float,
) -> dict:
    """
    Place a market order with SL and TP on cTrader.
    direction: 1=buy, -1=sell
    Returns order details dict.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATradeSide,
    )

    symbol_id = await _get_usdjpy_symbol_id(client)
    volume = int(lots * LOT_UNITS)

    result_future = asyncio.get_event_loop().create_future()

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
    req.symbolId = symbol_id
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY if direction == 1 else ProtoOATradeSide.SELL
    req.volume = volume
    req.relativeStopLoss = int(stop_loss_pips * 10)   # in 1/10 pips
    req.relativeTakeProfit = int(take_profit_pips * 10)
    req.guaranteedStopLoss = False
    req.trailingStopLoss = False
    req.comment = f"usdjpy-bot conf={signal_confidence:.2f}"

    def on_execution(resp):
        if not result_future.done():
            result_future.set_result(resp)

    def on_error(failure):
        if not result_future.done():
            result_future.set_exception(Exception(str(failure)))

    deferred = client.send(req)
    deferred.addCallback(on_execution)
    deferred.addErrback(on_error)

    try:
        resp = await asyncio.wait_for(result_future, timeout=15)
        order = resp.order if hasattr(resp, "order") else None
        position = resp.position if hasattr(resp, "position") else None

        entry_price = position.price / 100_000 if position else 0.0
        order_id = order.orderId if order else 0
        sl_price = _pips_to_price(-direction, entry_price, stop_loss_pips)
        tp_price = _pips_to_price(direction, entry_price, take_profit_pips)

        # Fetch rollover data for this trade
        from data.rollover_fetcher import get_today_rollover
        rollover = get_today_rollover(engine)

        # Log to DB
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO trades (
                        opened_at, direction, lots, entry_price,
                        stop_loss, take_profit, signal_confidence, ctrader_order_id,
                        rate_differential_at_open, swap_long_at_open, swap_short_at_open,
                        nights_held, swap_earned_eur
                    ) VALUES (
                        :opened_at, :direction, :lots, :entry_price,
                        :stop_loss, :take_profit, :signal_confidence, :order_id,
                        :rate_differential_at_open, :swap_long_at_open, :swap_short_at_open,
                        0, 0.0
                    )
                """),
                {
                    "opened_at": datetime.now(timezone.utc),
                    "direction": direction,
                    "lots": lots,
                    "entry_price": entry_price,
                    "stop_loss": sl_price,
                    "take_profit": tp_price,
                    "signal_confidence": signal_confidence,
                    "order_id": order_id,
                    "rate_differential_at_open": rollover["rate_differential"],
                    "swap_long_at_open":  rollover["swap_long_pts"],
                    "swap_short_at_open": rollover["swap_short_pts"],
                },
            )

        details = {
            "order_id": order_id,
            "direction": direction,
            "lots": lots,
            "entry_price": entry_price,
            "stop_loss": sl_price,
            "sl_pips": stop_loss_pips,
            "take_profit": tp_price,
            "tp_pips": take_profit_pips,
        }

        await send_order_placed(details)
        log.info(f"Order placed: {details}")
        return details

    except Exception as exc:
        log.error(f"Order placement failed: {exc}")
        await send_error(f"Order placement failed", str(exc))
        raise


async def close_position(client, engine, position_id: int, reason: str) -> dict:
    """
    Close an open position by ID and record the result in DB.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq

    result_future = asyncio.get_event_loop().create_future()

    req = ProtoOAClosePositionReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
    req.positionId = position_id

    def on_close(resp):
        if not result_future.done():
            result_future.set_result(resp)

    def on_error(failure):
        if not result_future.done():
            result_future.set_exception(Exception(str(failure)))

    deferred = client.send(req)
    deferred.addCallback(on_close)
    deferred.addErrback(on_error)

    try:
        resp = await asyncio.wait_for(result_future, timeout=15)
        position = resp.position if hasattr(resp, "position") else None
        exit_price = position.price / 100_000 if position else 0.0
        pnl_eur = position.netProfit / 100 if position else 0.0

        closed_at = datetime.now(timezone.utc)

        # Fetch trade record to compute swap earnings
        swap_earned_eur = 0.0
        nights_held = 0
        with engine.connect() as conn:
            trade_row = conn.execute(text("""
                SELECT opened_at, direction, lots, swap_long_at_open, swap_short_at_open
                FROM trades
                WHERE ctrader_order_id = :position_id AND closed_at IS NULL
                LIMIT 1
            """), {"position_id": position_id}).fetchone()

        if trade_row:
            opened_at, trade_dir, trade_lots, swap_long, swap_short = trade_row
            nights_held = (closed_at.date() - opened_at.date()).days
            swap_per_night = (swap_long or 0.0) if trade_dir == 1 else (swap_short or 0.0)
            swap_earned_eur = swap_per_night * (trade_lots or 0.1) * 6.67 * nights_held

        # Update DB
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE trades
                    SET closed_at = :closed_at,
                        exit_price = :exit_price,
                        pnl_eur = :pnl_eur,
                        close_reason = :close_reason,
                        nights_held = :nights_held,
                        swap_earned_eur = :swap_earned_eur
                    WHERE ctrader_order_id = :position_id
                      AND closed_at IS NULL
                """),
                {
                    "closed_at": closed_at,
                    "exit_price": exit_price,
                    "pnl_eur": pnl_eur,
                    "close_reason": reason,
                    "nights_held": nights_held,
                    "swap_earned_eur": round(swap_earned_eur, 4),
                    "position_id": position_id,
                },
            )

        details = {
            "position_id": position_id,
            "exit_price": exit_price,
            "pnl_eur": pnl_eur,
            "close_reason": reason,
            "pnl_pips": (pnl_eur / 8.0) if pnl_eur else 0.0,  # rough estimate
            "swap_earned_eur": round(swap_earned_eur, 4),
        }
        await send_order_closed(details)
        log.info(f"Position {position_id} closed: {reason}, PnL={pnl_eur:.2f} EUR, swap={swap_earned_eur:.4f} EUR")
        return details

    except Exception as exc:
        log.error(f"Failed to close position {position_id}: {exc}")
        await send_error("Failed to close position", str(exc))
        raise


async def check_signal_reversal(client, engine, new_signal: dict) -> None:
    """
    If bot has an open position opposite to the new signal, close and reverse.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq

    result_future = asyncio.get_event_loop().create_future()

    req = ProtoOAReconcileReq()
    req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID

    def on_reconcile(resp):
        if not result_future.done():
            result_future.set_result(resp)

    deferred = client.send(req)
    deferred.addCallback(on_reconcile)

    try:
        resp = await asyncio.wait_for(result_future, timeout=10)
        positions = list(resp.position)

        for pos in positions:
            pos_direction = 1 if pos.tradeSide == 1 else -1  # 1=BUY, 2=SELL
            if pos_direction != new_signal["direction"]:
                log.info(
                    f"Signal reversal: closing position {pos.positionId} "
                    f"({pos_direction}) to reverse to {new_signal['direction']}"
                )
                await close_position(client, engine, pos.positionId, "signal_reverse")

    except Exception as exc:
        log.error(f"Signal reversal check failed: {exc}")
