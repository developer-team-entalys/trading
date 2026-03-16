"""
dom_fetcher.py — Persist DOM (Depth of Market) snapshots to TimescaleDB.
"""
import logging
from sqlalchemy import text

log = logging.getLogger(__name__)


def save_dom_snapshot_to_db(snapshot: dict, engine) -> None:
    """Upsert one DOM snapshot row into dom_snapshots."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO dom_snapshots (
                    time, symbol, best_bid, best_ask, spread_pips,
                    bid_depth_total, ask_depth_total, order_imbalance, levels_count
                ) VALUES (
                    :time, :symbol, :best_bid, :best_ask, :spread_pips,
                    :bid_depth_total, :ask_depth_total, :order_imbalance, :levels_count
                )
                ON CONFLICT DO NOTHING
            """),
            snapshot,
        )
    spread = snapshot.get("spread_pips")
    log.debug(
        f"DOM snapshot saved: spread={'N/A' if spread is None else f'{spread:.2f}'} pips, "
        f"imbalance={snapshot.get('order_imbalance', 0.5):.3f}, "
        f"levels={snapshot.get('levels_count')}"
    )
