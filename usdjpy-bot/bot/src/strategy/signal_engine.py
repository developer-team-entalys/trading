"""
signal_engine.py — Combine all signals into a final BUY/SELL/HOLD decision.
"""
import logging
import json
from datetime import datetime, timezone

from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from strategy.features import build_feature_vector
from strategy.decision_tree import predict

log = logging.getLogger(__name__)

SESSION_NAMES = {0: "tokyo", 1: "london", 2: "new_york", 3: "overlap"}


def _save_signal(signal: dict, engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO signals (
                    time, direction, confidence,
                    cot_signal, sentiment_signal, technical_signal, rollover_signal,
                    atr_1h, training_phase, features
                ) VALUES (
                    :time, :direction, :confidence,
                    :cot_signal, :sentiment_signal, :technical_signal, :rollover_signal,
                    :atr_1h, :training_phase, :features
                )
            """),
            {
                "time": signal["timestamp"],
                "direction": signal["direction"],
                "confidence": signal["confidence"],
                "cot_signal": signal["features"].get("cot_direction", 0),
                "sentiment_signal": signal["features"].get("retail_contrarian", 0),
                "technical_signal": signal["features"].get("rsi_zone", 0),
                "rollover_signal": signal["features"].get("rollover_direction", 1),
                "atr_1h": signal["features"].get("atr_1h", 0),
                "training_phase": signal["training_phase"],
                "features": json.dumps(signal["features"]),
            },
        )


def compute_signal(engine) -> dict:
    """
    Main signal computation. Called every cycle by the scheduler.

    1. Build feature vector from DB (phase-aware)
    2. Overlay live sentiment (always, for logging/history)
    3. Get ML prediction
    4. Apply hard override rules (Phase 2 only)
    5. Confidence gate → HOLD if below threshold
    6. Log to DB
    7. Return full signal dict
    """
    training_phase = config.TRAINING_PHASE
    features = build_feature_vector(engine, training_phase)

    # Fetch live sentiment and overlay (always, for logging + history building)
    from data.sentiment_fetcher import get_sentiment
    live_sentiment = get_sentiment(config.MYFXBOOK_EMAIL, config.MYFXBOOK_PASSWORD)
    if live_sentiment:
        features["retail_long_pct"] = live_sentiment["long_pct"]
        features["retail_short_pct"] = live_sentiment["short_pct"]
        if training_phase == 2:
            # Recompute contrarian from live values (already in features for Phase 2)
            _long = features["retail_long_pct"]
            _short = features["retail_short_pct"]
            features["retail_contrarian"] = -1 if _long > 65 else (1 if _short > 65 else 0)
    else:
        log.warning("sentiment_unavailable — using neutral 50/50")
        features.setdefault("retail_long_pct", 50.0)
        features.setdefault("retail_short_pct", 50.0)

    ml_direction, ml_confidence = predict(features)

    direction = ml_direction
    confidence = ml_confidence
    override_reason = None

    retail_long_pct = features.get("retail_long_pct", 50.0)
    retail_short_pct = features.get("retail_short_pct", 50.0)
    cot_extreme = features.get("cot_extreme", 0)
    cot_direction = features.get("cot_direction", 0)
    session = features.get("session", 0)

    # Hard override: extreme retail sentiment (contrarian) — Phase 2 only
    if training_phase == 2:
        if retail_long_pct > 80:
            direction = -1
            override_reason = f"Extreme retail long ({retail_long_pct:.0f}%) — force SHORT"
            log.info(override_reason)
        elif retail_short_pct > 80:
            direction = 1
            override_reason = f"Extreme retail short ({retail_short_pct:.0f}%) — force LONG"
            log.info(override_reason)

    # COT conflict: reduce confidence
    if cot_extreme and cot_direction != ml_direction:
        confidence = max(0.0, confidence - 0.15)
        log.info(
            f"COT extreme but conflicts with ML direction — confidence reduced to {confidence:.2f}"
        )

    # Tokyo session penalty (low liquidity)
    if session == 0:
        confidence = max(0.0, confidence - 0.10)
        log.debug(f"Tokyo session penalty — confidence reduced to {confidence:.2f}")

    # Confidence gate
    if confidence < config.CONFIDENCE_THRESHOLD:
        direction = 0
        log.info(
            f"Confidence {confidence:.2f} below threshold {config.CONFIDENCE_THRESHOLD} — HOLD"
        )

    action_map = {1: "LONG", -1: "SHORT", 0: "HOLD"}
    action = action_map[direction]

    signal = {
        "direction": direction,
        "confidence": confidence,
        "action": action,
        "features": features,
        "ml_direction": ml_direction,
        "ml_confidence": ml_confidence,
        "override_reason": override_reason,
        "training_phase": training_phase,
        "timestamp": datetime.now(timezone.utc),
    }

    try:
        _save_signal(signal, engine)
    except Exception as exc:
        log.error(f"Failed to save signal to DB: {exc}")

    log.info(
        f"Signal: {action} | confidence={confidence:.2f} | "
        f"ml={action_map.get(ml_direction, '?')}({ml_confidence:.2f}) | "
        f"phase={training_phase}"
    )
    return signal
