"""
Sentiment data from Myfxbook Community Outlook API.

API flow:
  Step 1 - Login to get session token:
    GET https://www.myfxbook.com/api/login.json
        ?email=X&password=Y
    Response: {"error": false, "session": "DSL07vu14QxHWErTIAFrH40"}

  Step 2 - Fetch community outlook with session:
    GET https://www.myfxbook.com/api/get-community-outlook.json
        ?session=DSL07vu14QxHWErTIAFrH40
    Response: {"symbols": [{"name": "USDJPY", "longPercentage": 45,
               "shortPercentage": 55, ...}], ...}

Rate limit: 100 requests per 24 hours (free tier)
  -> Bot polls every 60 min = 24 requests/day
  -> Session expires after ~24h, auto-renewed on next poll

Symbol name in response: "USDJPY" (no underscore)
"""

import requests
import structlog
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import unquote

logger = structlog.get_logger()

# Module-level session cache (persists within container lifetime)
_session_token: Optional[str] = None
_session_acquired_at: Optional[datetime] = None
SESSION_TTL_HOURS = 23  # Renew session 1h before expiry

# Rate limit tracking (100 req/24h on free tier; we budget 90 for safety)
_daily_request_count: int = 0
_daily_count_reset_at: Optional[datetime] = None
DAILY_REQUEST_LIMIT = 90

MYFXBOOK_LOGIN_URL = "https://www.myfxbook.com/api/login.json"
MYFXBOOK_OUTLOOK_URL = "https://www.myfxbook.com/api/get-community-outlook.json"
TARGET_SYMBOL = "USDJPY"


def _check_rate_limit() -> bool:
    """
    Track daily API requests. Returns False (block the call) if limit is reached.
    Resets the counter at midnight UTC.
    """
    global _daily_request_count, _daily_count_reset_at

    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    if _daily_count_reset_at is None or now >= _daily_count_reset_at:
        _daily_request_count = 0
        _daily_count_reset_at = midnight

    if _daily_request_count >= DAILY_REQUEST_LIMIT:
        logger.warning("sentiment_fetcher.rate_limit_reached",
                       count=_daily_request_count, limit=DAILY_REQUEST_LIMIT)
        return False
    return True


def _get_session(email: str, password: str) -> Optional[str]:
    """
    Login to Myfxbook and return session token.
    Returns None if login fails (wrong credentials or API down).
    """
    global _session_token, _session_acquired_at

    # Return cached session if still valid
    if _session_token and _session_acquired_at:
        age = datetime.now(timezone.utc) - _session_acquired_at
        if age < timedelta(hours=SESSION_TTL_HOURS):
            return _session_token

    # Acquire new session
    if not email or not password:
        logger.warning("sentiment_fetcher.no_credentials",
                       msg="MYFXBOOK_EMAIL or MYFXBOOK_PASSWORD not set")
        return None

    try:
        resp = requests.get(
            MYFXBOOK_LOGIN_URL,
            params={"email": email, "password": password},
            timeout=10
        )
        data = resp.json()

        if data.get("error") is False and data.get("session"):
            _session_token = unquote(data["session"])
            _session_acquired_at = datetime.now(timezone.utc)
            logger.info("sentiment_fetcher.session_acquired")
            return _session_token
        else:
            logger.error("sentiment_fetcher.login_failed",
                         message=data.get("message", "unknown error"))
            return None

    except Exception as e:
        logger.error("sentiment_fetcher.login_error", error=str(e))
        return None


def fetch_myfxbook_sentiment(email: str, password: str) -> Optional[dict]:
    """
    Fetch USD/JPY community sentiment from Myfxbook.

    Returns:
        {
            'long_pct': float,          # e.g. 45.0
            'short_pct': float,         # e.g. 55.0
            'long_positions': int,      # number of long positions
            'short_positions': int,     # number of short positions
            'avg_long_price': float,    # average entry price of longs
            'avg_short_price': float,   # average entry price of shorts
            'source': 'myfxbook',
            'fetched_at': datetime
        }
        Returns None if sentiment unavailable.
    """
    global _session_token, _daily_request_count

    if not _check_rate_limit():
        return None

    session = _get_session(email, password)
    if not session:
        return None
    _daily_request_count += 1  # login request counted

    try:
        resp = requests.get(
            MYFXBOOK_OUTLOOK_URL,
            params={"session": session},
            timeout=10
        )
        data = resp.json()

        if data.get("error") is not False:
            logger.error("sentiment_fetcher.outlook_error",
                         message=data.get("message"))
            # Invalidate session so next call re-authenticates
            _session_token = None
            return None

        # Find USDJPY in symbols list
        symbols = data.get("symbols", [])
        usdjpy = next(
            (s for s in symbols if s.get("name") == TARGET_SYMBOL),
            None
        )

        if not usdjpy:
            logger.error("sentiment_fetcher.symbol_not_found",
                         symbol=TARGET_SYMBOL,
                         available=[s.get("name") for s in symbols[:5]])
            return None

        result = {
            "long_pct": float(usdjpy.get("longPercentage", 0)),
            "short_pct": float(usdjpy.get("shortPercentage", 0)),
            "long_positions": int(usdjpy.get("longPositions", 0)),
            "short_positions": int(usdjpy.get("shortPositions", 0)),
            "avg_long_price": float(usdjpy.get("avgLongPrice", 0)),
            "avg_short_price": float(usdjpy.get("avgShortPrice", 0)),
            "source": "myfxbook",
            "fetched_at": datetime.now(timezone.utc)
        }

        _daily_request_count += 1  # outlook request counted
        logger.info("sentiment_fetcher.success",
                    long_pct=result["long_pct"],
                    short_pct=result["short_pct"])
        return result

    except Exception as e:
        logger.error("sentiment_fetcher.fetch_error", error=str(e))
        return None


def get_sentiment(email: str, password: str) -> Optional[dict]:
    """
    Main entry point called by the hourly scheduler.
    Fetches Myfxbook sentiment. Returns None if unavailable
    (bot continues without sentiment signal).
    """
    return fetch_myfxbook_sentiment(email, password)


def save_sentiment_to_db(data: Optional[dict], engine) -> None:
    """
    Insert sentiment snapshot into TimescaleDB sentiment_data table.
    No-ops silently if data is None.
    """
    if not data:
        return

    from sqlalchemy import text
    sql = text("""
        INSERT INTO sentiment_data (
            time, long_pct, short_pct,
            long_positions, short_positions,
            avg_long_price, avg_short_price,
            source
        ) VALUES (
            :time, :long_pct, :short_pct,
            :long_positions, :short_positions,
            :avg_long_price, :avg_short_price,
            :source
        )
        ON CONFLICT DO NOTHING
    """)

    with engine.begin() as conn:
        conn.execute(sql, {
            "time": data["fetched_at"],
            "long_pct": data["long_pct"],
            "short_pct": data["short_pct"],
            "long_positions": data.get("long_positions"),
            "short_positions": data.get("short_positions"),
            "avg_long_price": data.get("avg_long_price"),
            "avg_short_price": data.get("avg_short_price"),
            "source": data["source"],
        })


def get_sentiment_count(engine) -> int:
    """Return the total number of rows in sentiment_data."""
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM sentiment_data")).scalar() or 0
