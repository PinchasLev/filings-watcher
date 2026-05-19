"""Typed insert/get helpers over the filings DB.

Uses SQLAlchemy Core (no ORM) per ADR 0008 to keep SQL portable. Every
function takes an Engine and uses short-lived connections via `engine.begin()`
to commit each operation atomically.

The classifications API enforces ADR 0011's append-only rule:
`insert_classifications` writes new rows tagged with the classifier and
taxonomy versions present on the FilingClassification; rows are never updated
in place. The UNIQUE INDEX in the schema makes same-version writes idempotent
(silently skipped on conflict).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Engine, text

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    domain_for,
)
from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Filing


def upsert_filing(engine: Engine, filing: Filing) -> None:
    """Insert or update a filing's metadata. Body fields are left untouched."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO filings (
                    accession_number, cik, ticker, company_name, form,
                    filing_date, report_date, primary_document,
                    primary_document_url, items_json, fetched_at
                )
                VALUES (
                    :accession, :cik, :ticker, :company, :form,
                    :filing_date, :report_date, :primary_doc,
                    :primary_url, :items_json, :fetched_at
                )
                ON CONFLICT (accession_number) DO UPDATE SET
                    cik = excluded.cik,
                    ticker = excluded.ticker,
                    company_name = excluded.company_name,
                    form = excluded.form,
                    filing_date = excluded.filing_date,
                    report_date = excluded.report_date,
                    primary_document = excluded.primary_document,
                    primary_document_url = excluded.primary_document_url,
                    items_json = excluded.items_json,
                    fetched_at = excluded.fetched_at
                """
            ),
            {
                "accession": filing.accession_number,
                "cik": filing.cik,
                "ticker": filing.ticker,
                "company": filing.company_name,
                "form": filing.form,
                "filing_date": filing.filing_date.isoformat(),
                "report_date": filing.report_date.isoformat() if filing.report_date else None,
                "primary_doc": filing.primary_document,
                "primary_url": filing.primary_document_url,
                "items_json": json.dumps([item.model_dump() for item in filing.items]),
                "fetched_at": datetime.now(UTC).isoformat(),
            },
        )


def upsert_filing_document(engine: Engine, document: FilingDocument) -> None:
    """Insert or update a filing including its parsed body and per-Item sections.

    Pairs with upsert_filing — call this when you have the document body,
    otherwise call upsert_filing with just the metadata.
    """
    upsert_filing(engine, document.filing)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE filings
                   SET body_text       = :body_text,
                       body_size_bytes = :body_size,
                       sections_json   = :sections_json
                 WHERE accession_number = :accession
                """
            ),
            {
                "accession": document.filing.accession_number,
                "body_text": document.text,
                "body_size": document.raw_size_bytes,
                "sections_json": json.dumps([section.model_dump() for section in document.items]),
            },
        )


def insert_classifications(engine: Engine, result: FilingClassification) -> int:
    """Insert every classification from a FilingClassification.

    Returns the number of rows newly inserted (the rest, if any, were
    same-version duplicates rejected by the UNIQUE index).
    """
    inserted = 0
    rows = _classification_rows(result)
    sql = text(
        """
        INSERT OR IGNORE INTO classifications (
            accession_number, item_number, item_title,
            event_type, event_domain, is_material, confidence, reasoning,
            classifier_version, taxonomy_version, classified_at
        )
        VALUES (
            :accession, :item_number, :item_title,
            :event_type, :event_domain, :is_material, :confidence, :reasoning,
            :classifier_version, :taxonomy_version, :classified_at
        )
        """
    )
    with engine.begin() as conn:
        for row in rows:
            outcome = conn.execute(sql, row)
            inserted += outcome.rowcount
    return inserted


def _classification_rows(result: FilingClassification) -> list[dict[str, object]]:
    """Project a FilingClassification into the per-row dicts for INSERT."""
    classified_at = result.classified_at.isoformat()
    rows: list[dict[str, object]] = []

    def _row(
        item_number: str | None,
        item_title: str | None,
        classification: Classification,
    ) -> dict[str, object]:
        event_type = EventType(classification.event_type)
        return {
            "accession": result.accession_number,
            "item_number": item_number,
            "item_title": item_title,
            "event_type": event_type.value,
            "event_domain": domain_for(event_type).value,
            "is_material": 1 if classification.is_material else 0,
            "confidence": classification.confidence,
            "reasoning": classification.reasoning,
            "classifier_version": result.classifier_version,
            "taxonomy_version": result.taxonomy_version,
            "classified_at": classified_at,
        }

    for item in result.items:
        rows.append(_row(item.item_number, item.item_title, item.classification))
    if result.whole_filing is not None:
        rows.append(_row(None, None, result.whole_filing))
    return rows


def read_ingest_cursor(engine: Engine) -> tuple[str, str] | None:
    """Return the daily-index ingest cursor as (accession_number, filed_at).

    Returns None when the singleton row has not yet been written — meaning
    the next tick is the first one and should fetch only the current ET day.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT last_accession_number, last_filed_at FROM ingest_cursor WHERE id = 1")
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def advance_ingest_cursor(engine: Engine, accession_number: str, filed_at: str) -> None:
    """Upsert the singleton cursor row to (accession_number, filed_at).

    Called after each filing's classification has been persisted. The
    per-filing cadence (vs. per-batch) is the resume contract from ADR
    0021: a crashed tick leaves the cursor at the last good filing.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO ingest_cursor
                       (id, last_accession_number, last_filed_at, updated_at)
                VALUES (1, :accession, :filed_at, :updated_at)
                ON CONFLICT (id) DO UPDATE SET
                    last_accession_number = excluded.last_accession_number,
                    last_filed_at         = excluded.last_filed_at,
                    updated_at            = excluded.updated_at
                """
            ),
            {
                "accession": accession_number,
                "filed_at": filed_at,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )


def latest_classifications_for_filing(
    engine: Engine, accession_number: str
) -> list[dict[str, object]]:
    """Return the most-recent classification per (item, classifier_version).

    Returns dicts of column → value. Tests can assert against these directly;
    the CLI can use them for human-readable display.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT id, accession_number, item_number, item_title,
                       event_type, event_domain, is_material, confidence,
                       reasoning, classifier_version, taxonomy_version,
                       classified_at
                  FROM classifications
                 WHERE accession_number = :accession
                 ORDER BY classified_at DESC
                """
            ),
            {"accession": accession_number},
        )
        return [dict(row._mapping) for row in result]
