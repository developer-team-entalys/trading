-- TimescaleDB schema for USD/JPY trading bot

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────────
-- 1. Candles (OHLCV + pre-computed indicators)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS candles (
    time                TIMESTAMPTZ     NOT NULL,
    open                DOUBLE PRECISION,
    high                DOUBLE PRECISION,
    low                 DOUBLE PRECISION,
    close               DOUBLE PRECISION,
    volume              INTEGER,
    atr_1h              DOUBLE PRECISION,
    atr_4h              DOUBLE PRECISION,
    rsi_14              DOUBLE PRECISION,
    session             VARCHAR(10),        -- 'tokyo','london','new_york','overlap'
    distance_to_round   DOUBLE PRECISION,   -- pips to nearest 0.50 round number
    near_round          BOOLEAN             -- true if distance_to_round <= 20 pips
);

SELECT create_hypertable('candles', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_candles_time   ON candles (time DESC);

-- ─────────────────────────────────────────────────────────────
-- 2. COT (Commitment of Traders) data — weekly
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cot_data (
    week_date           DATE            NOT NULL PRIMARY KEY,
    noncomm_long        BIGINT,
    noncomm_short       BIGINT,
    noncomm_net         BIGINT,         -- calculated: long - short
    comm_long           BIGINT,
    comm_short          BIGINT,
    comm_net            BIGINT,
    noncomm_net_change  BIGINT,         -- week-over-week change
    noncomm_net_pct     DOUBLE PRECISION, -- net as % of open interest
    open_interest       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_cot_week_date ON cot_data (week_date DESC);

-- ─────────────────────────────────────────────────────────────
-- 3. Sentiment snapshots
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentiment_data (
    time            TIMESTAMPTZ     NOT NULL,
    long_pct        DOUBLE PRECISION,
    short_pct       DOUBLE PRECISION,
    long_positions  INTEGER,
    short_positions INTEGER,
    avg_long_price  DOUBLE PRECISION,
    avg_short_price DOUBLE PRECISION,
    source          VARCHAR(20)     -- 'myfxbook'
);

SELECT create_hypertable('sentiment_data', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_sentiment_time ON sentiment_data (time DESC);

-- ─────────────────────────────────────────────────────────────
-- 4. Signals
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    time                TIMESTAMPTZ     NOT NULL,
    direction           SMALLINT,       -- 1=long, -1=short, 0=hold
    confidence          DOUBLE PRECISION,
    cot_signal          SMALLINT,
    sentiment_signal    SMALLINT,
    technical_signal    SMALLINT,
    rollover_signal     SMALLINT,
    atr_1h              DOUBLE PRECISION,
    training_phase      SMALLINT,
    features            JSONB
);

SELECT create_hypertable('signals', 'time',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_signals_time      ON signals (time DESC);
CREATE INDEX IF NOT EXISTS idx_signals_direction ON signals (direction);

-- ─────────────────────────────────────────────────────────────
-- 5. Trades
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                  SERIAL          PRIMARY KEY,
    opened_at           TIMESTAMPTZ     NOT NULL,
    closed_at           TIMESTAMPTZ,
    direction           SMALLINT,       -- 1=long, -1=short
    lots                DOUBLE PRECISION,
    entry_price         DOUBLE PRECISION,
    exit_price          DOUBLE PRECISION,
    stop_loss           DOUBLE PRECISION,
    take_profit         DOUBLE PRECISION,
    pnl_eur             DOUBLE PRECISION,
    pnl_pips            DOUBLE PRECISION,
    close_reason        VARCHAR(20),    -- 'sl','tp','signal_reverse','manual'
    signal_confidence   DOUBLE PRECISION,
    training_phase      SMALLINT,
    ctrader_order_id    BIGINT
);

CREATE INDEX IF NOT EXISTS idx_trades_opened_at  ON trades (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_direction  ON trades (direction);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at  ON trades (closed_at DESC);

-- ─────────────────────────────────────────────────────────────
-- 6. Model performance
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_performance (
    trained_at                  TIMESTAMPTZ     NOT NULL PRIMARY KEY,
    accuracy                    DOUBLE PRECISION,
    precision_long              DOUBLE PRECISION,
    precision_short             DOUBLE PRECISION,
    feature_importances         JSONB,
    training_samples            INTEGER,
    model_path                  VARCHAR(255),
    training_phase              SMALLINT,
    sentiment_rows_available    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_model_trained_at ON model_performance (trained_at DESC);
