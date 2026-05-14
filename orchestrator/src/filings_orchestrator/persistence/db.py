"""SQLAlchemy Engine factory for the filings DB.

The engine is the connection pool root; callers acquire short-lived
connections via `engine.connect()` or `engine.begin()` and release them
when done. We commit to SQLAlchemy Core (no ORM) per ADR 0008 to keep the
SQL portable to Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine


def open_engine(db_path: str) -> Engine:
    """Open (and create the parent directory of) a SQLite Engine.

    `db_path` may be an absolute or tilde-expanded path. The special value
    `":memory:"` returns an in-memory database — used by tests.
    """
    if db_path == ":memory:":
        return create_engine("sqlite:///:memory:")

    resolved = os.path.expanduser(db_path)
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{resolved}")
