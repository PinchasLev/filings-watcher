"""High-level operations over EDGAR: ticker lookup, recent filings fetch."""

from __future__ import annotations

from datetime import date
from typing import Any

from filings_orchestrator.edgar.client import EdgarClient
from filings_orchestrator.edgar.models import Filing, FilingItem

_TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"


def ticker_to_cik(ticker: str, client: EdgarClient) -> tuple[str, str]:
    """Resolve a stock ticker to (cik_padded, company_name).

    The SEC publishes the full ticker-to-CIK index as one JSON file. For v0
    we fetch it on demand; a real deployment should cache it (it changes
    infrequently — daily at most).
    """
    ticker_upper = ticker.upper()
    payload = client.get_json(_TICKER_INDEX_URL)
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("ticker", "").upper() == ticker_upper:
            cik_int = int(entry["cik_str"])
            return f"{cik_int:010d}", str(entry["title"])
    raise LookupError(f"ticker not found in EDGAR index: {ticker}")


def recent_8k_filings(
    ticker: str,
    client: EdgarClient,
    limit: int = 20,
) -> list[Filing]:
    """Return the most recent 8-K filings for a ticker, newest first.

    Pulls the company's submissions feed, filters to form == "8-K", and
    returns up to `limit` entries. Does not fetch the filing bodies; that's
    a separate step.
    """
    cik, company_name = ticker_to_cik(ticker, client)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    submissions = client.get_json(submissions_url)
    recent = submissions.get("filings", {}).get("recent", {})
    return _parse_recent_filings(
        recent=recent,
        cik=cik,
        company_name=company_name,
        ticker=ticker.upper(),
        limit=limit,
        form_filter="8-K",
    )


def load_ticker_index(client: EdgarClient) -> dict[str, tuple[str, str]]:
    """Fetch the SEC ticker index once and return {TICKER: (cik_padded, company_name)}.

    For bulk resolution (a coverage backfill of many tickers) — fetching the ~10 MB index
    once beats `ticker_to_cik` per name.
    """
    payload = client.get_json(_TICKER_INDEX_URL)
    index: dict[str, tuple[str, str]] = {}
    for entry in payload.values():
        if not isinstance(entry, dict) or "ticker" not in entry:
            continue
        cik_int = int(entry["cik_str"])
        index[str(entry["ticker"]).upper()] = (f"{cik_int:010d}", str(entry["title"]))
    return index


def periodic_filings_for_cik(
    cik: str,
    company_name: str,
    client: EdgarClient,
    *,
    form: str = "10-K",
    limit: int = 2,
    max_pages: int = 8,
) -> list[Filing]:
    """The `limit` most recent `form` filings for a CIK, newest first.

    Reads the submissions `recent` block, then pages the older `filings.files` chunks when
    `recent` holds fewer than `limit` of the form. This matters for firehose filers:
    JPMorgan files ~25k documents a year, so its `recent` window spans under a year and
    contains only the single latest annual 10-K — the prior years live in the paged files
    (JPMorgan has 68). Pages are read most-recent-chunk first and capped at `max_pages`
    (an annual filing's prior year is within the first page or two); results are then
    sorted newest-first and de-duplicated, so ordering of the `files` array is irrelevant.
    """
    subs = client.get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    name = str(subs.get("name") or company_name)
    meta = subs.get("filings", {})
    out = _parse_recent_filings(meta.get("recent", {}), cik, name, "", limit, form)
    if len(out) < limit:
        pages = sorted(
            meta.get("files", []), key=lambda f: str(f.get("filingTo", "")), reverse=True
        )
        for page_meta in pages[:max_pages]:
            page = client.get_json(f"https://data.sec.gov/submissions/{page_meta['name']}")
            out += _parse_recent_filings(page, cik, name, "", limit, form)
            if len(out) >= limit:
                break

    seen: set[str] = set()
    unique: list[Filing] = []
    for filing in sorted(out, key=lambda f: f.filing_date, reverse=True):
        if filing.accession_number in seen:
            continue
        seen.add(filing.accession_number)
        unique.append(filing)
    return unique[:limit]


def _parse_recent_filings(
    recent: dict[str, Any],
    cik: str,
    company_name: str,
    ticker: str,
    limit: int,
    form_filter: str,
) -> list[Filing]:
    """Project the EDGAR `recent` block (columnar) into row-oriented Filings."""
    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_documents = recent.get("primaryDocument", [])
    items_lists = recent.get("items", [])

    cik_unpadded = str(int(cik))
    results: list[Filing] = []
    for i, form in enumerate(forms):
        if form != form_filter:
            continue
        accession = accession_numbers[i]
        accession_compact = accession.replace("-", "")
        primary_document = primary_documents[i]
        primary_document_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_unpadded}/{accession_compact}/{primary_document}"
        )
        items = _parse_items_field(items_lists[i] if i < len(items_lists) else "")
        report_date_str = report_dates[i] if i < len(report_dates) else ""
        results.append(
            Filing(
                cik=cik,
                company_name=company_name,
                ticker=ticker,
                form=form,
                accession_number=accession,
                filing_date=date.fromisoformat(filing_dates[i]),
                report_date=date.fromisoformat(report_date_str) if report_date_str else None,
                primary_document=primary_document,
                primary_document_url=primary_document_url,
                items=items,
            )
        )
        if len(results) >= limit:
            break
    return results


def _parse_items_field(raw: str) -> list[FilingItem]:
    """Parse the EDGAR `items` string for an 8-K into FilingItem objects.

    EDGAR encodes 8-K Items as a comma-separated list of dotted numbers,
    e.g., "2.02,9.01". Item titles are not included in the metadata feed.
    """
    if not raw:
        return []
    return [FilingItem(number=part.strip()) for part in raw.split(",") if part.strip()]
