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
