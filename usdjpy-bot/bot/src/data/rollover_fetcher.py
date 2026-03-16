"""
Dynamic Rollover & Interest Rate Data

Two data sources:

SOURCE 1 — cTrader Open API (swap points)
  The broker's actual swap rates for USDJPY, live from IC Markets.
  Retrieved via ProtoOAGetSymbolsReq when the bot connects.
  Fields: swapLong, swapShort, swapRollover3Days

  Swap point calculation to EUR:
    swap_eur = swap_pts * lots * contract_size * pip_value / current_price
    For USDJPY 0.1 lots at price 150.00:
      pip_value per lot = 1000 EUR (approx, depends on EUR/USD rate)
      swap_long_eur = swap_long_pts * 0.1 * 100000 / (150.00 * 100) / 100
    Use the simplified formula: swap_eur = swap_pts * lots * 6.67
    (approximation valid when USD/JPY is between 140 and 160)

SOURCE 2 — FRED API (central bank rates, historical)
  Federal Reserve Economic Data — free, 120 requests/minute.
  API key required (free registration at fred.stlouisfed.org).

  Series used:
    FEDFUNDS          : Federal Funds Effective Rate (monthly, since 1954)
    IRSTCI01JPM156N   : BOJ Immediate Rates (monthly, since 1985)

  Both return monthly data. We forward-fill to daily frequency.
  Historical data available from 1985 -> aligns with COT data (2010+).
"""

import structlog
from datetime import datetime, timezone, date, timedelta
from typing import Optional
import pandas as pd

logger = structlog.get_logger()


# ── FRED Functions ──────────────────────────────────────────────────────────

def download_interest_rate_history(fred_api_key: str) -> pd.DataFrame:
    """
    Download full history of Fed and BOJ policy rates from FRED.

    Returns DataFrame with columns:
      date (DATE), series (str), rate_pct (float), country (str)

    Fed data available from 1954, BOJ from 1985.
    We request from 1985 to align with available BOJ data.

    Forward-fills monthly data to daily frequency so we can
    join it with daily candle data for training.

    Returns empty DataFrame if FRED API key not set or request fails.
    Never raises exceptions.
    """
    if not fred_api_key:
        logger.warning("rollover_fetcher.no_fred_key")
        return pd.DataFrame()

    try:
        from fredapi import Fred
        fred = Fred(api_key=fred_api_key)

        start_date = "1985-01-01"
        rows = []

        # Federal Funds Rate
        fed_series = fred.get_series('FEDFUNDS', observation_start=start_date)
        for dt, rate in fed_series.items():
            if pd.notna(rate):
                rows.append({
                    'date':     dt.date(),
                    'series':   'FEDFUNDS',
                    'rate_pct': float(rate),
                    'country':  'US'
                })

        # BOJ Policy Rate
        boj_series = fred.get_series('IRSTCI01JPM156N', observation_start=start_date)
        for dt, rate in boj_series.items():
            if pd.notna(rate):
                rows.append({
                    'date':     dt.date(),
                    'series':   'IRSTCI01JPM156N',
                    'rate_pct': float(rate),
                    'country':  'JP'
                })

        df = pd.DataFrame(rows)
        logger.info("rollover_fetcher.fred_downloaded",
                    fed_rows=len(df[df['series'] == 'FEDFUNDS']),
                    boj_rows=len(df[df['series'] == 'IRSTCI01JPM156N']))
        return df

    except Exception as e:
        logger.error("rollover_fetcher.fred_error", error=str(e))
        return pd.DataFrame()


def save_interest_rates_to_db(df: pd.DataFrame, engine) -> int:
    """
    Upsert interest rate history into interest_rates table.
    Returns number of rows inserted.
    """
    if df.empty:
        return 0

    from sqlalchemy import text
    rows_inserted = 0

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(text("""
                INSERT INTO interest_rates (date, series, rate_pct, country)
                VALUES (:date, :series, :rate_pct, :country)
                ON CONFLICT (date, series) DO UPDATE
                  SET rate_pct = EXCLUDED.rate_pct
            """), {
                'date':     row['date'],
                'series':   row['series'],
                'rate_pct': row['rate_pct'],
                'country':  row['country']
            })
            rows_inserted += 1

    logger.info("rollover_fetcher.rates_saved", rows=rows_inserted)
    return rows_inserted


