"""Operational alerting subsystem (ADR 0031).

A self-contained subsystem, distinct from the classifier: producers anywhere in
the orchestrator call `emit_alert` to append a row to the `alerts_outbox` table,
and the standalone `alarm-drain` worker (`drain.py`) delivers those rows to
Discord. The public producer surface is re-exported here so call sites import
`from filings_orchestrator.alerting import emit_alert, ALERT, INFO`; the
delivery internals (notify, outbox, drain) stay module-private to the package.
"""

from __future__ import annotations

from filings_orchestrator.alerting.emit import ALERT, INFO, emit_alert

__all__ = ["ALERT", "INFO", "emit_alert"]
