"""
db_writer.py — Utility helpers for database operations.
"""
import logging
import os
import re
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


def apply_schema(engine, sql_path: str = "/app/init.sql") -> None:
    """
    Apply init.sql to DB. Safe to call on every startup — uses IF NOT EXISTS.
    Runs from the bot container so it's independent of the timescaledb init quirks.
    """
    if not os.path.exists(sql_path):
        log.warning(f"apply_schema: file not found at {sql_path}")
        return
    with open(sql_path) as f:
        sql = f.read()
    # Strip single-line comments, then split on semicolons
    sql = re.sub(r'--[^\n]*', '', sql)
    statements = [s.strip() for s in sql.split(';') if s.strip()]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                log.debug(f"apply_schema: skipped statement ({exc})")


def table_row_count(table: str) -> int:
    """Return the number of rows in the given table, or 0 if it doesn't exist."""
    engine = get_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            return result.scalar()
    except sqlalchemy.exc.ProgrammingError:
        return 0


def is_table_empty(table: str) -> bool:
    return table_row_count(table) == 0
