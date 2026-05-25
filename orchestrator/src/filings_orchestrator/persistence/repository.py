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

from sqlalchemy import Connection, Engine, text

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
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


def upsert_cik_tickers(engine: Engine, mappings: list[tuple[str, str, str]]) -> int:
    """Upsert the SEC CIK→ticker mapping into cik_tickers.

    `mappings` is a list of (cik, ticker, company_name) tuples where cik
    is the zero-padded 10-digit form. SEC publishes CIK as an integer;
    the scan-tickers CLI is responsible for the zero-pad before calling
    this. Returns the number of rows touched (insert + update combined).

    See ADR 0025: cik is the stable join key; ticker is mutable.
    """
    if not mappings:
        return 0
    now = datetime.now(UTC).isoformat()
    sql = text(
        """
        INSERT INTO cik_tickers (cik, ticker, company_name, updated_at)
        VALUES (:cik, :ticker, :company_name, :updated_at)
        ON CONFLICT (cik) DO UPDATE SET
            ticker       = excluded.ticker,
            company_name = excluded.company_name,
            updated_at   = excluded.updated_at
        """
    )
    with engine.begin() as conn:
        for cik, ticker, name in mappings:
            conn.execute(
                sql,
                {"cik": cik, "ticker": ticker, "company_name": name, "updated_at": now},
            )
    return len(mappings)


