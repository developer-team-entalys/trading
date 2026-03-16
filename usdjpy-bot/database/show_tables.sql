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
\echo '  rollover_data'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM rollover_data ORDER BY date DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  interest_rates'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM interest_rates ORDER BY date DESC, series LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  vix_data'
\echo '════════════════════════════════════════════════════════════'
SELECT *,
    CASE vix_regime WHEN -1 THEN 'CALM' WHEN 0 THEN 'NORMAL' WHEN 1 THEN 'FEARFUL' END AS regime_label
FROM vix_data ORDER BY date DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  nikkei_data'
\echo '════════════════════════════════════════════════════════════'
SELECT *,
    CASE nikkei_regime WHEN -1 THEN 'DOWN' WHEN 0 THEN 'FLAT' WHEN 1 THEN 'UP' END AS regime_label
FROM nikkei_data ORDER BY date DESC LIMIT 20;

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
\echo '  dom_snapshots'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM dom_snapshots ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  news_events'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM news_events ORDER BY event_time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  dom_raw  (1-min raw depth snapshots)'
\echo '════════════════════════════════════════════════════════════'
SELECT time, symbol,
       bid1_price, bid1_volume, ask1_price, ask1_volume,
       bid2_price, bid2_volume, ask2_price, ask2_volume,
       spread_pips
FROM dom_raw ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  candles_5m  (5-min OHLCV)'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM candles_5m ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  tick_volume_1m  (1-min tick aggregates)'
\echo '════════════════════════════════════════════════════════════'
SELECT * FROM tick_volume_1m ORDER BY time DESC LIMIT 20;

\echo ''
\echo '════════════════════════════════════════════════════════════'
\echo '  ROW COUNTS'
\echo '════════════════════════════════════════════════════════════'
SELECT
    (SELECT COUNT(*) FROM candles)           AS candles,
    (SELECT COUNT(*) FROM candles_5m)        AS candles_5m,
    (SELECT COUNT(*) FROM cot_data)          AS cot_data,
    (SELECT COUNT(*) FROM sentiment_data)    AS sentiment_data,
    (SELECT COUNT(*) FROM rollover_data)     AS rollover_data,
    (SELECT COUNT(*) FROM interest_rates)    AS interest_rates,
    (SELECT COUNT(*) FROM vix_data)          AS vix_data;
SELECT
    (SELECT COUNT(*) FROM nikkei_data)       AS nikkei_data,
    (SELECT COUNT(*) FROM signals)           AS signals,
    (SELECT COUNT(*) FROM trades)            AS trades,
    (SELECT COUNT(*) FROM model_performance) AS model_performance,
    (SELECT COUNT(*) FROM dom_snapshots)     AS dom_snapshots,
    (SELECT COUNT(*) FROM dom_raw)           AS dom_raw,
    (SELECT COUNT(*) FROM tick_volume_1m)    AS tick_volume_1m,
    (SELECT COUNT(*) FROM news_events)       AS news_events;
