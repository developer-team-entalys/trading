-- inspect_db.sql — Quick inspection of all tables in the usdjpy_bot database
-- Run: docker compose exec timescaledb psql -U botuser -d usdjpy_bot -f /tmp/inspect_db.sql

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  TABLE OVERVIEW (row counts)'
\echo '════════════════════════════════════════════════════════════'
SELECT
    relname                          AS table_name,
    n_live_tup                       AS row_count,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
ORDER BY relname;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — candles'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'candles'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 5 candles'
SELECT time, open, high, low, close, atr_1h, rsi_14, session, distance_to_round, near_round
FROM candles ORDER BY time DESC LIMIT 5;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — cot_data'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'cot_data'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 5 cot_data rows'
SELECT week_date, noncomm_long, noncomm_short, noncomm_net, noncomm_net_pct, open_interest
FROM cot_data ORDER BY week_date DESC LIMIT 5;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — sentiment_data'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'sentiment_data'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 5 sentiment_data rows'
SELECT time, long_pct, short_pct, long_positions, short_positions,
       avg_long_price, avg_short_price, source
FROM sentiment_data ORDER BY time DESC LIMIT 5;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — signals'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'signals'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 5 signals'
SELECT time, direction, confidence, cot_signal, sentiment_signal,
       technical_signal, atr_1h, training_phase
FROM signals ORDER BY time DESC LIMIT 5;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — trades'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'trades'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 5 trades'
SELECT id, opened_at, closed_at, direction, lots, entry_price, exit_price,
       pnl_pips, close_reason, training_phase
FROM trades ORDER BY opened_at DESC LIMIT 5;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  COLUMNS — model_performance'
\echo '════════════════════════════════════════════════════════════'
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'model_performance'
ORDER BY ordinal_position;

\echo ''
\echo '  LATEST 3 model_performance rows'
SELECT trained_at, accuracy, precision_long, precision_short,
       training_samples, training_phase, sentiment_rows_available, model_path
FROM model_performance ORDER BY trained_at DESC LIMIT 3;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  SUMMARY'
\echo '════════════════════════════════════════════════════════════'
SELECT
    (SELECT COUNT(*) FROM candles)           AS candle_rows,
    (SELECT COUNT(*) FROM cot_data)          AS cot_rows,
    (SELECT COUNT(*) FROM sentiment_data)    AS sentiment_rows,
    (SELECT COUNT(*) FROM signals)           AS signal_rows,
    (SELECT COUNT(*) FROM trades)            AS trade_rows,
    (SELECT COUNT(*) FROM model_performance) AS model_rows;

\echo ''
\echo '  Time range of candles'
SELECT MIN(time) AS oldest, MAX(time) AS newest,
       COUNT(*) AS total_bars,
       ROUND(EXTRACT(EPOCH FROM (MAX(time) - MIN(time))) / 60 / 30) AS approx_m30_bars
FROM candles;
