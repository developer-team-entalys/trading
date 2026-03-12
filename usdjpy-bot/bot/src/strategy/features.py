"""
features.py — Build the feature vector for ML prediction.
"""
import logging
from sqlalchemy import text

log = logging.getLogger(__name__)

SESSION_MAP = {"tokyo": 0, "london": 1, "new_york": 2, "overlap": 3}

USDJPY_PIP = 0.01


def build_feature_vector(engine, training_phase: int = 1) -> dict:
    """
    Query TimescaleDB and build the feature vector for prediction.

    Phase 1: COT + Technical features only (12 features).
    Phase 2: Phase 1 + sentiment features (21 features).

    Returns a dict with feature names and current values.
    """
    with engine.connect() as conn:

        # ── COT (latest weekly row) ─────────────────────────────
        cot_row = conn.execute(
            text("""
                SELECT noncomm_net_pct, noncomm_net_change, noncomm_net, open_interest
                FROM cot_data
                ORDER BY week_date DESC
                LIMIT 1
            """)
        ).fetchone()

        if cot_row:
            cot_noncomm_net_pct = float(cot_row[0] or 0)
            cot_noncomm_net_change = float(cot_row[1] or 0)
            cot_direction = 1 if cot_noncomm_net_pct > 0 else -1
            cot_extreme = 1 if abs(cot_noncomm_net_pct) > 30 else 0
        else:
            log.warning("No COT data found — using neutral defaults")
            cot_noncomm_net_pct = 0.0
            cot_noncomm_net_change = 0.0
            cot_direction = 0
            cot_extreme = 0

        # ── Candles (latest row) ────────────────────────────────
        candle_row = conn.execute(
            text("""
                SELECT atr_1h, atr_4h, rsi_14, session, distance_to_round, close
                FROM candles
                ORDER BY time DESC
                LIMIT 1
            """)
        ).fetchone()

        if candle_row:
            atr_1h = float(candle_row[0] or 0)
            atr_4h = float(candle_row[1] or 0)
            rsi_14 = float(candle_row[2] or 50)
            session_str = candle_row[3] or "tokyo"
            distance_to_round = float(candle_row[4] or 0)
            current_price = float(candle_row[5] or 0)
        else:
            log.warning("No candle data found — using neutral defaults")
            atr_1h = 0.0
            atr_4h = 0.0
            rsi_14 = 50.0
            session_str = "tokyo"
            distance_to_round = 0.0
            current_price = 0.0

        if rsi_14 > 70:
            rsi_zone = -1   # overbought
        elif rsi_14 < 30:
            rsi_zone = 1    # oversold
        else:
            rsi_zone = 0

        near_round = 1 if distance_to_round <= 20 else 0
        session = SESSION_MAP.get(session_str, 0)

        # ── Sentiment (Phase 2 only) ─────────────────────────────
        if training_phase == 2:
            sent_rows = conn.execute(
                text("""
                    SELECT long_pct, short_pct, avg_long_price, avg_short_price
                    FROM sentiment_data
                    ORDER BY time DESC
                    LIMIT 2
                """)
            ).fetchall()

            if sent_rows:
                latest = sent_rows[0]
                retail_long_pct = float(latest[0] or 50)
                retail_short_pct = float(latest[1] or 50)
                avg_long_price = float(latest[2] or 0)
                avg_short_price = float(latest[3] or 0)

                sentiment_trend_30m = (
                    retail_long_pct - float(sent_rows[1][0] or 50)
                    if len(sent_rows) >= 2 else 0.0
                )
                sentiment_extreme = 1 if (retail_long_pct > 75 or retail_short_pct > 75) else 0

                if retail_long_pct > 65:
                    retail_contrarian = -1
                elif retail_short_pct > 65:
                    retail_contrarian = 1
                else:
                    retail_contrarian = 0

                crowd_pain_long = (current_price - avg_long_price) / USDJPY_PIP
                crowd_pain_short = (avg_short_price - current_price) / USDJPY_PIP
            else:
                log.warning("No sentiment data found — using neutral Phase 2 defaults")
                retail_long_pct = 50.0
                retail_short_pct = 50.0
                retail_contrarian = 0
                avg_long_price = 0.0
                avg_short_price = 0.0
                sentiment_trend_30m = 0.0
                sentiment_extreme = 0
                crowd_pain_long = 0.0
                crowd_pain_short = 0.0

    features = {
        # COT
        "cot_noncomm_net_pct": cot_noncomm_net_pct,
        "cot_noncomm_net_change": cot_noncomm_net_change,
        "cot_direction": cot_direction,
        "cot_extreme": cot_extreme,
        # Technical
        "atr_1h": atr_1h,
        "atr_4h": atr_4h,
        "rsi_14": rsi_14,
        "rsi_zone": rsi_zone,
        "distance_to_round": distance_to_round,
        "near_round": near_round,
        "session": session,
        # Rollover (USD rates > JPY rates → long favorable)
        "rollover_direction": 1,
    }

    if training_phase == 2:
        features.update({
            "retail_long_pct": retail_long_pct,
            "retail_short_pct": retail_short_pct,
            "retail_contrarian": retail_contrarian,
            "sentiment_trend_30m": sentiment_trend_30m,
            "sentiment_extreme": sentiment_extreme,
            "avg_long_price": avg_long_price,
            "avg_short_price": avg_short_price,
            "crowd_pain_long": crowd_pain_long,
            "crowd_pain_short": crowd_pain_short,
        })

    log.debug(f"Feature vector (phase={training_phase}): {features}")
    return features
