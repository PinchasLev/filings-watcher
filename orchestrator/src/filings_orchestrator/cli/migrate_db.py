"""CLI: apply pending DB migrations.

Usage:
    uv run migrate-db
    FILINGS_DB_PATH=/tmp/dev.db uv run migrate-db   # override DB location
"""

from __future__ import annotations

import sys

from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.taxonomy_snapshot import (
    TaxonomyIntegrityError,
    ensure_taxonomy_snapshot,
)


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

    if applied:
        print(f"Applied {len(applied)} migration(s):")
        for m in applied:
            print(f"  {m.version}  ({m.applied_at})")
    else:
        print("Already up to date — no migrations to apply.")

    # Cut the current taxonomy version if unseen, else verify it (ADR 0032). A
    # drifted taxonomy — content changed without a version bump — aborts here, so
    # a deploy cannot ship a taxonomy whose version label no longer matches its
    # choice-set.
    try:
        ensure_taxonomy_snapshot(engine)
        print("Taxonomy snapshot verified.")
    except TaxonomyIntegrityError as e:
        sys.exit(f"Taxonomy integrity check failed:\n{e}")


if __name__ == "__main__":
    main()
