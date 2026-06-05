"""Resolve an ingested filing reference to a `Filing` with primary document.

Both ingest paths — the daily-index master.idx parser (ADR 0021) and the
Atom feed parser (ADR 0029) — yield the same minimum identifiers for a
filing: CIK, accession number, company name, form type, and filing date.
Neither path includes the primary document's filename. This module fetches
the per-accession filing-index HTML page and extracts the primary document
from its "Document Format Files" table, returning a fully-populated
`Filing`.

Kept separate from the parser modules so both ingest paths import the same
resolver without circularity, and so a future third ingest source (e.g.,
per-company submissions feed if reinstated) plugs in the same way.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from filings_orchestrator.edgar.client import EdgarClient
from filings_orchestrator.edgar.models import Filing

_FILING_INDEX_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accession_compact}/"
    "{accession}-index.html"
)

_PRIMARY_DOC_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accession_compact}/{document}"
)

# Match the iXBRL viewer wrapper EDGAR uses for newer filings:
#   /ix?doc=/Archives/edgar/data/<cik>/<accession-compact>/<filename>
_IXBRL_VIEWER_PREFIX = "/ix?doc="

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def resolve_filing(
    *,
    cik: str,
    accession_number: str,
    company_name: str,
    form: str,
    filed_at: str,
    client: EdgarClient,
) -> Filing:
    """Resolve a filing reference to a `Filing` including its primary document.

    Fetches the per-accession filing-index HTML page and parses the
    "Document Format Files" table to find the row whose Type matches the
    form. The Document column on that row carries the primary `.htm`
    filename (sometimes wrapped in EDGAR's iXBRL viewer URL, which is
    stripped here).

    `filed_at` accepts three shapes — compact YYYYMMDD (daily-index),
    ISO date YYYY-MM-DD, or ISO 8601 datetime YYYY-MM-DDThh:mm:ss±hh:mm
    (Atom feed) — and is normalized to a `date` for the returned model.
    """
    cik_unpadded = str(int(cik))
    accession_compact = accession_number.replace("-", "")
    index_url = _FILING_INDEX_URL_TEMPLATE.format(
        cik_unpadded=cik_unpadded,
        accession_compact=accession_compact,
        accession=accession_number,
    )
    page_html = client.get_text(index_url)
    primary_name = _extract_primary_document_name(page_html, form)
    primary_url = _PRIMARY_DOC_URL_TEMPLATE.format(
        cik_unpadded=cik_unpadded,
        accession_compact=accession_compact,
        document=primary_name,
    )
    return Filing(
        cik=cik,
        company_name=company_name,
        ticker=None,
        form=form,
        accession_number=accession_number,
        filing_date=_to_date(filed_at),
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


def _to_date(filed_at: str) -> date:
    """Normalize a filing-date string to a `date`.

    Accepts:
      - "20260515"             — daily-index compact YYYYMMDD
      - "2026-05-15"           — ISO date
      - "2026-06-05T09:05:09-04:00" — Atom feed ISO 8601 with offset
    """
    s = filed_at.strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    if _ISO_DATE_RE.match(s):
        return date.fromisoformat(s[:10])
    raise ValueError(f"unrecognised filed_at format: {filed_at!r}")
