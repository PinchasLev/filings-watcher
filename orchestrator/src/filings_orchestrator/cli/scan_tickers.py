"""CLI: pull SEC's CIK→ticker mapping and refresh the local cache.

Fetches https://www.sec.gov/files/company_tickers.json (SEC's authoritative
mapping), upserts it into the cik_tickers table, then backfills the ticker
column on any filings rows that joined to a known CIK. Idempotent —
re-running is safe and only writes when values change.

Per ADR 0025, cik is the stable join key; ticker is mutable. This CLI
keeps the local mapping current; everything else (filings, classifications)
remains anchored on cik.

Run as a one-shot operator command for now; scheduling via a systemd
timer is tracked as a separate operational follow-up.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import httpx

from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    backfill_filings_tickers,
    upsert_cik_tickers,
)

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def main() -> None:
    started_at = datetime.now(UTC)
    emit("scan_tickers_started", started_at=started_at.isoformat())

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("scan_tickers_failed", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    headers = {"User-Agent": config.edgar_user_agent}
    try:
        response = httpx.get(_SEC_TICKERS_URL, headers=headers, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        emit(
            "scan_tickers_failed",
            error_class=type(exc).__name__,
            message=str(exc),
            url=_SEC_TICKERS_URL,
        )
        sys.exit(1)

    payload = response.json()
    mappings = _normalize_payload(payload)

    engine = open_engine(config.filings_db_path)
    written = upsert_cik_tickers(engine, mappings)
    backfilled = backfill_filings_tickers(engine)

    duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
    emit(
        "scan_tickers_completed",
        duration_ms=duration_ms,
        mappings_upserted=written,
        filings_backfilled=backfilled,
    )


def _normalize_payload(payload: object) -> list[tuple[str, str, str]]:
    """Turn SEC's company_tickers.json into (cik_padded, ticker, name) tuples.

    SEC publishes the file as an object indexed by integer-string keys
    ("0", "1", "2", ...) where each value carries {cik_str, ticker, title}.
    cik_str is an integer; we zero-pad to 10 digits to match the format
    every other CIK in the system uses (accession numbers, filings.cik).
    """
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object at the top level of company_tickers.json")
    out: list[tuple[str, str, str]] = []
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        cik_raw = entry.get("cik_str")
        ticker = entry.get("ticker")
        title = entry.get("title")
        if cik_raw is None or not ticker or not title:
            continue
        cik_padded = str(cik_raw).zfill(10)
        out.append((cik_padded, str(ticker), str(title)))
    return out


if __name__ == "__main__":
    main()
