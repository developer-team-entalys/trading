"""
cot_fetcher.py — Download and parse CFTC Commitment of Traders data.

Uses the Traders in Financial Futures (TFF) disaggregated report.
URL pattern: https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip

Column mapping (TFF report):
  Lev_Money  = Leveraged Money / Hedge Funds  → our "noncomm"
  Asset_Mgr  = Asset Managers / Institutional → our "comm"
"""
import io
import zipfile
import logging
from datetime import datetime, date

import requests
import pandas as pd
import numpy as np
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

JPY_MARKET_NAME = "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE"

# TFF report column names → our internal names
COT_COLUMN_MAP = {
    "Market_and_Exchange_Names":     "market_name",
    "Report_Date_as_YYYY-MM-DD":     "week_date",
    "Lev_Money_Positions_Long_All":  "noncomm_long",
    "Lev_Money_Positions_Short_All": "noncomm_short",
    "Asset_Mgr_Positions_Long_All":  "comm_long",
    "Asset_Mgr_Positions_Short_All": "comm_short",
    "Open_Interest_All":             "open_interest",
}


def download_cot_year(year: int) -> pd.DataFrame:
    """Download and parse COT (TFF) data for a specific year."""
    url = config.COT_URL.format(year=year)
    log.info(f"Downloading COT data for {year} from {url}")

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        csv_name = next(
            n for n in z.namelist() if n.lower().endswith((".txt", ".csv"))
        )
        with z.open(csv_name) as f:
            df = pd.read_csv(f, low_memory=False)

    # Keep only columns we need (skip missing ones gracefully)
    available = {k: v for k, v in COT_COLUMN_MAP.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available)

    # Filter for JPY futures
    df = df[df["market_name"] == JPY_MARKET_NAME].copy()
    df = df.drop(columns=["market_name"])

    df["week_date"] = pd.to_datetime(df["week_date"]).dt.date

    for col in ["noncomm_long", "noncomm_short", "comm_long", "comm_short", "open_interest"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    log.info(f"Fetched {len(df)} JPY COT rows for {year}")
    return df


def download_cot_history(start_year: int = 2010) -> pd.DataFrame:
    """Download all COT data from start_year to current year."""
    current_year = datetime.utcnow().year
    frames = []

    for year in range(start_year, current_year + 1):
        try:
            df = download_cot_year(year)
            frames.append(df)
        except Exception as exc:
            log.warning(f"Could not fetch COT data for {year}: {exc}")

    if not frames:
        raise RuntimeError("No COT data could be downloaded.")

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["week_date"]).sort_values("week_date")

    # Derived columns
    combined["noncomm_net"] = combined["noncomm_long"] - combined["noncomm_short"]
    combined["comm_net"] = combined["comm_long"] - combined["comm_short"]
    combined["noncomm_net_change"] = combined["noncomm_net"].diff().fillna(0).astype("int64")
    combined["noncomm_net_pct"] = np.where(
        combined["open_interest"] > 0,
        combined["noncomm_net"] / combined["open_interest"] * 100,
        0.0,
    )

    return combined.reset_index(drop=True)


def save_cot_to_db(df: pd.DataFrame, engine) -> int:
    """Upsert COT data into TimescaleDB. Returns number of rows inserted/updated."""
    rows_affected = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            result = conn.execute(
                text("""
                    INSERT INTO cot_data (
                        week_date, noncomm_long, noncomm_short, noncomm_net,
                        comm_long, comm_short, comm_net,
                        noncomm_net_change, noncomm_net_pct, open_interest
                    ) VALUES (
                        :week_date, :noncomm_long, :noncomm_short, :noncomm_net,
                        :comm_long, :comm_short, :comm_net,
                        :noncomm_net_change, :noncomm_net_pct, :open_interest
                    )
                    ON CONFLICT (week_date) DO UPDATE SET
                        noncomm_long        = EXCLUDED.noncomm_long,
                        noncomm_short       = EXCLUDED.noncomm_short,
                        noncomm_net         = EXCLUDED.noncomm_net,
                        comm_long           = EXCLUDED.comm_long,
                        comm_short          = EXCLUDED.comm_short,
                        comm_net            = EXCLUDED.comm_net,
                        noncomm_net_change  = EXCLUDED.noncomm_net_change,
                        noncomm_net_pct     = EXCLUDED.noncomm_net_pct,
                        open_interest       = EXCLUDED.open_interest
                """),
                {
                    "week_date": row["week_date"],
                    "noncomm_long": int(row["noncomm_long"]),
                    "noncomm_short": int(row["noncomm_short"]),
                    "noncomm_net": int(row["noncomm_net"]),
                    "comm_long": int(row["comm_long"]),
                    "comm_short": int(row["comm_short"]),
                    "comm_net": int(row["comm_net"]),
                    "noncomm_net_change": int(row["noncomm_net_change"]),
                    "noncomm_net_pct": float(row["noncomm_net_pct"]),
                    "open_interest": int(row["open_interest"]),
                },
            )
            rows_affected += result.rowcount
    log.info(f"Upserted {rows_affected} COT rows into DB")
    return rows_affected


def fetch_latest_cot() -> dict:
    """Fetch only the most recent week's COT data for the live signal."""
    current_year = datetime.utcnow().year
    try:
        df = download_cot_year(current_year)
    except Exception:
        df = download_cot_year(current_year - 1)

    if df.empty:
        return {}

    df = df.sort_values("week_date")
    df["noncomm_net"] = df["noncomm_long"] - df["noncomm_short"]
    df["comm_net"] = df["comm_long"] - df["comm_short"]
    df["noncomm_net_change"] = df["noncomm_net"].diff().fillna(0).astype("int64")
    df["noncomm_net_pct"] = np.where(
        df["open_interest"] > 0,
        df["noncomm_net"] / df["open_interest"] * 100,
        0.0,
    )

    latest = df.iloc[-1]
    return {
        "week_date": latest["week_date"],
        "noncomm_long": int(latest["noncomm_long"]),
        "noncomm_short": int(latest["noncomm_short"]),
        "noncomm_net": int(latest["noncomm_net"]),
        "noncomm_net_change": int(latest["noncomm_net_change"]),
        "noncomm_net_pct": float(latest["noncomm_net_pct"]),
        "open_interest": int(latest["open_interest"]),
    }
