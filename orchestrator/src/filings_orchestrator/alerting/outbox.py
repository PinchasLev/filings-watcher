"""Data access for the `alerts_outbox` table (ADR 0031).

The alerting subsystem owns its own persistence rather than sharing the
classifier/product repository: the outbox is auxiliary, and keeping its queries
here keeps the operational concern distinct from product data access. These
functions still ride the shared SQLAlchemy Engine and migrations from
`persistence` — that layer is infrastructure, not classifier code.

The write half (`insert_alert`) takes a Connection, not an Engine, so a caller
can emit an alert in the SAME transaction as the state change that warrants it —
the transactional-outbox guarantee. `emit` (emit.py) is the standalone-
transaction wrapper over it. The read/update half is the drainer's work set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import Connection, Engine, bindparam, text


class PendingAlert(NamedTuple):
    """One undelivered row of the alerting outbox, as the drainer sees it."""

    id: int
    created_at: str
    severity: str
    title: str
    body: str | None
    fields: dict[str, object]
    dedup_key: str | None


def insert_alert(
    conn: Connection,
    *,
    severity: str,
    title: str,
    body: str | None = None,
    fields: dict[str, object] | None = None,
    dedup_key: str | None = None,
    created_at: str | None = None,
) -> None:
    """Append one row to `alerts_outbox` using the caller's connection.

    Takes a `Connection` rather than an `Engine` deliberately: the alert is
    written inside whatever transaction the caller already holds, so it
    commits atomically with the work it reports (ADR 0031). For the standalone
    case — no surrounding transaction — use `emit`, which opens its own.

    `fields` is serialized to the `fields_json` column; `created_at` defaults
    to now (UTC, ISO 8601). Delivery columns (`delivered_at`, `attempts`,
    `last_error`) take their schema defaults — this is the producer half only.
    """
    conn.execute(
        text(
            """
            INSERT INTO alerts_outbox (
                created_at, severity, title, body, fields_json, dedup_key
            )
            VALUES (:created_at, :severity, :title, :body, :fields_json, :dedup_key)
            """
        ),
        {
            "created_at": created_at or datetime.now(UTC).isoformat(),
            "severity": severity,
            "title": title,
            "body": body,
            "fields_json": json.dumps(fields or {}),
            "dedup_key": dedup_key,
        },
    )


def fetch_undelivered_alerts(engine: Engine, *, limit: int | None = None) -> list[PendingAlert]:
    """Return undelivered outbox rows, oldest first — the drainer's work set.

    `delivered_at IS NULL` is the work predicate (matched by the partial index
    from migration 008). `limit` bounds a single drain pass; None returns all.
    """
    sql = """
        SELECT id, created_at, severity, title, body, fields_json, dedup_key
          FROM alerts_outbox
         WHERE delivered_at IS NULL
         ORDER BY created_at, id
    """
    if limit is not None:
        sql += "\n         LIMIT :limit"
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"limit": limit} if limit is not None else {}).fetchall()
    return [
        PendingAlert(
            id=int(r[0]),
            created_at=r[1],
            severity=r[2],
            title=r[3],
            body=r[4],
            fields=json.loads(r[5]) if r[5] else {},
            dedup_key=r[6],
        )
        for r in rows
    ]


def mark_alert_delivered(engine: Engine, alert_id: int, *, count_attempt: bool = True) -> None:
    """Retire an outbox row: stamp `delivered_at`, removing it from the work set.

    Used for both a real delivery and a deliberate non-delivery (a row dropped
    by the freshness window or coalesced by dedup): in every case the row is
    done and must leave `delivered_at IS NULL`. `count_attempt` controls whether
    this also bumps `attempts` — True for an actual POST, False for a suppression
    where no request was made, so `attempts` stays an honest POST counter.
    """
    set_attempts = ", attempts = attempts + 1" if count_attempt else ""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                UPDATE alerts_outbox
                   SET delivered_at = :delivered_at{set_attempts}
                 WHERE id = :id
                """
            ),
            {"delivered_at": datetime.now(UTC).isoformat(), "id": alert_id},
        )


def record_alert_delivery_failure(engine: Engine, alert_id: int, error: str) -> None:
    """Record a failed delivery POST: bump `attempts`, store `last_error`.

    `delivered_at` is left NULL, so the row stays in the work set and the next
    drain pass retries it. This is the at-least-once half of the outbox: a POST
    that errored simply waits for the next timer firing.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE alerts_outbox
                   SET attempts = attempts + 1,
                       last_error = :error
                 WHERE id = :id
                """
            ),
            {"error": error, "id": alert_id},
        )


def delivered_dedup_keys(engine: Engine, keys: list[str]) -> set[str]:
    """Return which of `keys` already have a delivered row (the coalesce set).

    The drainer suppresses a pending alert whose `dedup_key` already paged: the
    period lives in the key itself (e.g. `cost_cap:2026-06-15`), so a key that
    has ever been delivered means "this condition was already reported for its
    window". Empty `keys` short-circuits to an empty set (no query).
    """
    if not keys:
        return set()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT dedup_key
                  FROM alerts_outbox
                 WHERE delivered_at IS NOT NULL
                   AND dedup_key IN :keys
                """
            ).bindparams(bindparam("keys", expanding=True)),
            {"keys": keys},
        ).fetchall()
    return {r[0] for r in rows}
