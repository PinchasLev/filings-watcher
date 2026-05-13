"""Typed data structures for SEC EDGAR filings.

8-K Item numbers (e.g., "2.02", "5.02") are defined by the SEC and identify
the category of material event being reported. The taxonomy is the
classification's natural label set: we don't invent it, we adopt it.

Reference: https://www.sec.gov/forms (Form 8-K General Instructions)
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class FilingItem(BaseModel):
    """One Item number disclosed in an 8-K filing.

    Example: FilingItem(number="5.02", title="Departure of Directors or
    Certain Officers; Election of Directors; ...")
    """

    number: str
    title: str | None = None


class Filing(BaseModel):
    """Metadata for one SEC filing.

    This is the metadata layer only — the body of the filing lives at
    `primary_document_url` and is fetched separately when needed.
    """

    cik: str
    company_name: str
    ticker: str | None = None
    form: str
    accession_number: str
    filing_date: date
    report_date: date | None = None
    primary_document: str
    primary_document_url: str
    items: list[FilingItem] = Field(default_factory=list)