def get_latest_rates(engine) -> dict:
    """
    Query the most recent Fed and BOJ rates from interest_rates table.

    Returns:
      {
        'fed_rate_pct':       float,   # e.g. 4.33
        'boj_rate_pct':       float,   # e.g. 0.50
        'rate_differential':  float,   # fed - boj, e.g. 3.83
        'fed_date':           date,
        'boj_date':           date,
      }
    Returns dict with zeros if no data available.
    """
    from sqlalchemy import text

    result = {
        'fed_rate_pct':      0.0,
        'boj_rate_pct':      0.0,
        'rate_differential': 0.0,
        'fed_date':          None,
        'boj_date':          None,
    }

    try:
        with engine.connect() as conn:
            # Latest Fed rate
            fed_row = conn.execute(text("""
                SELECT date, rate_pct FROM interest_rates
                WHERE series = 'FEDFUNDS'
                ORDER BY date DESC LIMIT 1
            """)).fetchone()

            # Latest BOJ rate
            boj_row = conn.execute(text("""
                SELECT date, rate_pct FROM interest_rates
                WHERE series = 'IRSTCI01JPM156N'
                ORDER BY date DESC LIMIT 1
            """)).fetchone()

        if fed_row:
            result['fed_rate_pct'] = float(fed_row[1])
            result['fed_date']     = fed_row[0]
        if boj_row:
            result['boj_rate_pct'] = float(boj_row[1])
            result['boj_date']     = boj_row[0]

        result['rate_differential'] = result['fed_rate_pct'] - result['boj_rate_pct']

    except Exception as e:
        logger.error("rollover_fetcher.get_rates_error", error=str(e))

    return result


# ── cTrader Swap Functions ──────────────────────────────────────────────────

def fetch_swap_rates_from_ctrader(client, symbol_id: int) -> dict:
    """
    Fetch current swap rates for USDJPY from cTrader Open API.

    Uses ProtoOAGetSymbolsReq to retrieve full symbol details.
    Returns swap points per lot per night.

    Returns:
      {
        'swap_long_pts':    float,  # e.g. +0.38 (positive = we earn)
        'swap_short_pts':   float,  # e.g. -2.14 (negative = we pay)
        'triple_swap_day':  str,    # 'WEDNESDAY' (when 3x swap is charged)
        'source':           'ctrader'
      }
    Returns dict with zeros if request fails.
    """
    try:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolByIdReq
        )

        request = ProtoOASymbolByIdReq()
        request.ctidTraderAccountId = client.account_id
        request.symbolId.append(symbol_id)

        # Send request and await response
        # (Implementation depends on async client pattern used in ctrader_client.py)
        response = client.send_request(request)

        for symbol in response.symbol:
            if symbol.symbolId == symbol_id:
                triple_day = symbol.swapRollover3Days
                # Convert enum to string
                day_names = {0: 'MONDAY', 1: 'TUESDAY', 2: 'WEDNESDAY',
                             3: 'THURSDAY', 4: 'FRIDAY'}
                triple_str = day_names.get(triple_day, 'WEDNESDAY')

                return {
                    'swap_long_pts':  float(symbol.swapLong),
                    'swap_short_pts': float(symbol.swapShort),
                    'triple_swap_day': triple_str,
                    'source': 'ctrader'
                }

    except Exception as e:
        logger.error("rollover_fetcher.ctrader_swap_error", error=str(e))

    return {
        'swap_long_pts':   0.0,
        'swap_short_pts':  0.0,
        'triple_swap_day': 'WEDNESDAY',
        'source':          'ctrader_error'
    }


# ── Rollover Signal Computation ─────────────────────────────────────────────

