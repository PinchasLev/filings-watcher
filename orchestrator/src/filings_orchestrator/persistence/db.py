"""SQLAlchemy Engine factory for the filings DB.

The engine is the connection pool root; callers acquire short-lived
connections via `engine.connect()` or `engine.begin()` and release them
when done. We commit to SQLAlchemy Core (no ORM) per ADR 0008 to keep the
SQL portable to Postgres.

WAL mode is enabled on every connection (idempotent — once set on the
DB file, the setting is sticky in SQLite). WAL lets readers and writers
operate concurrently without blocking each other, which is required once
the Go service starts reading the same file the Python orchestrator
writes to.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text


def _enable_wal(dbapi_connection: object, _connection_record: object) -> None:
    """Set journal_mode=WAL on every new raw connection.

    SQLite makes the journal-mode setting sticky in the DB file — the first
    connection that sets WAL switches the file, and subsequent connections
    inherit it automatically. Setting it on every connection is cheap and
    defensive: it ensures WAL even if someone (or some test) inadvertently
    flipped the mode back. In-memory databases ignore the pragma silently.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
    finally:
        cursor.close()


def open_engine(db_path: str) -> Engine:
    """Open (and create the parent directory of) a SQLite Engine.

    `db_path` may be an absolute or tilde-expanded path. The special value
    `":memory:"` returns an in-memory database — used by tests.
    """
    if db_path == ":memory:":
        engine = create_engine("sqlite:///:memory:")
    else:
        resolved = os.path.expanduser(db_path)
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{resolved}")

    event.listen(engine, "connect", _enable_wal)
    # Trigger one connection so WAL is applied to the underlying DB file
    # before any caller touches it.
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return engine
