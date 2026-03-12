"""
config.py — Load and validate all environment variables.
"""
import os
from dotenv import load_dotenv
import sqlalchemy

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


# ── Database ──────────────────────────────────────────────────
DATABASE_URL: str = _require("DATABASE_URL")
POSTGRES_USER: str = os.getenv("POSTGRES_USER", "botuser")
POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "changeme")
POSTGRES_DB: str = os.getenv("POSTGRES_DB", "usdjpy_bot")

# ── cTrader Open API ──────────────────────────────────────────
CTRADER_CLIENT_ID: str = os.getenv("CTRADER_CLIENT_ID", "")
CTRADER_CLIENT_SECRET: str = os.getenv("CTRADER_CLIENT_SECRET", "")
CTRADER_ACCOUNT_ID: int = _int("CTRADER_ACCOUNT_ID", 0)
CTRADER_HOST: str = os.getenv("CTRADER_HOST", "demo.ctraderapi.com")
CTRADER_PORT: int = _int("CTRADER_PORT", 5035)
CTRADER_ENV: str = os.getenv("CTRADER_ENV", "demo")

# ── Myfxbook ──────────────────────────────────────────────────
MYFXBOOK_EMAIL: str = os.getenv("MYFXBOOK_EMAIL", "")
MYFXBOOK_PASSWORD: str = os.getenv("MYFXBOOK_PASSWORD", "")

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Grafana ───────────────────────────────────────────────────
GF_SECURITY_ADMIN_PASSWORD: str = os.getenv("GF_SECURITY_ADMIN_PASSWORD", "changeme")

# ── Strategy Parameters ───────────────────────────────────────
INSTRUMENT: str = os.getenv("INSTRUMENT", "USD_JPY")
SYMBOL_NAME: str = os.getenv("SYMBOL_NAME", "USDJPY")
MAX_POSITION_SIZE_LOTS: float = _float("MAX_POSITION_SIZE_LOTS", 0.1)
MAX_DAILY_DRAWDOWN_EUR: float = _float("MAX_DAILY_DRAWDOWN_EUR", 50.0)
CONFIDENCE_THRESHOLD: float = _float("CONFIDENCE_THRESHOLD", 0.65)
ATR_MULTIPLIER_SL: float = _float("ATR_MULTIPLIER_SL", 1.5)
ATR_MULTIPLIER_TP: float = _float("ATR_MULTIPLIER_TP", 2.5)

# ── Timeframe ─────────────────────────────────────────────────
CANDLE_TIMEFRAME: str = os.getenv("CANDLE_TIMEFRAME", "M30")
SENTIMENT_INTERVAL_MIN: int = _int("SENTIMENT_INTERVAL_MIN", 30)

# ── Risk Parameters ───────────────────────────────────────────
RISK_PER_TRADE_PCT: float = _float("RISK_PER_TRADE_PCT", 0.01)

# ── Signal Parameters ─────────────────────────────────────────
TARGET_PIPS: int = _int("TARGET_PIPS", 15)
TARGET_CANDLES_AHEAD: int = _int("TARGET_CANDLES_AHEAD", 4)

# ── Training Phase ────────────────────────────────────────────
TRAINING_PHASE: int = _int("TRAINING_PHASE", 1)
MIN_SENTIMENT_ROWS: int = _int("MIN_SENTIMENT_ROWS", 2000)

# ── COT Data ──────────────────────────────────────────────────
COT_URL: str = os.getenv(
    "COT_URL",
    "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
)

# ── Required variables for live trading ──────────────────────
REQUIRED_FOR_TRADING = [
    "DATABASE_URL",
    "CTRADER_CLIENT_ID",
    "CTRADER_CLIENT_SECRET",
    "CTRADER_ACCOUNT_ID",
]

REQUIRED_FOR_ALERTS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def validate_required() -> None:
    """Raise EnvironmentError if any critical variable is missing."""
    missing = [k for k in REQUIRED_FOR_TRADING if not os.getenv(k)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def validate_db() -> None:
    """Test database connectivity; raise on failure."""
    engine = sqlalchemy.create_engine(DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    engine.dispose()


def validate() -> None:
    """Full config + DB validation."""
    validate_required()
    validate_db()
