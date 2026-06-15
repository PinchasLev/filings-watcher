"""Raise an operator alert from anywhere in the orchestrator (ADR 0031).

`emit_alert` is the one verb a producer needs: it appends a row to the
`alerts_outbox` table. The standalone `alarm-drain` worker (see `drain.py`) is
the only component that reads the table and delivers rows to Discord — so call
sites here stay trivial and carry no transport knowledge. Swapping Discord for
Slack later changes only the drainer, never these call sites.

Two severities route to two Discord channels: `ALERT` (needs human action) and
`INFO` (situational awareness). Pick the channel by the severity argument.

This is the standalone-transaction path: each `emit_alert` opens its own
`engine.begin()`. When an alert must commit *atomically with* the state change
that warrants it (the transactional-outbox guarantee), call
`outbox.insert_alert(conn, ...)` directly inside that work's transaction
instead — `emit_alert` is the wrapper over it for the common case where there
is no surrounding transaction to enlist in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine

from filings_orchestrator.alerting.outbox import insert_alert

# Severity values double as the delivery-routing discriminator: each maps to
# one Discord channel. Kept as plain strings (not an enum) to match the TEXT
# column and the portable-SQL discipline; the frozenset guards typos at emit.
ALERT = "alert"
INFO = "info"
_VALID_SEVERITIES = frozenset({ALERT, INFO})


def emit_alert(
    engine: Engine,
    severity: str,
    title: str,
    *,
    body: str | None = None,
    dedup_key: str | None = None,
    **fields: Any,
) -> None:
    """Append one alert to the outbox in its own transaction.

    `severity` must be `ALERT` or `INFO` (it routes the Discord channel).
    `title` is the headline; `body` an optional longer detail; `dedup_key` an
    optional coalescing key (None = always deliver). Remaining keyword args
    become the structured `fields` the drainer renders into the message.

    Choosing `dedup_key`: pick it at the granularity the operator *acts on*. A
    per-entity key (e.g. `classification_abandoned:{accession}`) pages once per
    independently-actionable thing. A per-cause key (e.g. `classify_outage`)
    collapses one root cause that trips across many entities into a single page
    instead of a storm. Within the drainer's repeat window (ALERT_REPEAT_HOURS)
    a key pages at most once; a still-firing condition re-pages once per window
    until the producer stops emitting it.
    """
    if severity not in _VALID_SEVERITIES:
        raise ValueError(
            f"unknown alert severity {severity!r}; expected one of {sorted(_VALID_SEVERITIES)}"
        )
    with engine.begin() as conn:
        insert_alert(
            conn,
            severity=severity,
            title=title,
            body=body,
            fields=dict(fields),
            dedup_key=dedup_key,
        )