def compute_rollover_signal(
    swap_long_pts:   float,
    swap_short_pts:  float,
    fed_rate_pct:    float,
    boj_rate_pct:    float,
    lots:            float = 0.1,
    usdjpy_price:    float = 150.0
) -> dict:
    """
    Compute the full rollover signal for the Decision Tree.

    Swap EUR calculation (simplified, valid for 140-160 price range):
      swap_eur = swap_pts * lots * 6.67
    This approximation holds because:
      pip_value_per_lot approx 667 EUR at 150.00 USDJPY
      swap_eur = swap_pts * 0.01 * lots * pip_value approx swap_pts * lots * 6.67

    Rate differential:
      positive -> USD higher rate -> Long USD/JPY earns carry
      negative -> JPY higher rate -> Short USD/JPY earns carry (rare)

    Rollover direction:
      1  = carry strongly favors Long  (swap_long_pts > 0)
      0  = carry neutral               (-0.2 < swap_long_pts <= 0)
      -1 = carry favors Short          (swap_long_pts <= -0.2)

    Carry strength (0.0 to 1.0):
      Based on rate differential, normalized at 5% = full strength
      Also accounts for actual swap points as sanity check

    Returns dict with all computed values for DB storage and feature vector.
    """
    rate_differential = fed_rate_pct - boj_rate_pct

    # EUR cost/profit per night for our standard 0.1 lot position
    swap_long_eur  = swap_long_pts  * lots * 6.67
    swap_short_eur = swap_short_pts * lots * 6.67

    # Annualized yield from carry (Long position)
    # Positive = we earn X% per year just from holding Long
    if usdjpy_price > 0 and swap_long_pts != 0:
        carry_yield_annual_pct = (swap_long_pts / 10) * 365 / usdjpy_price * 100
    else:
        carry_yield_annual_pct = 0.0

    # Direction signal
    if swap_long_pts > 0.05:
        direction = 1     # Long earns carry — reinforce Long bias
    elif swap_long_pts <= -0.2:
        direction = -1    # Long is expensive — reduce Long bias
    else:
        direction = 0     # Neutral — carry not significant

    # Carry strength from rate differential (0.0 -> 1.0)
    carry_strength = min(abs(rate_differential) / 5.0, 1.0)

    # If swap points contradict the rate differential (broker anomaly),
    # use the lower of the two strengths as a conservative estimate
    swap_implied_strength = min(abs(swap_long_pts) / 2.0, 1.0)
    if swap_long_pts > 0:
        carry_strength = min(carry_strength, swap_implied_strength)

    return {
        # Raw data
        'swap_long_pts':         swap_long_pts,
        'swap_short_pts':        swap_short_pts,
        'swap_long_eur':         round(swap_long_eur, 4),
        'swap_short_eur':        round(swap_short_eur, 4),
        'carry_yield_annual_pct': round(carry_yield_annual_pct, 4),
        'fed_rate_pct':          fed_rate_pct,
        'boj_rate_pct':          boj_rate_pct,
        'rate_differential':     round(rate_differential, 4),
        # Decision Tree features
        'rollover_direction':    direction,
        'carry_strength':        round(carry_strength, 4),
        # Metadata
        'triple_swap_day':       'WEDNESDAY',
        'swap_source':           'ctrader',
        'rate_source':           'fred',
    }


def save_rollover_to_db(data: dict, engine) -> None:
    """
    Upsert today's rollover data into rollover_data table.
    Called daily at startup and after each cTrader reconnect.
    """
    from sqlalchemy import text

    today = date.today()

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO rollover_data (
                date, swap_long_pts, swap_short_pts,
                swap_long_eur, swap_short_eur,
                carry_yield_annual_pct,
                fed_rate_pct, boj_rate_pct, rate_differential,
                rollover_direction, carry_strength,
                triple_swap_day, swap_source, rate_source, updated_at
            ) VALUES (
                :date, :swap_long_pts, :swap_short_pts,
                :swap_long_eur, :swap_short_eur,
                :carry_yield_annual_pct,
                :fed_rate_pct, :boj_rate_pct, :rate_differential,
                :rollover_direction, :carry_strength,
                :triple_swap_day, :swap_source, :rate_source, NOW()
            )
            ON CONFLICT (date) DO UPDATE SET
                swap_long_pts         = EXCLUDED.swap_long_pts,
                swap_short_pts        = EXCLUDED.swap_short_pts,
                swap_long_eur         = EXCLUDED.swap_long_eur,
                swap_short_eur        = EXCLUDED.swap_short_eur,
                carry_yield_annual_pct = EXCLUDED.carry_yield_annual_pct,
                fed_rate_pct          = EXCLUDED.fed_rate_pct,
                boj_rate_pct          = EXCLUDED.boj_rate_pct,
                rate_differential     = EXCLUDED.rate_differential,
                rollover_direction    = EXCLUDED.rollover_direction,
                carry_strength        = EXCLUDED.carry_strength,
                updated_at            = NOW()
        """), {**data, 'date': today})

    logger.info("rollover_fetcher.saved",
                date=str(today),
                swap_long=data['swap_long_pts'],
                rate_diff=data['rate_differential'],
                direction=data['rollover_direction'],
                strength=data['carry_strength'])


def get_today_rollover(engine) -> dict:
    """
    Get today's rollover data from DB.
    Falls back to yesterday if today's data not yet written.
    Returns neutral defaults if no data at all.
    """
    from sqlalchemy import text

    defaults = {
        'swap_long_pts':      0.0,
        'swap_short_pts':     0.0,
        'swap_long_eur':      0.0,
        'swap_short_eur':     0.0,
        'fed_rate_pct':       0.0,
        'boj_rate_pct':       0.0,
        'rate_differential':  0.0,
        'rollover_direction': 0,
        'carry_strength':     0.0,
    }

    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT swap_long_pts, swap_short_pts,
                       swap_long_eur, swap_short_eur,
                       fed_rate_pct, boj_rate_pct, rate_differential,
                       rollover_direction, carry_strength
                FROM rollover_data
                ORDER BY date DESC
                LIMIT 1
            """)).fetchone()

        if row:
            return {
                'swap_long_pts':      row[0],
                'swap_short_pts':     row[1],
                'swap_long_eur':      row[2],
                'swap_short_eur':     row[3],
                'fed_rate_pct':       row[4],
                'boj_rate_pct':       row[5],
                'rate_differential':  row[6],
                'rollover_direction': row[7],
                'carry_strength':     row[8],
            }
    except Exception as e:
        logger.error("rollover_fetcher.get_today_error", error=str(e))

    return defaults


