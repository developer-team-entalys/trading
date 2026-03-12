"""
telegram_alert.py — Send structured alerts via Telegram bot.
"""
import logging
import traceback
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)


async def _send(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping alert")
        return
    try:
        from telegram import Bot
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        log.error(f"Telegram send failed: {exc}")


async def send_bot_started(next_signal_time: datetime) -> None:
    await _send(
        f"🟢 <b>USD/JPY Bot started</b> ✅\n"
        f"Next signal check: {next_signal_time.strftime('%H:%M UTC')}"
    )


async def send_bot_stopped() -> None:
    await _send("⛔ <b>USD/JPY Bot stopped</b>")


async def send_signal(signal: dict) -> None:
    f = signal.get("features", {})
    direction_label = {"LONG": "🐂 LONG", "SHORT": "🐻 SHORT", "HOLD": "⏸ HOLD"}.get(
        signal["action"], signal["action"]
    )
    session_names = {0: "Tokyo", 1: "London", 2: "New York", 3: "Overlap"}
    session = session_names.get(f.get("session", 0), "Unknown")

    await _send(
        f"📊 <b>Signal: {direction_label}</b> ({signal['confidence']:.0%})\n"
        f"COT: {'Long' if f.get('cot_direction', 0) > 0 else 'Short'} | "
        f"Sentiment: {f.get('retail_long_pct', 50):.0f}% Long\n"
        f"ATR: {f.get('atr_1h', 0):.1f} pips | Session: {session}"
    )


async def send_order_placed(details: dict) -> None:
    side = "BUY" if details["direction"] == 1 else "SELL"
    await _send(
        f"📈 <b>Order placed</b> ✅\n"
        f"{side} {details['lots']} lots USD/JPY @ {details['entry_price']:.3f}\n"
        f"SL: {details['stop_loss']:.3f} ({details['sl_pips']:.1f} pips)\n"
        f"TP: {details['take_profit']:.3f} ({details['tp_pips']:.1f} pips)"
    )


async def send_order_closed(details: dict) -> None:
    pnl = details.get("pnl_eur", 0)
    pips = details.get("pnl_pips", 0)
    reason_labels = {
        "sl": "Stop Loss hit",
        "tp": "Take Profit hit",
        "signal_reverse": "Signal reversal",
        "manual": "Manually closed",
    }
    reason = reason_labels.get(details.get("close_reason", ""), details.get("close_reason", ""))
    await _send(
        f"📉 <b>Position closed</b>\n"
        f"{reason} | PnL: {pnl:+.2f} EUR ({pips:+.1f} pips)"
    )


async def send_drawdown_warning(daily_loss: float) -> None:
    await _send(
        f"⚠️ <b>Daily drawdown limit reached</b>\n"
        f"Loss today: {daily_loss:.2f} EUR\n"
        f"Trading paused until tomorrow."
    )


async def send_error(context: str, error_message: str) -> None:
    tb = traceback.format_exc()
    last_line = tb.strip().split("\n")[-1] if tb.strip() else error_message
    await _send(
        f"❌ <b>Bot error</b>\n"
        f"<i>{context}</i>\n"
        f"{error_message}\n"
        f"<code>{last_line}</code>"
    )


async def send_phase2_ready(count: int) -> None:
    weeks = count // (48 * 7)  # 48 polls/day * 7 days
    await _send(
        f"⬆️ <b>Phase 2 Ready!</b>\n"
        f"{count} sentiment rows (~{weeks} weeks).\n"
        f"Set TRAINING_PHASE=2 in .env and restart to upgrade model."
    )


async def send_weekly_summary(stats: dict) -> None:
    await _send(
        f"📅 <b>Weekly Performance</b>\n"
        f"Trades: {stats.get('n_trades', 0)} | "
        f"Win rate: {stats.get('win_rate', 0):.0%}\n"
        f"PnL: {stats.get('weekly_pnl', 0):+.2f} EUR\n"
        f"Best: +{stats.get('best_trade', 0):.2f} EUR | "
        f"Worst: {stats.get('worst_trade', 0):.2f} EUR\n"
        f"Model accuracy: {stats.get('model_accuracy', 0):.0%}"
    )
