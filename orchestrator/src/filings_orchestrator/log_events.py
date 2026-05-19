"""Structured log emission for orchestrator ticks.

Per ADR 0013: every operation an operator might want to debug after the
fact gets a JSON line. Lines go to stdout and are captured by journald
when the orchestrator runs under systemd. Per ADR 0018 the same lines
are the input to the future OTel collector.

This module deliberately uses only stdlib. The cost of pulling in a
structured-logging library (structlog, loguru) for this use case would
exceed the benefit: emit a line, keep moving.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any


def emit(event: str, **fields: Any) -> None:
    """Print one JSON line carrying `event`, timestamp, and the fields.

    Field names are part of the operational contract — see ADR 0021 for
    the canonical set used by the daily-index ingest path. Avoid
    drift; the OTel exporter joins on these names.
    """
    payload: dict[str, Any] = {
        "event": event,
        "ts": datetime.now(UTC).isoformat(),
    }
    payload.update(fields)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
