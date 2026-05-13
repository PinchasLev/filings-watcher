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