def download_vix_history(fred_api_key: str) -> pd.DataFrame:
    """
    Download daily VIX (CBOE Volatility Index) history from FRED series VIXCLS.

    Returns DataFrame with columns: date, vix_close, vix_regime
      vix_regime: -1 = calm (<15), 0 = normal (15–25), 1 = fearful (>25)

    Returns empty DataFrame on error. Never raises.
    """
    if not fred_api_key:
        logger.warning("rollover_fetcher.no_fred_key_vix")
        return pd.DataFrame()

    try:
        from fredapi import Fred
        fred = Fred(api_key=fred_api_key)

        series = fred.get_series('VIXCLS', observation_start='2010-01-01')
        rows = []
        for dt, val in series.items():
            if pd.notna(val):
                vix_val = float(val)
                if vix_val < 15:
                    regime = -1
                elif vix_val <= 25:
                    regime = 0
                else:
                    regime = 1
                rows.append({
                    'date':       dt.date(),
                    'vix_close':  vix_val,
                    'vix_regime': regime,
                })

        df = pd.DataFrame(rows)
        logger.info("rollover_fetcher.vix_downloaded", rows=len(df))
        return df

    except Exception as e:
        logger.error("rollover_fetcher.vix_error", error=str(e))
        return pd.DataFrame()


def save_vix_to_db(df: pd.DataFrame, engine) -> int:
    """
    Upsert VIX history into vix_data table.
    Returns number of rows processed.
    """
    if df.empty:
        return 0

    from sqlalchemy import text
    rows_inserted = 0

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(text("""
                INSERT INTO vix_data (date, vix_close, vix_regime)
                VALUES (:date, :vix_close, :vix_regime)
                ON CONFLICT (date) DO UPDATE
                  SET vix_close  = EXCLUDED.vix_close,
                      vix_regime = EXCLUDED.vix_regime
            """), {
                'date':       row['date'],
                'vix_close':  row['vix_close'],
                'vix_regime': int(row['vix_regime']),
            })
            rows_inserted += 1

    logger.info("rollover_fetcher.vix_saved", rows=rows_inserted)
    return rows_inserted


