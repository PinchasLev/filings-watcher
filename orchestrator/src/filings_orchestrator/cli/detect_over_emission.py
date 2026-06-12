"""CLI: detect over-emitted reduce events (events subsumed by another event).

Read-only audit over the persisted events layer. For each filing's latest
reduce run (ADR 0028), it reports any event whose contributing-Item set is a
subset of another event's in the same run — the reducer over-emission the
ADIL merger filing exhibited, where one map output is double-counted across
two event cards.

    uv run detect-over-emission                       # scan every filing
    uv run detect-over-emission --accession 0001234-26-000001  # one filing

Two intended uses:

- Before re-reducing the corpus, enumerate the existing backlog so the heal
  can be targeted and its effect measured.
- As a regression check after the heal: once `reduce-corpus` re-derives the
  events layer with the subsumed-event drop in place, this should report
  zero findings. The exit code is non-zero when any are found, so it doubles
  as a scriptable gate.

Touches only the DB — no EDGAR fetch and no Anthropic call — so it needs only
the DB path, not the full ingest config. Output is JSON-line structured
events to stdout.
"""

from __future__ import annotations

import argparse
import sys

from filings_orchestrator.config import get_config_str
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import find_over_emitted_events


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="detect-over-emission",
        description="Report reduce events whose Item set is subsumed by another event.",
    )
    parser.add_argument(
        "--accession",
        help="Scan a single filing by accession number (default: every filing).",
    )
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    emit("over_emission_scan_started", accession=args.accession)
    findings = find_over_emitted_events(engine, args.accession)

    for finding in findings:
        emit("over_emission_found", **finding)

    affected = len({str(f["accession_number"]) for f in findings})
    emit("over_emission_scan_completed", findings=len(findings), filings_affected=affected)
    if findings:
        sys.exit(1)


if __name__ == "__main__":
    main()
