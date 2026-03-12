"""
decision_tree.py — Train and serve the USD/JPY direction classifier.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

import joblib
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import classification_report, accuracy_score
from sqlalchemy import text

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

TARGET_CLASSES = {-1: "SHORT", 0: "HOLD", 1: "LONG"}
MODEL_PATH = "/app/models/decision_tree.joblib"

# Phase-specific feature column lists
FEATURE_COLS_P1 = [
    "cot_noncomm_net_pct",
    "cot_noncomm_net_change",
    "cot_direction",
    "cot_extreme",
    "atr_1h",
    "atr_4h",
    "rsi_14",
    "rsi_zone",
    "distance_to_round",
    "near_round",
    "session",
    "rollover_direction",
    "rate_differential",
    "carry_strength",
]

FEATURE_COLS_P2 = FEATURE_COLS_P1 + [
    "retail_long_pct",
    "retail_short_pct",
    "retail_contrarian",
    "sentiment_trend_30m",
    "sentiment_extreme",
    "avg_long_price",
    "avg_short_price",
    "crowd_pain_long",
    "crowd_pain_short",
]

# Legacy alias (used by predict() when phase is unknown)
FEATURE_COLS = FEATURE_COLS_P2


def build_training_dataset(engine, training_phase: int = 1) -> tuple:
    """
    Build training set from DB.

    Phase 1: candles + COT only. Min 500 samples.
    Phase 2: candles + COT + sentiment. Min 2000 samples.
    Target: ±TARGET_PIPS in TARGET_CANDLES_AHEAD bars.
    """
    with engine.connect() as conn:
        if training_phase == 1:
            df = pd.read_sql(
                text("""
                    SELECT
                        c.time,
                        c.close,
                        c.atr_1h,
                        c.atr_4h,
                        c.rsi_14,
                        c.session,
                        c.distance_to_round,
                        cd.noncomm_net_pct     AS cot_noncomm_net_pct,
                        cd.noncomm_net_change  AS cot_noncomm_net_change
                    FROM candles c
                    LEFT JOIN LATERAL (
                        SELECT noncomm_net_pct, noncomm_net_change
                        FROM cot_data
                        WHERE week_date <= c.time::date
                        ORDER BY week_date DESC
                        LIMIT 1
                    ) cd ON TRUE
                    ORDER BY c.time ASC
                """),
                conn,
            )
        else:
            df = pd.read_sql(
                text("""
                    SELECT
                        c.time,
                        c.close,
                        c.atr_1h,
                        c.atr_4h,
                        c.rsi_14,
                        c.session,
                        c.distance_to_round,
                        cd.noncomm_net_pct     AS cot_noncomm_net_pct,
                        cd.noncomm_net_change  AS cot_noncomm_net_change,
                        s.long_pct             AS retail_long_pct,
                        s.short_pct            AS retail_short_pct,
                        s.avg_long_price,
                        s.avg_short_price
                    FROM candles c
                    LEFT JOIN LATERAL (
                        SELECT noncomm_net_pct, noncomm_net_change
                        FROM cot_data
                        WHERE week_date <= c.time::date
                        ORDER BY week_date DESC
                        LIMIT 1
                    ) cd ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT long_pct, short_pct, avg_long_price, avg_short_price
                        FROM sentiment_data
                        WHERE time <= c.time
                        ORDER BY time DESC
                        LIMIT 1
                    ) s ON TRUE
                    ORDER BY c.time ASC
                """),
                conn,
            )

    min_samples = 500 if training_phase == 1 else config.MIN_SENTIMENT_ROWS
    if len(df) < min_samples:
        raise ValueError(
            f"Insufficient training data: {len(df)} rows "
            f"(minimum {min_samples} required for phase {training_phase}). "
            "Run the bot in data-collection mode first."
        )

    # Future close price
    df["future_close"] = df["close"].shift(-config.TARGET_CANDLES_AHEAD)
    df = df.dropna(subset=["future_close", "close"])

    # Price change in pips
    df["pip_change"] = (df["future_close"] - df["close"]) / 0.01

    # Target labels
    df["target"] = 0
    df.loc[df["pip_change"] >= config.TARGET_PIPS, "target"] = 1
    df.loc[df["pip_change"] <= -config.TARGET_PIPS, "target"] = -1

    # Derived COT features
    df["cot_direction"] = (df["cot_noncomm_net_pct"] > 0).map({True: 1, False: -1})
    df["cot_extreme"] = (df["cot_noncomm_net_pct"].abs() > 30).astype(int)
    df["cot_noncomm_net_change"] = df["cot_noncomm_net_change"].fillna(0)

    # Derived technical features
    df["rsi_zone"] = 0
    df.loc[df["rsi_14"] > 70, "rsi_zone"] = -1
    df.loc[df["rsi_14"] < 30, "rsi_zone"] = 1
    df["near_round"] = (df["distance_to_round"] <= 20).astype(int)

    session_map = {"tokyo": 0, "london": 1, "new_york": 2, "overlap": 3}
    df["session"] = df["session"].map(session_map).fillna(0).astype(int)

    # Derived rollover/carry features from FRED interest rate history
    from data.rollover_fetcher import get_historical_rate_differential
    rates_df = get_historical_rate_differential(engine)
    if not rates_df.empty:
        df["candle_date"] = pd.to_datetime(df["time"]).dt.date
        rates_df = rates_df.copy()
        rates_df["date"] = pd.to_datetime(rates_df["date"]).dt.date
        df = df.sort_values("time").reset_index(drop=True)
        rates_df = rates_df.sort_values("date").reset_index(drop=True)
        df = pd.merge_asof(
            df,
            rates_df[["date", "rate_differential", "rollover_direction", "carry_strength"]],
            left_on="candle_date",
            right_on="date",
            direction="backward",
        )
        df["rate_differential"]  = df["rate_differential"].fillna(0.0)
        df["rollover_direction"] = df["rollover_direction"].fillna(1)
        df["carry_strength"]     = df["carry_strength"].fillna(0.0)
    else:
        df["rate_differential"]  = 0.0
        df["rollover_direction"] = 1
        df["carry_strength"]     = 0.0

    # Derived sentiment features (Phase 2 only)
    if training_phase == 2:
        df["retail_long_pct"] = df["retail_long_pct"].fillna(50.0)
        df["retail_short_pct"] = df["retail_short_pct"].fillna(50.0)
        df["avg_long_price"] = df["avg_long_price"].fillna(df["close"])
        df["avg_short_price"] = df["avg_short_price"].fillna(df["close"])

        df["retail_contrarian"] = 0
        df.loc[df["retail_long_pct"] > 65, "retail_contrarian"] = -1
        df.loc[df["retail_short_pct"] > 65, "retail_contrarian"] = 1

        df["sentiment_trend_30m"] = df["retail_long_pct"].diff(1).fillna(0)
        df["sentiment_extreme"] = (
            (df["retail_long_pct"] > 75) | (df["retail_short_pct"] > 75)
        ).astype(int)

        df["crowd_pain_long"] = (df["close"] - df["avg_long_price"]) / 0.01
        df["crowd_pain_short"] = (df["avg_short_price"] - df["close"]) / 0.01

    feature_cols = FEATURE_COLS_P1 if training_phase == 1 else FEATURE_COLS_P2
    available_cols = [c for c in feature_cols if c in df.columns]
    X = df[available_cols].fillna(0)
    y = df["target"]

    return X, y


def train_model(X: pd.DataFrame, y: pd.Series, training_phase: int = 1) -> tuple:
    """
    Train DecisionTreeClassifier with time-series split (no shuffle).
    Returns (trained_model, metrics_dict).
    """
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model = DecisionTreeClassifier(
        max_depth=6,
        min_samples_split=15,
        min_samples_leaf=8,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    report = classification_report(y_test, y_pred, target_names=["SHORT", "HOLD", "LONG"], output_dict=True)

    feature_importances = dict(zip(X.columns, model.feature_importances_.tolist()))

    metrics = {
        "accuracy": acc,
        "classification_report": report,
        "feature_importances": feature_importances,
        "precision_long": report.get("LONG", {}).get("precision", 0.0),
        "precision_short": report.get("SHORT", {}).get("precision", 0.0),
        "training_samples": len(X_train),
        "test_samples": len(X_test),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_phase": training_phase,
    }

    log.info(f"Model trained: accuracy={acc:.3f}, samples={len(X_train)}, phase={training_phase}")
    return model, metrics


def save_model(model, metrics: dict, engine=None) -> None:
    """Save model to disk and metrics to DB."""
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    log.info(f"Model saved to {MODEL_PATH}")

    if engine is not None:
        import json
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO model_performance (
                        trained_at, accuracy, precision_long, precision_short,
                        feature_importances, training_samples, model_path,
                        training_phase, sentiment_rows_available
                    ) VALUES (
                        :trained_at, :accuracy, :precision_long, :precision_short,
                        :feature_importances, :training_samples, :model_path,
                        :training_phase, :sentiment_rows_available
                    )
                    ON CONFLICT (trained_at) DO UPDATE SET
                        accuracy                 = EXCLUDED.accuracy,
                        precision_long           = EXCLUDED.precision_long,
                        precision_short          = EXCLUDED.precision_short,
                        feature_importances      = EXCLUDED.feature_importances,
                        training_samples         = EXCLUDED.training_samples,
                        training_phase           = EXCLUDED.training_phase,
                        sentiment_rows_available = EXCLUDED.sentiment_rows_available
                """),
                {
                    "trained_at": metrics["trained_at"],
                    "accuracy": metrics["accuracy"],
                    "precision_long": metrics["precision_long"],
                    "precision_short": metrics["precision_short"],
                    "feature_importances": json.dumps(metrics["feature_importances"]),
                    "training_samples": metrics["training_samples"],
                    "model_path": MODEL_PATH,
                    "training_phase": metrics.get("training_phase", 1),
                    "sentiment_rows_available": metrics.get("sentiment_rows_available"),
                },
            )


