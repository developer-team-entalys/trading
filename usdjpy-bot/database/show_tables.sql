\echo '════════════════════════════════════════════════════════════'
\echo '  candles'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM candles ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  cot_data'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM cot_data ORDER BY week_date DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  sentiment_data'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM sentiment_data ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  signals'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM signals ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  trades'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM trades ORDER BY opened_at DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  model_performance'
\echo '════════════════════════════════════════════════════════════'
SELECT trained_at, accuracy, precision_long, precision_short,
       training_samples, training_phase, sentiment_rows_available, model_path
FROM model_performance ORDER BY trained_at DESC LIMIT 10;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  ROW COUNTS'
\echo '════════════════════════════════════════════════════════════'
SELECT
    (SELECT COUNT(*) FROM candles)           AS candles,
    (SELECT COUNT(*) FROM cot_data)          AS cot_data,
    (SELECT COUNT(*) FROM sentiment_data)    AS sentiment_data,
    (SELECT COUNT(*) FROM signals)           AS signals,
    (SELECT COUNT(*) FROM trades)            AS trades,
    (SELECT COUNT(*) FROM model_performance) AS model_performance;
