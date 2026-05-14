"""CLI: apply pending DB migrations.

Usage:
    uv run migrate-db
    FILINGS_DB_PATH=/tmp/dev.db uv run migrate-db   # override DB location
"""

from __future__ import annotations

import sys

from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.persistence import apply_migrations, open_engine


def main() -> None:
    try:
        config = load_config()
    except MissingConfigError as e:
        sys.exit(
            f"{e}\nCopy orchestrator/.env.example to orchestrator/.env and fill in real values."
        )

    print(f"DB path: {config.filings_db_path}")
    engine = open_engine(config.filings_db_path)
    applied = apply_migrations(engine)

    if not applied:
        print("Already up to date — no migrations to apply.")
        return

    print(f"Applied {len(applied)} migration(s):")
    for m in applied:
        print(f"  {m.version}  ({m.applied_at})")


if __name__ == "__main__":
    main()