def load_model():
    """Load the latest model from disk."""
    if not os.path.exists(MODEL_PATH):
        return None
    return joblib.load(MODEL_PATH)


def predict(features: dict) -> tuple:
    """
    Run prediction on a feature vector dict.
    Returns (direction: int, confidence: float).
    Uses only the feature columns the model was trained on.
    """
    model = load_model()
    if model is None:
        log.warning("No trained model found — returning HOLD")
        return 0, 0.0

    feature_cols = FEATURE_COLS_P1 if config.TRAINING_PHASE == 1 else FEATURE_COLS_P2
    available = [c for c in feature_cols if c in features]
    row = pd.DataFrame([[features.get(c, 0) for c in available]], columns=available)

    direction = int(model.predict(row)[0])
    probas = model.predict_proba(row)[0]
    confidence = float(probas.max())

    return direction, confidence


def retrain_if_needed(engine, training_phase: int = 1) -> bool:
    """
    Retrain if: no model, last training > 7 days ago, or recent accuracy < 0.52.
    Returns True if a retrain was performed.
    Also logs a warning when Phase 2 readiness threshold is reached.
    """
    should_retrain = False

    if not os.path.exists(MODEL_PATH):
        log.info("No model found — triggering initial training")
        should_retrain = True
    else:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT trained_at, accuracy FROM model_performance ORDER BY trained_at DESC LIMIT 1")
            ).fetchone()
        if row is None:
            should_retrain = True
        else:
            trained_at = row[0]
            accuracy = row[1]
            age = datetime.now(timezone.utc) - trained_at.replace(tzinfo=timezone.utc)
            if age > timedelta(days=7):
                log.info(f"Model is {age.days} days old — retraining")
                should_retrain = True
            elif accuracy < 0.52:
                log.info(f"Model accuracy {accuracy:.3f} below threshold — retraining")
                should_retrain = True

    if should_retrain:
        try:
            from data.sentiment_fetcher import get_sentiment_count
            X, y = build_training_dataset(engine, training_phase)
            model, metrics = train_model(X, y, training_phase)
            metrics["sentiment_rows_available"] = get_sentiment_count(engine)
            save_model(model, metrics, engine)
            return True
        except Exception as exc:
            log.error(f"Retraining failed: {exc}")
            return False

    # Phase 2 readiness check
    from data.sentiment_fetcher import get_sentiment_count
    count = get_sentiment_count(engine)
    if config.TRAINING_PHASE == 1 and count >= config.MIN_SENTIMENT_ROWS:
        log.warning(
            f"PHASE 2 READY: {count} sentiment rows. "
            "Set TRAINING_PHASE=2 in .env and restart to upgrade model."
        )

    return False
