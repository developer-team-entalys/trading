"""
db_writer.py — Utility helpers for database operations.
"""
import logging
import sqlalchemy
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

log = logging.getLogger(__name__)

_engine = None


def get_engine() -> sqlalchemy.Engine:
    """Return (or create) a shared SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = sqlalchemy.create_engine(
            config.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def table_row_count(table: str) -> int:
    """Return the number of rows in the given table."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return result.scalar()


def is_table_empty(table: str) -> bool:
    return table_row_count(table) == 0
