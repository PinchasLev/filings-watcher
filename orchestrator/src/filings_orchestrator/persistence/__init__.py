"""Persistence layer: SQLAlchemy Core over portable SQL.

The package commits to two architectural rules from ADR 0008 and ADR 0011:

- Portable SQL only — schema and queries must run identically on SQLite and
  Postgres. Use SQLAlchemy Core (query builder) rather than the ORM.
- Append-only, version-tagged classifications — never updated in place.

Public surface:

- `open_engine(path)` — open a SQLAlchemy Engine pointing at a SQLite file
- `apply_migrations(engine)` — apply any unapplied SQL migrations
- `repository` module — typed insert/get helpers
"""

from filings_orchestrator.persistence.db import open_engine
from filings_orchestrator.persistence.migrations import apply_migrations

__all__ = ["apply_migrations", "open_engine"]