def get_latest_vix(engine) -> dict:
    """
    Query the most recent VIX reading from vix_data table.

    Returns {'vix_close': float, 'vix_regime': int}
    Returns neutral defaults (vix_close=20.0, vix_regime=0) if no data.
    """
    from sqlalchemy import text

    defaults = {'vix_close': 20.0, 'vix_regime': 0}

    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT vix_close, vix_regime
                FROM vix_data
                ORDER BY date DESC
                LIMIT 1
            """)).fetchone()

        if row:
            return {'vix_close': float(row[0]), 'vix_regime': int(row[1])}
    except Exception as e:
        logger.error("rollover_fetcher.get_vix_error", error=str(e))

    return defaults


def get_historical_vix(engine) -> pd.DataFrame:
    """
    Query full VIX history from vix_data table, sorted ascending by date.
    Used by decision_tree.py to merge VIX into training data.
    Returns empty DataFrame on error.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT date, vix_close, vix_regime
                FROM vix_data
                ORDER BY date ASC
            """)).fetchall()

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows, columns=['date', 'vix_close', 'vix_regime'])

    except Exception as e:
        logger.error("rollover_fetcher.historical_vix_error", error=str(e))
        return pd.DataFrame()


def download_nikkei_history() -> pd.DataFrame:
    """
    Download daily Nikkei 225 (^N225) history from Yahoo Finance.
    No API key required.
    Returns DataFrame with columns: date, nikkei_close, nikkei_regime
      nikkei_regime: -1 = down day (<-0.3%), 0 = flat, 1 = up day (>+0.3%)
    Returns empty DataFrame on error. Never raises.
    """
    try:
        import yfinance as yf
        raw = yf.download("^N225", start="2010-01-01", progress=False, auto_adjust=True)
        if raw.empty:
            logger.warning("rollover_fetcher.nikkei_empty")
            return pd.DataFrame()

        closes = raw["Close"]
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
        closes = closes.dropna()
        pct    = closes.pct_change()
        rows   = []
        for dt, close_val in closes.items():
            chg = pct.get(dt, float("nan"))
            if isinstance(chg, pd.Series):
                chg = chg.iloc[0] if not chg.empty else float("nan")
            if pd.isna(chg):
                regime = 0
            elif chg > 0.003:
                regime = 1
            elif chg < -0.003:
                regime = -1
            else:
                regime = 0
            rows.append({
                "date":          dt.date() if hasattr(dt, "date") else dt,
                "nikkei_close":  float(close_val),
                "nikkei_regime": regime,
            })

        df = pd.DataFrame(rows)
        logger.info("rollover_fetcher.nikkei_downloaded", rows=len(df))
        return df

    except Exception as e:
        logger.error("rollover_fetcher.nikkei_error", error=str(e))
        return pd.DataFrame()


def save_nikkei_to_db(df: pd.DataFrame, engine) -> int:
    """Upsert Nikkei history into nikkei_data table. Returns rows processed."""
    if df.empty:
        return 0

    from sqlalchemy import text
    rows_inserted = 0

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(text("""
                INSERT INTO nikkei_data (date, nikkei_close, nikkei_regime)
                VALUES (:date, :nikkei_close, :nikkei_regime)
                ON CONFLICT (date) DO UPDATE
                  SET nikkei_close  = EXCLUDED.nikkei_close,
                      nikkei_regime = EXCLUDED.nikkei_regime
            """), {
                "date":          row["date"],
                "nikkei_close":  row["nikkei_close"],
                "nikkei_regime": int(row["nikkei_regime"]),
            })
            rows_inserted += 1

    logger.info("rollover_fetcher.nikkei_saved", rows=rows_inserted)
    return rows_inserted


def get_latest_nikkei(engine) -> dict:
    """
    Query the most recent Nikkei reading.
    Returns neutral defaults if no data: {'nikkei_close': 0.0, 'nikkei_regime': 0}
    """
    from sqlalchemy import text
    defaults = {"nikkei_close": 0.0, "nikkei_regime": 0}
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT nikkei_close, nikkei_regime
                FROM nikkei_data
                ORDER BY date DESC
                LIMIT 1
            """)).fetchone()
        if row:
            return {"nikkei_close": float(row[0]), "nikkei_regime": int(row[1])}
    except Exception as e:
        logger.error("rollover_fetcher.get_nikkei_error", error=str(e))
    return defaults


def get_historical_nikkei(engine) -> pd.DataFrame:
    """
    Full Nikkei history sorted ascending. Used by decision_tree for training merge.
    Returns empty DataFrame on error.
    """
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT date, nikkei_close, nikkei_regime
                FROM nikkei_data
                ORDER BY date ASC
            """)).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=["date", "nikkei_close", "nikkei_regime"])
    except Exception as e:
        logger.error("rollover_fetcher.historical_nikkei_error", error=str(e))
        return pd.DataFrame()


def get_historical_rate_differential(engine) -> pd.DataFrame:
    """
    Query full rate differential history from interest_rates table.
    Used by decision_tree.py to add carry features to training data.

    Returns DataFrame with columns: date, fed_rate_pct, boj_rate_pct,
    rate_differential, rollover_direction, carry_strength

    Sorted ascending by date.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    f.date,
                    f.rate_pct AS fed_rate_pct,
                    b.rate_pct AS boj_rate_pct,
                    (f.rate_pct - b.rate_pct) AS rate_differential
                FROM interest_rates f
                JOIN interest_rates b ON f.date = b.date
                WHERE f.series = 'FEDFUNDS'
                  AND b.series = 'IRSTCI01JPM156N'
                ORDER BY f.date ASC
            """)).fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=[
            'date', 'fed_rate_pct', 'boj_rate_pct', 'rate_differential'
        ])

        # Derive rollover features (same logic as compute_rollover_signal)
        df['rollover_direction'] = df['rate_differential'].apply(
            lambda d: 1 if d > 1.0 else (-1 if d < -1.0 else 0)
        )
        df['carry_strength'] = df['rate_differential'].apply(
            lambda d: min(abs(d) / 5.0, 1.0)
        )
        return df

    except Exception as e:
        logger.error("rollover_fetcher.historical_error", error=str(e))
        return pd.DataFrame()
