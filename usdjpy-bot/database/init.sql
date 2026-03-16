-- ── Extensions ─────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── 1. Candles (30min OHLCV + indicators) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS candles (
    time                TIMESTAMPTZ     NOT NULL,
    open                DOUBLE PRECISION NOT NULL,
    high                DOUBLE PRECISION NOT NULL,
    low                 DOUBLE PRECISION NOT NULL,
    close               DOUBLE PRECISION NOT NULL,
    volume              INTEGER          DEFAULT 0,
    atr_1h              DOUBLE PRECISION,   -- 14-period ATR on native timeframe bars (pips)
    atr_4h              DOUBLE PRECISION,   -- 14-period ATR on 4H bars (pips)
    rsi_14              DOUBLE PRECISION,   -- 14-period RSI (0-100)
    session             VARCHAR(10),        -- 'tokyo','london','new_york','overlap'
    distance_to_round   DOUBLE PRECISION,   -- pips to nearest 0.50 level
    near_round          BOOLEAN DEFAULT FALSE,
    UNIQUE (time)
);
SELECT create_hypertable('candles', 'time', chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candles_time ON candles (time DESC);

-- ── 2. COT Report (weekly CFTC data) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS cot_data (
    week_date               DATE            NOT NULL PRIMARY KEY,
    noncomm_long            BIGINT,
    noncomm_short           BIGINT,
    noncomm_net             BIGINT,         -- noncomm_long - noncomm_short
    comm_long               BIGINT,
    comm_short              BIGINT,
    comm_net                BIGINT,
    noncomm_net_change      BIGINT,         -- week-over-week change
    noncomm_net_pct         DOUBLE PRECISION, -- net as % of open interest
    open_interest           BIGINT
);
CREATE INDEX IF NOT EXISTS idx_cot_week ON cot_data (week_date DESC);

-- ── 3. Myfxbook Sentiment (every 30min) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentiment_data (
    time                TIMESTAMPTZ     NOT NULL,
    long_pct            DOUBLE PRECISION NOT NULL,  -- % retail traders long
    short_pct           DOUBLE PRECISION NOT NULL,  -- % retail traders short
    long_positions      INTEGER          DEFAULT 0,
    short_positions     INTEGER          DEFAULT 0,
    avg_long_price      DOUBLE PRECISION,           -- crowd average long entry
    avg_short_price     DOUBLE PRECISION,           -- crowd average short entry
    source              VARCHAR(20)      DEFAULT 'myfxbook',
    UNIQUE (time)
);
SELECT create_hypertable('sentiment_data', 'time', chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_sentiment_time ON sentiment_data (time DESC);

-- ── 4. Rollover & Interest Rate Data ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS rollover_data (
    date                DATE            NOT NULL PRIMARY KEY,

    -- Raw swap points from cTrader (IC Markets) for USDJPY
    -- Unit: points per lot per night (1 point = 0.001 JPY)
    swap_long_pts       DOUBLE PRECISION,   -- positive = we EARN holding Long
    swap_short_pts      DOUBLE PRECISION,   -- negative = we PAY holding Short

    -- Calculated EUR cost per 0.1 lot (our max position size)
    -- Formula: swap_pts * lots * pip_value / 10
    -- For USDJPY: pip_value approx 1000 EUR per lot at ~150 rate
    swap_long_eur       DOUBLE PRECISION,   -- positive = profit per night
    swap_short_eur      DOUBLE PRECISION,   -- negative = cost per night

    -- Wednesday rollover multiplier (broker charges 3x on Wednesday
    -- to cover the weekend when markets are closed)
    triple_swap_day     VARCHAR(10)         DEFAULT 'WEDNESDAY',

    -- Annualized swap yield (swap_long_pts / 10 * 365 / current_price)
    -- Useful for comparing carry attractiveness over time
    carry_yield_annual_pct DOUBLE PRECISION,

    -- Central bank interest rates from FRED (monthly, forward-filled daily)
    fed_rate_pct        DOUBLE PRECISION,   -- Federal Funds Rate (e.g. 4.33)
    boj_rate_pct        DOUBLE PRECISION,   -- BOJ Policy Rate (e.g. 0.50)
    rate_differential   DOUBLE PRECISION,   -- fed_rate - boj_rate (e.g. 3.83)

    -- Derived signal for Decision Tree
    -- 1  = carry strongly favors Long (differential > 1.0%)
    -- 0  = carry neutral (differential between -1.0% and 1.0%)
    -- -1 = carry favors Short (differential < -1.0%)
    rollover_direction  SMALLINT            DEFAULT 1,

    -- Normalized carry strength (0.0 to 1.0)
    -- carry_strength = min(abs(rate_differential) / 5.0, 1.0)
    -- At 5% differential -> full strength 1.0
    -- At 1% differential -> strength 0.2
    carry_strength      DOUBLE PRECISION    DEFAULT 0.0,

    -- Data provenance
    swap_source         VARCHAR(20)         DEFAULT 'ctrader',
    rate_source         VARCHAR(20)         DEFAULT 'fred',
    updated_at          TIMESTAMPTZ         DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rollover_date ON rollover_data (date DESC);

-- ── 5. Interest Rate History ────────────────────────────────────────────────
-- Full historical record of Fed and BOJ rates from FRED
-- Used for training the Decision Tree on historical carry signals
CREATE TABLE IF NOT EXISTS interest_rates (
    date            DATE            NOT NULL,
    series          VARCHAR(30)     NOT NULL,   -- 'FEDFUNDS' or 'IRSTCI01JPM156N'
    rate_pct        DOUBLE PRECISION NOT NULL,
    country         VARCHAR(5)      NOT NULL,   -- 'US' or 'JP'
    PRIMARY KEY (date, series)
);
CREATE INDEX IF NOT EXISTS idx_rates_date ON interest_rates (date DESC);
CREATE INDEX IF NOT EXISTS idx_rates_series ON interest_rates (series, date DESC);

-- ── 6. VIX — CBOE Volatility Index (FRED VIXCLS) ───────────────────────────
CREATE TABLE IF NOT EXISTS vix_data (
    date        DATE             NOT NULL PRIMARY KEY,
    vix_close   DOUBLE PRECISION NOT NULL,  -- VIXCLS daily closing value
    vix_regime  SMALLINT         NOT NULL DEFAULT 0
    -- -1 = calm (<15, risk-on, JPY weakens)
    --  0 = normal (15–25)
    --  1 = fearful (>25, risk-off, JPY strengthens)
);
CREATE INDEX IF NOT EXISTS idx_vix_date ON vix_data (date DESC);

-- ── 6b. Nikkei 225 — Japanese equity index (Yahoo Finance ^N225) ─────────────
CREATE TABLE IF NOT EXISTS nikkei_data (
    date           DATE             NOT NULL PRIMARY KEY,
    nikkei_close   DOUBLE PRECISION NOT NULL,  -- ^N225 daily close
    nikkei_regime  SMALLINT         NOT NULL DEFAULT 0
    -- -1 = down day (<-0.3%, risk-off, JPY strengthens)
    --  0 = flat (±0.3%)
    --  1 = up day (>+0.3%, risk-on, JPY weakens)
);
CREATE INDEX IF NOT EXISTS idx_nikkei_date ON nikkei_data (date DESC);

-- ── 7. Signals ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    time                TIMESTAMPTZ     NOT NULL,
    direction           SMALLINT,               -- 1=long, -1=short, 0=hold
    confidence          DOUBLE PRECISION,
    training_phase      SMALLINT        DEFAULT 1,
    cot_signal          SMALLINT,
    technical_signal    SMALLINT,
    sentiment_signal    SMALLINT,               -- NULL in phase 1
    rollover_signal     SMALLINT,
    carry_strength      DOUBLE PRECISION,       -- carry signal strength
    rate_differential   DOUBLE PRECISION,       -- Fed-BOJ differential
    atr_1h              DOUBLE PRECISION,
    blackout            BOOLEAN         DEFAULT FALSE,
    blackout_event      VARCHAR(100),
    features            JSONB
);
SELECT create_hypertable('signals', 'time', chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (time DESC);

-- ── 7. Trades ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                      SERIAL PRIMARY KEY,
    opened_at               TIMESTAMPTZ     NOT NULL,
    closed_at               TIMESTAMPTZ,
    direction               SMALLINT,
    lots                    DOUBLE PRECISION,
    entry_price             DOUBLE PRECISION,
    exit_price              DOUBLE PRECISION,
    stop_loss               DOUBLE PRECISION,
    take_profit             DOUBLE PRECISION,
    pnl_eur                 DOUBLE PRECISION,
    pnl_pips                DOUBLE PRECISION,
    swap_earned_eur         DOUBLE PRECISION    DEFAULT 0,  -- rollover collected
    nights_held             INTEGER             DEFAULT 0,  -- nights position open
    close_reason            VARCHAR(20),        -- 'sl','tp','signal_reverse','manual'
    signal_confidence       DOUBLE PRECISION,
    training_phase          SMALLINT,
    rate_differential_at_open DOUBLE PRECISION, -- snapshot of carry at open
    swap_long_at_open       DOUBLE PRECISION,   -- swap long rate when trade opened
    swap_short_at_open      DOUBLE PRECISION,   -- swap short rate when trade opened
    ctrader_order_id        BIGINT
);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades (closed_at DESC);

-- ── 8. Model Performance ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_performance (
    trained_at                  TIMESTAMPTZ     NOT NULL PRIMARY KEY,
    training_phase              SMALLINT,
    accuracy                    DOUBLE PRECISION,
    precision_long              DOUBLE PRECISION,
    precision_short             DOUBLE PRECISION,
    feature_importances         JSONB,
    training_samples            INTEGER,
    sentiment_rows_available    INTEGER,
    model_path                  VARCHAR(255)
);

-- ── 9. DOM Snapshots (Depth of Market microstructure) ──────────────────────
CREATE TABLE IF NOT EXISTS dom_snapshots (
    time             TIMESTAMPTZ      NOT NULL,
    symbol           TEXT             NOT NULL DEFAULT 'USDJPY',
    best_bid         DOUBLE PRECISION,
    best_ask         DOUBLE PRECISION,
    spread_pips      DOUBLE PRECISION,
    bid_depth_total  DOUBLE PRECISION,   -- total volume in top-10 bid levels
    ask_depth_total  DOUBLE PRECISION,   -- total volume in top-10 ask levels
    order_imbalance  DOUBLE PRECISION,   -- bid_depth / (bid+ask depth), 0.5 = balanced
    levels_count     INTEGER,            -- total quotes in book at snapshot time
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('dom_snapshots', 'time',
    if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day');
CREATE INDEX IF NOT EXISTS idx_dom_time ON dom_snapshots (time DESC);

-- ── 10. News Events (from Finnhub) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_events (
    id              SERIAL PRIMARY KEY,
    event_time      TIMESTAMPTZ     NOT NULL,
    event_name      VARCHAR(100)    NOT NULL,
    country         VARCHAR(5)      NOT NULL,
    impact          VARCHAR(10)     DEFAULT 'high',
    blackout_start  TIMESTAMPTZ,
    blackout_end    TIMESTAMPTZ,
    signal_skipped  BOOLEAN         DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_events (event_time DESC);
