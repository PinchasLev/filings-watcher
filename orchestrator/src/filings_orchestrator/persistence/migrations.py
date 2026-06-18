"""Migration runner: apply numbered SQL files in order, track applied versions.

Migrations live as plain `.sql` files in a sibling `db/migrations/` directory
at the orchestrator project root. The runner:

1. Ensures a `schema_versions` tracking table exists.
2. Reads migration files in alphabetical order.
3. Applies any whose version (the filename without extension) is not already
   recorded.
4. Records each successful application in `schema_versions`.

The discipline is deliberate plain-SQL portability: any migration file must
run identically on SQLite and on Postgres. No ORM, no engine-specific SQL.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import Engine, text

# Where the SQL files live, relative to this module.
# Path layout:
#   orchestrator/
#   ├── db/migrations/
#   └── src/filings_orchestrator/persistence/migrations.py  (this file)
# So we walk up three: persistence/ → filings_orchestrator/ → src/ → orchestrator/.
_MIGRATIONS_DIR_RELATIVE = Path("..") / ".." / ".." / "db" / "migrations"

_CREATE_VERSIONS_TABLE = text(
    """
    CREATE TABLE IF NOT EXISTS schema_versions (
        version    TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """
)


class _AppliedMigration(NamedTuple):
    version: str
    applied_at: str


def _migrations_dir() -> Path:
    """Resolve the db/migrations directory regardless of cwd."""
    return (Path(__file__).resolve().parent / _MIGRATIONS_DIR_RELATIVE).resolve()


def _list_migration_files(directory: Path) -> list[Path]:
    if not directory.exists():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    return sorted(directory.glob("*.sql"))


def _read_applied_versions(engine: Engine) -> set[str]:
    with engine.begin() as conn:
        conn.execute(_CREATE_VERSIONS_TABLE)
        rows = conn.execute(text("SELECT version FROM schema_versions")).fetchall()
    return {row[0] for row in rows}


def _strip_line_comments(sql_text: str) -> str:
    """Remove `-- ...` line comments before statement splitting.

    Naive — does not handle `--` inside string literals. Our migrations are
    pure DDL with no string literals, so this is sufficient. If a future
    migration needs to insert data containing `--`, switch to sqlparse.
    """
    lines: list[str] = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


_BEGIN_END_OR_SEMI = re.compile(r"(\bBEGIN\b|\bEND\b|;)", re.IGNORECASE)


def _split_statements(sql_text: str) -> list[str]:
    """Split a migration into individual statements, splitting on `;`.

    `CREATE TRIGGER` bodies contain their own `;`-terminated statements inside a
    `BEGIN ... END` block, so a naive `split(";")` would tear a trigger apart. We
    track `BEGIN`/`END` nesting (word-boundary matched, so `APPEND` etc. are safe)
    and split only on semicolons at depth 0. Plain DDL has no `BEGIN`/`END`, so it
    is unaffected.
    """
    cleaned = _strip_line_comments(sql_text)
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    for tok in _BEGIN_END_OR_SEMI.split(cleaned):
        if not tok:
            continue
        upper = tok.upper()
        if upper == "BEGIN":
            depth += 1
            buf.append(tok)
        elif upper == "END":
            depth = max(0, depth - 1)
            buf.append(tok)
        elif tok == ";" and depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(tok)
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _apply_one(engine: Engine, version: str, sql_text: str) -> None:
    # SQLite's sqlalchemy driver does not execute multiple statements in a
    # single text() call. Strip line comments, split on `;`, execute one at
    # a time, then record the applied version in the same transaction.
    statements = _split_statements(sql_text)
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
        conn.execute(
            text("INSERT INTO schema_versions (version, applied_at) VALUES (:v, :t)"),
            {"v": version, "t": datetime.now(UTC).isoformat()},
        )


def apply_migrations(
    engine: Engine,
    *,
    migrations_dir: Path | None = None,
) -> list[_AppliedMigration]:
    """Apply every migration whose version is not already recorded.

    Returns the list of migrations applied in this call. An empty list means
    the DB was already up to date.
    """
    directory = migrations_dir or _migrations_dir()
    applied = _read_applied_versions(engine)
    pending = [p for p in _list_migration_files(directory) if p.stem not in applied]

    now = datetime.now(UTC).isoformat()
    applied_now: list[_AppliedMigration] = []
    for path in pending:
        _apply_one(engine, path.stem, path.read_text())
        applied_now.append(_AppliedMigration(version=path.stem, applied_at=now))
    return applied_now