def lookup_ticker_by_cik(engine: Engine, cik: str) -> str | None:
    """Return the current ticker for the given (zero-padded) CIK, or None.

    None means either (a) we have not yet ingested SEC's mapping, or
    (b) the CIK has no public ticker — common for private subsidiaries,
    trusts, and other registrants that don't trade.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT ticker FROM cik_tickers WHERE cik = :cik"),
            {"cik": cik},
        ).fetchone()
    return str(row[0]) if row else None


def backfill_filings_tickers(engine: Engine) -> int:
    """Populate filings.ticker for rows where it is NULL via a join on cik.

    Run by scan-tickers after a fresh ingest of cik_tickers so existing
    filings retroactively gain their ticker. Idempotent: only touches
    rows whose ticker is currently NULL, so re-running is safe.
    Returns the number of rows updated.
    """
    sql = text(
        """
        UPDATE filings
           SET ticker = (SELECT ticker FROM cik_tickers WHERE cik = filings.cik)
         WHERE ticker IS NULL
           AND EXISTS (SELECT 1 FROM cik_tickers WHERE cik = filings.cik)
        """
    )
    with engine.begin() as conn:
        result = conn.execute(sql)
    return int(result.rowcount or 0)


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


def create_run(
    engine: Engine,
    *,
    stage: str,
    config_version: str,
    taxonomy_version: str,
    model: str | None = None,
    source_run_id: int | None = None,
    status: str = "running",
    notes: str | None = None,
) -> int:
    """Insert a runs-ledger row and return its run_id (ADR 0028).

    A run is one processing pass of a single stage. `run_id` is the monotonic
    versioning axis: every deliberate (re-)run is a new run, so this always
    inserts — there is no dedup on `config_version`, because identical
    configuration may still yield different LLM output. The caller marks
    completion with `complete_run`.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO runs (
                    stage, model, config_version, taxonomy_version,
                    source_run_id, status, started_at, notes
                )
                VALUES (
                    :stage, :model, :config_version, :taxonomy_version,
                    :source_run_id, :status, :started_at, :notes
                )
                """
            ),
            {
                "stage": stage,
                "model": model,
                "config_version": config_version,
                "taxonomy_version": taxonomy_version,
                "source_run_id": source_run_id,
                "status": status,
                "started_at": datetime.now(UTC).isoformat(),
                "notes": notes,
            },
        )
    return int(result.lastrowid or 0)


def complete_run(engine: Engine, run_id: int, *, status: str = "succeeded") -> None:
    """Mark a run finished with a terminal status (succeeded / failed / partial)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE runs
                   SET status = :status, finished_at = :finished_at
                 WHERE run_id = :run_id
                """
            ),
            {"status": status, "finished_at": datetime.now(UTC).isoformat(), "run_id": run_id},
        )


def insert_events(engine: Engine, filing_events: FilingEvents, *, run_id: int) -> int:
    """Write a filing's reduce output as event rows under `run_id`, linking each
    event to the classification rows it collated.

    Idempotent within a run: the UNIQUE (run_id, accession, anchor) index makes
    re-insertion a no-op (INSERT OR IGNORE), so a resumed run skips finished
    work. Across runs, every deliberate re-run is a new run_id and is preserved
    (ADR 0028). Returns the number of event rows newly inserted.

    The event→classification join is resolved by mapping each event's
    contributing Item numbers to the latest classification row for
    (accession, item). Until classify runs carry run_ids, resolution is by Item
    alone; provenance tightens to a source run when that lands.
    """
    inserted = 0
    accession = filing_events.accession_number
    event_sql = text(
        """
        INSERT OR IGNORE INTO events (
            run_id, accession_number, anchor_item_number,
            event_type, event_domain, is_material, confidence, summary
        )
        VALUES (
            :run_id, :accession, :anchor, :event_type, :event_domain,
            :is_material, :confidence, :summary
        )
        """
    )
    link_sql = text(
        """
        INSERT OR IGNORE INTO event_classifications (event_id, classification_id)
        VALUES (:event_id, :classification_id)
        """
    )
    with engine.begin() as conn:
        for event in filing_events.events:
            event_type = EventType(event.event_type)
            outcome = conn.execute(
                event_sql,
                {
                    "run_id": run_id,
                    "accession": accession,
                    "anchor": event.anchor_item_number,
                    "event_type": event_type.value,
                    "event_domain": domain_for(event_type).value,
                    "is_material": 1 if event.is_material else 0,
                    "confidence": event.confidence,
                    "summary": event.summary,
                },
            )
            if outcome.rowcount == 0:
                # Already present in this run (idempotent retry); skip linking.
                continue
            inserted += 1
            event_id = int(outcome.lastrowid or 0)
            for class_id in _contributing_classification_ids(
                conn, accession, event.contributing_item_numbers
            ):
                conn.execute(link_sql, {"event_id": event_id, "classification_id": class_id})
    return inserted


def _contributing_classification_ids(
    conn: Connection, accession: str, item_numbers: list[str]
) -> list[int]:
    """Resolve Item numbers to the latest classification row id per (accession, item).

    An Item with no classification row is skipped — the event is still written;
    only that one join link is omitted.
    """
    ids: list[int] = []
    for item in item_numbers:
        row = conn.execute(
            text(
                """
                SELECT id FROM classifications
                 WHERE accession_number = :accession AND item_number = :item
                 ORDER BY classified_at DESC, id DESC
                 LIMIT 1
                """
            ),
            {"accession": accession, "item": item},
        ).fetchone()
        if row is not None:
            ids.append(int(row[0]))
    return ids


def latest_run_events_for_filing(engine: Engine, accession_number: str) -> list[dict[str, object]]:
    """Return the events of the filing's latest run, selected wholesale (ADR 0028).

    Current view = the complete output of the run with the greatest run_id that
    produced events for this filing — never a per-anchor maximum. Selecting by
    run as a unit ensures an anchor the latest run did not emit cannot surface
    from an older, larger run.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT id, run_id, accession_number, anchor_item_number,
                       event_type, event_domain, is_material, confidence, summary
                  FROM events
                 WHERE accession_number = :accession
                   AND run_id = (
                       SELECT MAX(run_id) FROM events WHERE accession_number = :accession
                   )
                 ORDER BY anchor_item_number
                """
            ),
            {"accession": accession_number},
        )
        return [dict(row._mapping) for row in result]


def list_classified_accessions(engine: Engine) -> list[str]:
    """Return every accession number that has at least one classification.

    The set of filings the reduce stage can operate on. Ordered by filing_date
    so a corpus reduce processes newest-first (the operator-visible order)."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT DISTINCT c.accession_number
                  FROM classifications c
                  JOIN filings f ON f.accession_number = c.accession_number
                 ORDER BY f.filing_date DESC, c.accession_number
                """
            )
        )
        return [str(row[0]) for row in result]


def load_latest_filing_classification(
    engine: Engine, accession_number: str
) -> FilingClassification | None:
    """Reconstruct a FilingClassification from the stored rows for one filing.

    Reads the filing metadata plus the latest classification per Item (the most
    recent `classified_at` per `item_number`, whole-filing row included), so the
    reduce stage consumes the current per-Item judgments. Returns None when the
    filing is absent or has no classifications yet. This is the map output the
    reduce stage replays over, decoupled from the live classify path.
    """
    with engine.begin() as conn:
        filing = conn.execute(
            text("SELECT cik, company_name, filing_date FROM filings WHERE accession_number = :a"),
            {"a": accession_number},
        ).fetchone()
        if filing is None:
            return None
        rows = conn.execute(
            text(
                """
                WITH ranked AS (
                    SELECT item_number, item_title, event_type, is_material,
                           confidence, reasoning, classifier_version, taxonomy_version,
                           classified_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(item_number, '')
                               ORDER BY classified_at DESC, id DESC
                           ) AS rn
                      FROM classifications
                     WHERE accession_number = :a
                )
                SELECT * FROM ranked WHERE rn = 1
                """
            ),
            {"a": accession_number},
        ).fetchall()

    if not rows:
        return None

    items: list[ItemClassification] = []
    whole_filing: Classification | None = None
    classifier_version = ""
    taxonomy_version = ""
    classified_at = datetime.now(UTC)
    for row in rows:
        m = row._mapping
        classification = Classification(
            event_type=EventType(m["event_type"]),
            is_material=bool(m["is_material"]),
            confidence=float(m["confidence"]),
            reasoning=str(m["reasoning"]),
        )
        if m["item_number"] is None:
            whole_filing = classification
        else:
            items.append(
                ItemClassification(
                    item_number=str(m["item_number"]),
                    item_title=m["item_title"],
                    classification=classification,
                )
            )
        classifier_version = str(m["classifier_version"])
        taxonomy_version = str(m["taxonomy_version"])
        classified_at = datetime.fromisoformat(str(m["classified_at"]))

    return FilingClassification(
        accession_number=accession_number,
        cik=str(filing._mapping["cik"]),
        company_name=str(filing._mapping["company_name"]),
        filing_date=str(filing._mapping["filing_date"]),
        items=items,
        whole_filing=whole_filing,
        classified_at=classified_at,
        # The classifications table stores classifier_version (model+prompt hash)
        # but not the bare model; recover it from the prefix for the wrapper.
        model=classifier_version.split("+", 1)[0],
        classifier_version=classifier_version,
        taxonomy_version=taxonomy_version,
    )
