"""
ctrader_client.py — cTrader Open API connection management.
"""
import asyncio
import logging

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from data.candle_fetcher import CTraderSession

log = logging.getLogger(__name__)

_session: CTraderSession | None = None


async def get_session() -> CTraderSession:
    """Return (or create) the global cTrader session."""
    global _session
    if _session is None or _session.client is None:
        log.info("Connecting to cTrader...")
        _session = CTraderSession()
        await _session.connect()
    return _session


async def disconnect() -> None:
    """Gracefully disconnect from cTrader."""
    global _session
    if _session is not None:
        _session.disconnect()
        _session = None
        log.info("cTrader connection closed")
