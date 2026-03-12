"""
risk_manager.py — Position sizing and drawdown protection.
"""
import logging
from datetime import datetime, timezone, date

from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

PIP_VALUE_USD_PER_LOT = 1000.0   # $1,000 per pip per standard lot for USD/JPY
# (1 lot = 100,000 units; 1 pip = 0.01 JPY; approx $9–$10 but using $10 for safety)


def calculate_position_size(
    account_balance_eur: float,
    atr_pips: float,
    direction: int,
) -> float:
    """
    Risk 1% of account per trade.
    stop_loss_pips = ATR_MULTIPLIER_SL * atr_pips
    lots = (account * 0.01) / (stop_loss_pips * pip_value_per_lot)
    Capped at MAX_POSITION_SIZE_LOTS.
    Returns lots rounded to 0.01.
    """
    if atr_pips <= 0:
        log.warning("ATR is zero or negative — using minimum position size")
        return 0.01

    risk_eur = account_balance_eur * 0.01
    stop_loss_pips = config.ATR_MULTIPLIER_SL * atr_pips

    # Convert pip value: for USD/JPY, 1 pip ≈ $9.something; approximate with EUR parity
    # We use a conservative flat estimate of €8 per pip per lot
    pip_value_eur_per_lot = 8.0
    raw_lots = risk_eur / (stop_loss_pips * pip_value_eur_per_lot)

    lots = round(min(raw_lots, config.MAX_POSITION_SIZE_LOTS), 2)
    lots = max(lots, 0.01)  # cTrader minimum 0.01 lots

    log.info(
        f"Position size: {lots} lots "
        f"(balance={account_balance_eur:.0f} EUR, "
        f"risk={risk_eur:.2f} EUR, "
        f"SL={stop_loss_pips:.1f} pips)"
    )
    return lots


def check_daily_drawdown(engine) -> bool:
    """
    Return True (ok to trade) if today's cumulative PnL is above -MAX_DAILY_DRAWDOWN_EUR.
    Return False (pause trading) if the daily loss limit has been breached.
    """
    today = date.today()
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(SUM(pnl_eur), 0) AS daily_pnl
                FROM trades
                WHERE closed_at::date = :today
                  AND pnl_eur IS NOT NULL
            """),
            {"today": today},
        ).fetchone()

    daily_pnl = float(row[0]) if row else 0.0

    if daily_pnl <= -config.MAX_DAILY_DRAWDOWN_EUR:
        log.warning(
            f"Daily drawdown limit reached: {daily_pnl:.2f} EUR "
            f"(limit: -{config.MAX_DAILY_DRAWDOWN_EUR} EUR)"
        )
        return False

    log.debug(f"Daily PnL: {daily_pnl:.2f} EUR — within drawdown limit")
    return True


async def has_open_position(client) -> bool:
    """Check if the bot currently has an open USD/JPY position via cTrader."""
    try:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq
        import asyncio

        result_future = asyncio.get_event_loop().create_future()

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID

        def on_reconcile(resp):
            if not result_future.done():
                result_future.set_result(resp)

        deferred = client.send(req)
        deferred.addCallback(on_reconcile)

        resp = await asyncio.wait_for(result_future, timeout=10)
        positions = list(resp.position)
        return len(positions) > 0
    except Exception as exc:
        log.error(f"Error checking open positions: {exc}")
        return False
