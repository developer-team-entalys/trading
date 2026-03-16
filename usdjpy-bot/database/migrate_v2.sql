-- migrate_v2.sql — Incremental migration for running containers
-- Apply with:
--   docker compose exec timescaledb psql -U botuser -d usdjpy_bot \
--     -f /docker-entrypoint-initdb.d/migrate_v2.sql

ALTER TABLE candles ADD COLUMN IF NOT EXISTS near_round BOOLEAN;

ALTER TABLE sentiment_data ADD COLUMN IF NOT EXISTS long_positions INTEGER;
ALTER TABLE sentiment_data ADD COLUMN IF NOT EXISTS short_positions INTEGER;
ALTER TABLE sentiment_data ADD COLUMN IF NOT EXISTS avg_long_price DOUBLE PRECISION;
ALTER TABLE sentiment_data ADD COLUMN IF NOT EXISTS avg_short_price DOUBLE PRECISION;

ALTER TABLE signals ADD COLUMN IF NOT EXISTS training_phase SMALLINT;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS training_phase SMALLINT;

ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS training_phase SMALLINT;
ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS sentiment_rows_available INTEGER;

-- ── VIX data table (new in v4) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vix_data (
    date        DATE             NOT NULL PRIMARY KEY,
    vix_close   DOUBLE PRECISION NOT NULL,
    vix_regime  SMALLINT         NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vix_date ON vix_data (date DESC);

-- ── Nikkei 225 data table (new in v5) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS nikkei_data (
    date           DATE             NOT NULL PRIMARY KEY,
    nikkei_close   DOUBLE PRECISION NOT NULL,
    nikkei_regime  SMALLINT         NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nikkei_date ON nikkei_data (date DESC);

-- ── DOM Snapshots table (new in v3) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dom_snapshots (
    time             TIMESTAMPTZ      NOT NULL,
    symbol           TEXT             NOT NULL DEFAULT 'USDJPY',
    best_bid         DOUBLE PRECISION,
    best_ask         DOUBLE PRECISION,
    spread_pips      DOUBLE PRECISION,
    bid_depth_total  DOUBLE PRECISION,
    ask_depth_total  DOUBLE PRECISION,
    order_imbalance  DOUBLE PRECISION,
    levels_count     INTEGER,
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('dom_snapshots', 'time',
    if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day');
CREATE INDEX IF NOT EXISTS idx_dom_time ON dom_snapshots (time DESC);
