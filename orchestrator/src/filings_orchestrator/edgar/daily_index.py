"""EDGAR daily-index ingest path.

The daily index is the "firehose" companion to the per-company submissions
feed already used by `recent_8k_filings`. For each business day EDGAR
publishes `master.<date>.idx` at:

    https://www.sec.gov/Archives/edgar/daily-index/<year>/QTR<n>/master.<date>.idx

The file is pipe-delimited (`CIK|Company Name|Form Type|Date Filed|File Name`)
and covers every form filed across every company that day. One fetch per
tick replaces N per-company fetches.

The index gives us each filing's accession number and the path to the SGML
submission archive (`*.txt`), but NOT the primary document name needed by
`fetch_filing_document`. To bridge that, this module resolves each new
filing's primary `.htm` document via the per-accession filing-index HTML
page:

    https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<accession>-index.html

The page contains a "Document Format Files" table whose row Type column
matches the filing's form (e.g., "8-K") and whose Document column links
the primary document. (The companion `index.json` endpoint exposes only
icon metadata, not form-type-to-document mapping.)

See ADR 0021.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup
from pydantic import BaseModel

from filings_orchestrator.edgar.client import EdgarClient
from filings_orchestrator.edgar.models import Filing

_DAILY_INDEX_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{date}.idx"
)
_FILING_INDEX_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accession_compact}/"
    "{accession}-index.html"
)

# Match the iXBRL viewer wrapper that EDGAR uses for newer filings:
#   /ix?doc=/Archives/edgar/data/<cik>/<accession-compact>/<filename>
_IXBRL_VIEWER_PREFIX = "/ix?doc="

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


def resolve_filing(entry: DailyIndexEntry, client: EdgarClient) -> Filing:
    """Resolve a daily-index entry to a Filing including its primary document.

    Fetches the per-accession filing-index HTML page and parses the
    "Document Format Files" table to find the row whose Type matches the
    entry's form. The Document column on that row carries the primary
    `.htm` filename (sometimes wrapped in EDGAR's iXBRL viewer URL).
    """
    cik_unpadded = str(int(entry.cik))
    accession_compact = entry.accession_number.replace("-", "")
    index_url = _FILING_INDEX_URL_TEMPLATE.format(
        cik_unpadded=cik_unpadded,
        accession_compact=accession_compact,
        accession=entry.accession_number,
    )
    page_html = client.get_text(index_url)
    primary_name = _extract_primary_document_name(page_html, entry.form)
    primary_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accession_compact}/{primary_name}"
    )

    filed_date = date.fromisoformat(_format_iso_date(entry.filed_at))
    return Filing(
        cik=entry.cik,
        company_name=entry.company_name,
        ticker=None,
        form=entry.form,
        accession_number=entry.accession_number,
        filing_date=filed_date,
        report_date=None,
        primary_document=primary_name,
        primary_document_url=primary_url,
        items=[],
    )


def _extract_primary_document_name(page_html: str, form: str) -> str:
    """Extract the primary document filename from a filing-index HTML page.

    The page's "Document Format Files" table has rows of:
        Seq | Description | Document | Type | Size
    where the Type column equals the form (`8-K`, `10-K`, etc.) for the
    primary document row. The Document column carries an `<a href>`
    pointing at either the file directly, or the iXBRL viewer wrapper
    (`/ix?doc=<path>`) which we strip.
    """
    soup = BeautifulSoup(page_html, "lxml")
    table = soup.find("table", attrs={"summary": "Document Format Files"})
    if table is None:
        raise LookupError("filing-index page missing 'Document Format Files' table")

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        type_text = cells[3].get_text(strip=True)
        if type_text != form:
            continue
        link = cells[2].find("a")
        if link is None or not link.get("href"):
            continue
        href = str(link["href"])
        if href.startswith(_IXBRL_VIEWER_PREFIX):
            href = href[len(_IXBRL_VIEWER_PREFIX) :]
        return href.rsplit("/", 1)[-1]

    raise LookupError(f"no Document Format Files row with Type={form!r} on filing-index page")


def _format_iso_date(yyyymmdd_or_iso: str) -> str:
    """Normalize a daily-index date to ISO YYYY-MM-DD.

    EDGAR has used two formats for the `Date Filed` column historically:
    `20260515` (compact) and `2026-05-15` (ISO). Tolerate both.
    """
    s = yyyymmdd_or_iso.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s
