"""EDGAR daily-index ingest path.

The daily index is the "firehose" companion to the per-company submissions
feed already used by `recent_8k_filings`. For each business day EDGAR
publishes `master.<date>.idx` at:

    https://www.sec.gov/Archives/edgar/daily-index/<year>/QTR<n>/master.<date>.idx

The file is pipe-delimited (`CIK|Company Name|Form Type|Date Filed|File Name`)
and covers every form filed across every company that day. One fetch per
tick replaces N per-company fetches.

The index gives us each filing's accession number, CIK, company name, form
type, and submission path. The primary document name needed by
`fetch_filing_document` is resolved via `filing_resolver.resolve_filing`,
which is shared with the Atom feed ingest path (ADR 0029).

See ADR 0021.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel

from filings_orchestrator.edgar.client import EdgarClient

_DAILY_INDEX_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{date}.idx"
)

# Accession number pattern from the SGML archive filename. Example file path:
#   edgar/data/320193/0000320193-26-000045.txt
_ACCESSION_FROM_PATH_RE = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt$")


class DailyIndexEntry(BaseModel):
    """One row from `master.<date>.idx`."""

    cik: str
    company_name: str
    form: str
    filed_at: str
    accession_number: str
    submission_path: str


def daily_index_url(target: date) -> str:
    """Compose the master daily-index URL for `target`."""
    quarter = (target.month - 1) // 3 + 1
    return _DAILY_INDEX_URL_TEMPLATE.format(
        year=target.year,
        quarter=quarter,
        date=target.strftime("%Y%m%d"),
    )


def fetch_daily_index(target: date, client: EdgarClient) -> str:
    """Fetch the raw `master.<date>.idx` text for `target`."""
    return client.get_text(daily_index_url(target))


def parse_daily_index(text: str) -> list[DailyIndexEntry]:
    """Parse `master.<date>.idx` into typed entries.

    The file has a multi-line preamble (Description, Last Data Received,
    Comments, Anonymous FTP), the pipe-delimited header
    `CIK|Company Name|Form Type|Date Filed|File Name`, and a separator of
    dashes, followed by pipe-delimited rows. The preamble is dropped by
    skipping until the dash-separator line.
    """
    entries: list[DailyIndexEntry] = []
    body_started = False
    for line in text.splitlines():
        if not body_started:
            if line.startswith("---"):
                body_started = True
            continue
        if not line.strip() or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik_raw, company_name, form, filed_at, submission_path = (p.strip() for p in parts)
        accession_match = _ACCESSION_FROM_PATH_RE.search(submission_path)
        if not accession_match:
            continue
        cik_int = int(cik_raw)
        entries.append(
            DailyIndexEntry(
                cik=f"{cik_int:010d}",
                company_name=company_name,
                form=form,
                filed_at=filed_at,
                accession_number=accession_match.group(1),
                submission_path=submission_path,
            )
        )
    return entries


def filter_form(entries: list[DailyIndexEntry], form: str) -> list[DailyIndexEntry]:
    """Return entries whose `form` field equals `form` exactly.

    Exact match on purpose: 8-K amendments file as `8-K/A`, which are
    handled separately if at all (currently out of scope per ADR 0021).
    """
    return [e for e in entries if e.form == form]
