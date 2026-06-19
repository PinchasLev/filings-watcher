"""Persist and verify the per-version taxonomy snapshot (ADR 0032).

When a taxonomy version is first seen it is *cut*: its anchor plus all of its
domain and leaf rows are written in one transaction, with a content hash that
binds the version to its definition. On every later run the live in-code
taxonomy and the stored snapshot rows are both verified against that recorded
hash — so a content change that forgot to bump the version, or any tampering with
a frozen snapshot (including an appended leaf), is caught as a loud abort rather
than silent drift. This makes `taxonomy_version` a trustworthy identifier of the
exact choice-set a classification faced.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text

from filings_orchestrator.classify.taxonomy import (
    TAXONOMY_VERSION,
    hash_taxonomy_content,
    taxonomy_content_hash,
    taxonomy_definition,
)


class TaxonomyIntegrityError(RuntimeError):
    """The live taxonomy or a stored snapshot diverges from the version it claims.

    Signals a violation of the choice-set invariant (ADR 0032): a `taxonomy_version`
    must denote exactly one set of leaves, descriptions, and rollup.
    """


def _parse_major_minor(version: str) -> tuple[int, int]:
    """Parse a `vMAJOR[.MINOR]` version into its parts. `"v1"` reads as `(1, 0)`."""
    raw = version[1:] if version[:1].lower() == "v" else version
    parts = raw.split(".")
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return major, minor


def leaves_for_version(engine: Engine, version: str) -> list[str]:
    """Return the leaf values recorded in the snapshot for `version`.

    The choice-set that version offered the classifier — used by classify-ab to
    classify a sample under a prior taxonomy version (the baseline arm).
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT leaf FROM taxonomy_leaves WHERE taxonomy_version = :v ORDER BY leaf"),
            {"v": version},
        ).fetchall()
    return [str(r[0]) for r in rows]


def _recorded_hash(engine: Engine, version: str) -> str | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT content_hash FROM taxonomy_versions WHERE taxonomy_version = :v"),
            {"v": version},
        ).fetchone()
    return None if row is None else str(row[0])


def _stored_rows_hash(engine: Engine, version: str) -> str:
    """Recompute the content hash from the stored snapshot rows for `version`.

    Uses the same canonicalization as the in-code hash, so a match proves the
    persisted snapshot has not been altered since the version was cut.
    """
    with engine.begin() as conn:
        domains = conn.execute(
            text("SELECT domain, description FROM taxonomy_domains WHERE taxonomy_version = :v"),
            {"v": version},
        ).fetchall()
        leaves = conn.execute(
            text(
                "SELECT leaf, description, domain FROM taxonomy_leaves WHERE taxonomy_version = :v"
            ),
            {"v": version},
        ).fetchall()
    return hash_taxonomy_content(
        [(str(d[0]), None if d[1] is None else str(d[1])) for d in domains],
        [(str(r[0]), str(r[1]), str(r[2])) for r in leaves],
    )


def _cut_version(conn: Connection, version: str, content_hash: str) -> None:
    """Write a version's anchor + all domain and leaf rows in the caller's tx."""
    definition = taxonomy_definition()
    major, minor = _parse_major_minor(version)
    conn.execute(
        text(
            """
            INSERT INTO taxonomy_versions (taxonomy_version, major, minor, content_hash, cut_at)
            VALUES (:v, :major, :minor, :hash, :cut_at)
            """
        ),
        {
            "v": version,
            "major": major,
            "minor": minor,
            "hash": content_hash,
            "cut_at": datetime.now(UTC).isoformat(),
        },
    )
    conn.execute(
        text(
            "INSERT INTO taxonomy_domains (taxonomy_version, domain, description) "
            "VALUES (:v, :domain, :description)"
        ),
        [
            {"v": version, "domain": d.domain, "description": d.description}
            for d in definition.domains
        ],
    )
    conn.execute(
        text(
            "INSERT INTO taxonomy_leaves (taxonomy_version, leaf, description, domain) "
            "VALUES (:v, :leaf, :description, :domain)"
        ),
        [
            {
                "v": version,
                "leaf": leaf.leaf,
                "description": leaf.description,
                "domain": leaf.domain,
            }
            for leaf in definition.leaves
        ],
    )


def ensure_taxonomy_snapshot(engine: Engine) -> None:
    """Cut the current taxonomy version if unseen, else verify integrity.

    Idempotent. Raises `TaxonomyIntegrityError` if the current `TAXONOMY_VERSION`
    already exists but the in-code taxonomy (a forgotten bump) or the stored
    snapshot rows (tampering) no longer hash-match its anchor.
    """
    version = TAXONOMY_VERSION
    code_hash = taxonomy_content_hash()

    recorded = _recorded_hash(engine, version)
    if recorded is None:
        with engine.begin() as conn:
            _cut_version(conn, version, code_hash)
        return

    if recorded != code_hash:
        raise TaxonomyIntegrityError(
            f"in-code taxonomy hashes to {code_hash[:12]} but {version} was cut as "
            f"{recorded[:12]}: the taxonomy changed without a version bump (ADR 0032). "
            f"Bump TAXONOMY_VERSION to cut a new version."
        )
    stored = _stored_rows_hash(engine, version)
    if stored != recorded:
        raise TaxonomyIntegrityError(
            f"stored snapshot for {version} hashes to {stored[:12]} but its anchor is "
            f"{recorded[:12]}: the frozen snapshot rows were modified (ADR 0032)."
        )
