"""Raise an operator alert from anywhere in the orchestrator (ADR 0031).

`emit_alert` is the one verb a producer needs: it appends a row to the
`alerts_outbox` table. A separate standalone `alarm-drain` CLI (a later PR)
is the only component that reads the table and delivers rows to Discord — so
call sites here stay trivial and carry no transport knowledge. Swapping Discord
for Slack later changes only the drainer, never these call sites.

Two severities route to two Discord channels: `ALERT` (needs human action) and
`INFO` (situational awareness). Pick the channel by the severity argument.

This module is the standalone-transaction path: each `emit_alert` opens its own
`engine.begin()`. When an alert must commit *atomically with* the state change
that warrants it (the transactional-outbox guarantee), call
`repository.insert_alert(conn, ...)` directly inside that work's transaction
instead — `emit_alert` is the wrapper over it for the common case where there
is no surrounding transaction to enlist in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine

from filings_orchestrator.persistence.repository import insert_alert

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
    optional coalescing key the drainer uses to suppress re-paging a standing
    condition (None = always deliver). Remaining keyword args become the
    structured `fields` the drainer renders into the message.
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
