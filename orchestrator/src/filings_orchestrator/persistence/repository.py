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
from datetime import UTC, date, datetime
from typing import NamedTuple

from sqlalchemy import Connection, Engine, bindparam, text

from filings_orchestrator.change_detection import RiskFactorBlock
from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
    domain_for,
)
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.form4 import Form4Filing
from filings_orchestrator.edgar.models import Exhibit, Filing, FilingItem


def upsert_filing(engine: Engine, filing: Filing) -> None:
    """Insert or update a filing's metadata. Body fields are left untouched.

    `submitted_at` (the bitemporal valid-time half) is preserved across
    conflict updates: a re-ingest whose submitted_at is NULL does not
    wipe a previously-recorded non-NULL value. This protects the
    atom-path-set timestamp from being cleared by a later daily-index
    re-fetch of the same accession.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO filings (
                    accession_number, cik, ticker, company_name, form,
                    filing_date, report_date, primary_document,
                    primary_document_url, items_json, fetched_at,
                    submitted_at
                )
                VALUES (
                    :accession, :cik, :ticker, :company, :form,
                    :filing_date, :report_date, :primary_doc,
                    :primary_url, :items_json, :fetched_at,
                    :submitted_at
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
                    fetched_at = excluded.fetched_at,
                    submitted_at = COALESCE(excluded.submitted_at, filings.submitted_at)
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
                "submitted_at": filing.submitted_at,
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
                       sections_json   = :sections_json,
                       exhibits_json   = :exhibits_json
                 WHERE accession_number = :accession
                """
            ),
            {
                "accession": document.filing.accession_number,
                "body_text": document.text,
                "body_size": document.raw_size_bytes,
                "sections_json": json.dumps([section.model_dump() for section in document.items]),
                "exhibits_json": json.dumps([ex.model_dump() for ex in document.exhibits]),
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


def find_over_emitted_events(
    engine: Engine, accession_number: str | None = None
) -> list[dict[str, object]]:
    """Find latest-run events whose Item set is subsumed by another event's.

    The reduce stage should give every Item exactly one event, but the model
    sometimes also emits a smaller, separately-anchored event whose contributing
    Items are a subset of a larger event's — the same map output double-counted
    (the ADIL Certificate-of-Designation case). `reducer._drop_subsumed_events`
    now prevents this at write time; this read-only detector surfaces rows that
    predate the fix or slip through, for backfill targeting and as a regression
    check (it should return nothing once the corpus is re-reduced).

    An event's Item set is its `anchor_item_number` plus the Item numbers of its
    linked classifications. Within each filing's latest run, an event is reported
    when its non-empty Item set is a proper subset of another event's, or equals
    another's and has the larger `id` (so exact duplicates report all but the
    first). The predicate mirrors `_drop_subsumed_events`, with the event `id` as
    the stable tiebreak. Scoped to one filing when `accession_number` is given.

    Each finding is a dict: `accession_number`, `subsumed_event_id`,
    `subsumed_anchor`, `subsumed_items`, `container_event_id`,
    `container_anchor`, `container_items` (item lists sorted).
    """
    sql = """
        SELECT e.accession_number AS accession,
               e.id               AS event_id,
               e.anchor_item_number AS anchor,
               c.item_number      AS item_number
          FROM events e
          LEFT JOIN event_classifications ec ON ec.event_id = e.id
          LEFT JOIN classifications c ON c.id = ec.classification_id
         WHERE e.run_id = (
               SELECT MAX(run_id) FROM events e2
                WHERE e2.accession_number = e.accession_number
         )
    """
    params: dict[str, object] = {}
    if accession_number is not None:
        sql += " AND e.accession_number = :accession"
        params["accession"] = accession_number
    sql += " ORDER BY e.accession_number, e.id"

    # Reassemble each event's Item set from the (possibly multi-row) join.
    by_accession: dict[str, dict[int, dict[str, object]]] = {}
    with engine.begin() as conn:
        for row in conn.execute(text(sql), params):
            m = row._mapping
            accession = str(m["accession"])
            event_id = int(m["event_id"])
            event = by_accession.setdefault(accession, {}).setdefault(
                event_id, {"anchor": m["anchor"], "items": set()}
            )
            items = event["items"]
            assert isinstance(items, set)
            if m["anchor"] is not None:
                items.add(str(m["anchor"]))
            if m["item_number"] is not None:
                items.add(str(m["item_number"]))

    findings: list[dict[str, object]] = []
    for accession, events in by_accession.items():
        for event_id, event in events.items():
            item_set = event["items"]
            assert isinstance(item_set, set)
            if not item_set:
                continue
            container = _subsuming_event(event_id, item_set, events)
            if container is None:
                continue
            container_id, container_event = container
            container_items = container_event["items"]
            assert isinstance(container_items, set)
            findings.append(
                {
                    "accession_number": accession,
                    "subsumed_event_id": event_id,
                    "subsumed_anchor": event["anchor"],
                    "subsumed_items": sorted(item_set),
                    "container_event_id": container_id,
                    "container_anchor": container_event["anchor"],
                    "container_items": sorted(container_items),
                }
            )
    findings.sort(key=lambda f: (f["accession_number"], f["subsumed_event_id"]))
    return findings


def _subsuming_event(
    event_id: int, item_set: set[str], events: dict[int, dict[str, object]]
) -> tuple[int, dict[str, object]] | None:
    """Return the (id, event) that subsumes `event_id`, or None if it is maximal.

    Mirrors `_drop_subsumed_events`: a proper superset wins; among exact-equal
    sets the lower id wins, so duplicates report against their kept first.
    """
    for other_id, other in events.items():
        if other_id == event_id:
            continue
        other_items = other["items"]
        assert isinstance(other_items, set)
        if item_set < other_items or (item_set == other_items and other_id < event_id):
            return other_id, other
    return None


def list_exhibit_bearing_accessions(engine: Engine, *, limit: int | None = None) -> list[str]:
    """Return accessions that have stored EX-99 exhibits and a reconstructable body.

    The population for the exhibit A/B evaluation: filings whose `exhibits_json`
    holds at least one exhibit (a non-empty JSON array) and whose `body_text` is
    present, so `load_filing_document` can rebuild the document to classify both
    with and without exhibits. Ordered newest-first; `limit` bounds the sample.
    """
    sql = """
        SELECT accession_number
          FROM filings
         WHERE body_text IS NOT NULL
           AND exhibits_json IS NOT NULL
           AND exhibits_json NOT IN ('[]', '')
         ORDER BY filing_date DESC, accession_number
    """
    if limit is not None:
        sql += "\n         LIMIT :limit"
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"limit": limit} if limit is not None else {}).fetchall()
    return [str(r[0]) for r in rows]


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


def list_orphaned_accessions(
    engine: Engine,
    max_attempts: int | None = None,
    fetched_before: str | None = None,
) -> list[str]:
    """Return every filing that has a row but zero classifications.

    The orphan signature (ADR 0030): a `filings` row written before classify,
    then a mid-classify failure, leaves a filing the ingest dedup skips forever
    because it keys on row-existence, not on whether the map stage completed.
    These are the work set of the classify reconciler. Ordered newest-first so
    a heal processes the operator-visible order, mirroring
    `list_classified_accessions`.

    When `max_attempts` is given, filings whose `classify_attempts` have reached
    it are excluded — the dead-letter set the reconciler has given up on after
    repeated deterministic failures. `None` (the default) returns every orphan,
    abandoned ones included.

    When `fetched_before` (ISO 8601 UTC) is given, filings whose row was written
    at or after that cutoff are excluded. The live ingest path upserts the
    `filings` row *before* it classifies, so a just-fetched filing is almost
    certainly mid-classification in the live tick rather than a genuine failure.
    A grace cutoff keeps the reconciler from racing the live path over a filing
    that was never actually orphaned — which would burn duplicate LLM work and
    raise a false "healed" alert. `None` (the default) applies no grace window.
    """
    sql = """
        SELECT f.accession_number
          FROM filings f
          LEFT JOIN classifications c
                 ON c.accession_number = f.accession_number
         WHERE c.accession_number IS NULL
    """
    params: dict[str, object] = {}
    if max_attempts is not None:
        sql += " AND f.classify_attempts < :max_attempts"
        params["max_attempts"] = max_attempts
    if fetched_before is not None:
        sql += " AND f.fetched_at < :fetched_before"
        params["fetched_before"] = fetched_before
    sql += " ORDER BY f.filing_date DESC, f.accession_number"
    with engine.begin() as conn:
        return [str(row[0]) for row in conn.execute(text(sql), params)]


def increment_classify_attempt(engine: Engine, accession_number: str) -> int:
    """Bump a filing's deterministic-classification-failure counter; return the new value.

    Called by the classify reconciler when an attempt fails for a reason intrinsic
    to the filing (not a transient outage — see `is_retryable_error`). The returned
    count drives the abandonment threshold (ADR 0030). Returns 0 if the accession
    is absent (no row to bump).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE filings
                   SET classify_attempts = classify_attempts + 1
                 WHERE accession_number = :a
                """
            ),
            {"a": accession_number},
        )
        row = conn.execute(
            text("SELECT classify_attempts FROM filings WHERE accession_number = :a"),
            {"a": accession_number},
        ).fetchone()
    return int(row[0]) if row is not None else 0


def load_filing_document(engine: Engine, accession_number: str) -> FilingDocument | None:
    """Reconstruct a FilingDocument from the stored filing row.

    Rebuilds the map stage's input from the persisted body text and parsed
    sections, so the classify reconciler can re-run classification without
    re-fetching from EDGAR (filing text is immutable — ADR 0028/0030). Returns
    None when the filing is absent or has no stored body (a metadata-only row
    the reconciler cannot classify without a re-fetch, which is out of scope).
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT cik, ticker, company_name, form, filing_date, report_date,
                       primary_document, primary_document_url, items_json,
                       body_text, body_size_bytes, sections_json, submitted_at,
                       exhibits_json
                  FROM filings
                 WHERE accession_number = :a
                """
            ),
            {"a": accession_number},
        ).fetchone()
    if row is None:
        return None
    m = row._mapping
    if m["body_text"] is None:
        return None
    filing = Filing(
        cik=str(m["cik"]),
        company_name=str(m["company_name"]),
        ticker=m["ticker"],
        form=str(m["form"]),
        accession_number=accession_number,
        filing_date=date.fromisoformat(str(m["filing_date"])),
        report_date=date.fromisoformat(str(m["report_date"])) if m["report_date"] else None,
        primary_document=str(m["primary_document"]),
        primary_document_url=str(m["primary_document_url"]),
        items=[FilingItem(**item) for item in json.loads(m["items_json"])],
        submitted_at=m["submitted_at"],
    )
    sections = json.loads(m["sections_json"]) if m["sections_json"] else []
    exhibits = json.loads(m["exhibits_json"]) if m["exhibits_json"] else []
    body_text = str(m["body_text"])
    return FilingDocument(
        filing=filing,
        text=body_text,
        items=[ItemSection(**section) for section in sections],
        exhibits=[Exhibit(**ex) for ex in exhibits],
        raw_size_bytes=int(m["body_size_bytes"]) if m["body_size_bytes"] is not None else 0,
    )


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
            text(
                "SELECT cik, company_name, filing_date, form "
                "FROM filings WHERE accession_number = :a"
            ),
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
        form=str(filing._mapping["form"]),
        items=items,
        whole_filing=whole_filing,
        classified_at=classified_at,
        # The classifications table stores classifier_version (model+prompt hash)
        # but not the bare model; recover it from the prefix for the wrapper.
        model=classifier_version.split("+", 1)[0],
        classifier_version=classifier_version,
        taxonomy_version=taxonomy_version,
    )


def select_seen_accessions(engine: Engine, accessions: list[str]) -> set[str]:
    """Return the subset of `accessions` already present in the filings table.

    Both ingest paths (daily-index and Atom feed) use this to dedup candidate
    entries before doing any LLM-bound work. Correctness relies on the
    accession_number PK, not on any per-path cursor. One indexed lookup per
    tick scales to peak-day candidate volume comfortably.
    """
    if not accessions:
        return set()
    sql = text("SELECT accession_number FROM filings WHERE accession_number IN :accs").bindparams(
        bindparam("accs", expanding=True)
    )
    with engine.begin() as conn:
        rows = conn.execute(sql, {"accs": accessions}).fetchall()
    return {row[0] for row in rows}


def select_seen_insider_accessions(engine: Engine, accessions: list[str]) -> set[str]:
    """Return the subset of `accessions` already processed, per insider_filings.

    Form-4 ingest dedups on the accession PK before fetching/parsing, mirroring
    `select_seen_accessions` for the filings table. The anchor is insider_filings
    (one row per processed Form 4, written even for option-only or unparseable
    filings) — NOT insider_transactions, which lacks rows for filings with no
    non-derivative transactions and would re-fetch them forever. See ADR 0038.
    """
    if not accessions:
        return set()
    sql = text(
        "SELECT accession_number FROM insider_filings WHERE accession_number IN :accs"
    ).bindparams(bindparam("accs", expanding=True))
    with engine.begin() as conn:
        rows = conn.execute(sql, {"accs": accessions}).fetchall()
    return {row[0] for row in rows}


def read_form4_cursor(engine: Engine) -> tuple[str, str] | None:
    """Return the Form-4 ingest cursor as (accession_number, filed_at), or None.

    None means the singleton has never been written — the next tick is the
    first and should scan only the current ET day (no backfill), mirroring the
    8-K cursor's first-tick contract.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT last_accession_number, last_filed_at FROM form4_ingest_cursor WHERE id = 1"
            )
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def advance_form4_cursor(engine: Engine, accession_number: str, filed_at: str) -> None:
    """Upsert the singleton Form-4 cursor to (accession_number, filed_at).

    Called only after an index date is FULLY ingested (every Form 4 anchored in
    insider_filings). An aborted tick never reaches the advance, so the next run
    resumes from the incomplete date and fills the gap. See ADR 0038.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO form4_ingest_cursor
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


def insert_insider_filing(
    engine: Engine,
    *,
    accession_number: str,
    filed_at: str,
    ingested_at: str,
    filing: Form4Filing | None,
    non_derivative_count: int = 0,
    derivative_count: int = 0,
) -> None:
    """Upsert the per-filing envelope row — the Form-4 dedup anchor.

    Written for every PROCESSED Form 4: with `filing` set for a parsed document,
    or `filing=None` (parsed=0, null issuer/owner) for a fetched-but-unparseable
    document so it still anchors and is not re-fetched. Idempotent on the
    accession PK. See ADR 0038.
    """
    if filing is not None:
        row: dict[str, object | None] = {
            "accession_number": accession_number,
            "filed_at": filed_at,
            "period_of_report": filing.period_of_report,
            "issuer_cik": filing.issuer_cik,
            "issuer_name": filing.issuer_name,
            "issuer_ticker": filing.issuer_ticker,
            "owner_cik": filing.owner_cik,
            "owner_name": filing.owner_name,
            "is_director": 1 if filing.is_director else 0,
            "is_officer": 1 if filing.is_officer else 0,
            "is_ten_percent_owner": 1 if filing.is_ten_percent_owner else 0,
            "is_other": 1 if filing.is_other else 0,
            "officer_title": filing.officer_title,
            "is_10b5_1": 1 if filing.is_10b5_1 else 0,
            "not_subject_to_section16": 1 if filing.not_subject_to_section16 else 0,
            "parsed": 1,
            "non_derivative_count": non_derivative_count,
            "derivative_count": derivative_count,
            "ingested_at": ingested_at,
        }
    else:
        row = {
            "accession_number": accession_number,
            "filed_at": filed_at,
            "period_of_report": None,
            "issuer_cik": None,
            "issuer_name": None,
            "issuer_ticker": None,
            "owner_cik": None,
            "owner_name": None,
            "is_director": 0,
            "is_officer": 0,
            "is_ten_percent_owner": 0,
            "is_other": 0,
            "officer_title": None,
            "is_10b5_1": 0,
            "not_subject_to_section16": 0,
            "parsed": 0,
            "non_derivative_count": 0,
            "derivative_count": 0,
            "ingested_at": ingested_at,
        }
    sql = text(
        """
        INSERT INTO insider_filings (
            accession_number, filed_at, period_of_report,
            issuer_cik, issuer_name, issuer_ticker, owner_cik, owner_name,
            is_director, is_officer, is_ten_percent_owner, is_other, officer_title,
            is_10b5_1, not_subject_to_section16, parsed,
            non_derivative_count, derivative_count, ingested_at
        ) VALUES (
            :accession_number, :filed_at, :period_of_report,
            :issuer_cik, :issuer_name, :issuer_ticker, :owner_cik, :owner_name,
            :is_director, :is_officer, :is_ten_percent_owner, :is_other, :officer_title,
            :is_10b5_1, :not_subject_to_section16, :parsed,
            :non_derivative_count, :derivative_count, :ingested_at
        )
        ON CONFLICT (accession_number) DO UPDATE SET
            filed_at                 = excluded.filed_at,
            period_of_report         = excluded.period_of_report,
            issuer_cik               = excluded.issuer_cik,
            issuer_name              = excluded.issuer_name,
            issuer_ticker            = excluded.issuer_ticker,
            owner_cik                = excluded.owner_cik,
            owner_name               = excluded.owner_name,
            is_director              = excluded.is_director,
            is_officer               = excluded.is_officer,
            is_ten_percent_owner     = excluded.is_ten_percent_owner,
            is_other                 = excluded.is_other,
            officer_title            = excluded.officer_title,
            is_10b5_1                = excluded.is_10b5_1,
            not_subject_to_section16 = excluded.not_subject_to_section16,
            parsed                   = excluded.parsed,
            non_derivative_count     = excluded.non_derivative_count,
            derivative_count         = excluded.derivative_count,
            ingested_at              = excluded.ingested_at
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, row)


def insert_insider_transactions(
    engine: Engine, filing: Form4Filing, *, filed_at: str, ingested_at: str
) -> int:
    """Insert a parsed Form 4's non-derivative transactions. Idempotent on
    (accession_number, txn_seq); returns the number of rows newly inserted."""
    rows = _insider_rows(filing, filed_at=filed_at, ingested_at=ingested_at)
    if not rows:
        return 0
    sql = text(
        """
        INSERT OR IGNORE INTO insider_transactions (
            accession_number, txn_seq, filed_at, period_of_report,
            issuer_cik, issuer_name, issuer_ticker,
            owner_cik, owner_name, is_director, is_officer,
            is_ten_percent_owner, is_other, officer_title,
            transaction_date, security_title, transaction_code, acquired_disposed,
            shares, price_per_share, transaction_value, shares_owned_following,
            direct_or_indirect, is_10b5_1, not_subject_to_section16, ingested_at
        ) VALUES (
            :accession_number, :txn_seq, :filed_at, :period_of_report,
            :issuer_cik, :issuer_name, :issuer_ticker,
            :owner_cik, :owner_name, :is_director, :is_officer,
            :is_ten_percent_owner, :is_other, :officer_title,
            :transaction_date, :security_title, :transaction_code, :acquired_disposed,
            :shares, :price_per_share, :transaction_value, :shares_owned_following,
            :direct_or_indirect, :is_10b5_1, :not_subject_to_section16, :ingested_at
        )
        """
    )
    inserted = 0
    with engine.begin() as conn:
        for row in rows:
            inserted += conn.execute(sql, row).rowcount
    return inserted


def _insider_rows(
    filing: Form4Filing, *, filed_at: str, ingested_at: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for txn in filing.transactions:
        value = (
            txn.shares * txn.price_per_share
            if txn.shares is not None and txn.price_per_share is not None
            else None
        )
        rows.append(
            {
                "accession_number": filing.accession_number,
                "txn_seq": txn.txn_seq,
                "filed_at": filed_at,
                "period_of_report": filing.period_of_report,
                "issuer_cik": filing.issuer_cik,
                "issuer_name": filing.issuer_name,
                "issuer_ticker": filing.issuer_ticker,
                "owner_cik": filing.owner_cik,
                "owner_name": filing.owner_name,
                "is_director": 1 if filing.is_director else 0,
                "is_officer": 1 if filing.is_officer else 0,
                "is_ten_percent_owner": 1 if filing.is_ten_percent_owner else 0,
                "is_other": 1 if filing.is_other else 0,
                "officer_title": filing.officer_title,
                "transaction_date": txn.transaction_date,
                "security_title": txn.security_title,
                "transaction_code": txn.transaction_code,
                "acquired_disposed": txn.acquired_disposed,
                "shares": txn.shares,
                "price_per_share": txn.price_per_share,
                "transaction_value": value,
                "shares_owned_following": txn.shares_owned_following,
                "direct_or_indirect": txn.direct_or_indirect,
                "is_10b5_1": 1 if filing.is_10b5_1 else 0,
                "not_subject_to_section16": 1 if filing.not_subject_to_section16 else 0,
                "ingested_at": ingested_at,
            }
        )
    return rows


def insert_insider_derivative_transactions(
    engine: Engine, filing: Form4Filing, *, filed_at: str, ingested_at: str
) -> int:
    """Insert a parsed Form 4's derivative (option/warrant) transactions. Idempotent
    on (accession_number, txn_seq); returns the number of rows newly inserted."""
    rows = _derivative_rows(filing, filed_at=filed_at, ingested_at=ingested_at)
    if not rows:
        return 0
    sql = text(
        """
        INSERT OR IGNORE INTO insider_derivative_transactions (
            accession_number, txn_seq, filed_at, period_of_report,
            issuer_cik, issuer_name, issuer_ticker,
            owner_cik, owner_name, is_director, is_officer,
            is_ten_percent_owner, is_other, officer_title,
            security_title, conversion_exercise_price, transaction_date,
            transaction_code, acquired_disposed, shares, price_per_share,
            transaction_value, exercise_date, expiration_date,
            underlying_security_title, underlying_shares, shares_owned_following,
            direct_or_indirect, is_10b5_1, not_subject_to_section16, ingested_at
        ) VALUES (
            :accession_number, :txn_seq, :filed_at, :period_of_report,
            :issuer_cik, :issuer_name, :issuer_ticker,
            :owner_cik, :owner_name, :is_director, :is_officer,
            :is_ten_percent_owner, :is_other, :officer_title,
            :security_title, :conversion_exercise_price, :transaction_date,
            :transaction_code, :acquired_disposed, :shares, :price_per_share,
            :transaction_value, :exercise_date, :expiration_date,
            :underlying_security_title, :underlying_shares, :shares_owned_following,
            :direct_or_indirect, :is_10b5_1, :not_subject_to_section16, :ingested_at
        )
        """
    )
    inserted = 0
    with engine.begin() as conn:
        for row in rows:
            inserted += conn.execute(sql, row).rowcount
    return inserted


def _derivative_rows(
    filing: Form4Filing, *, filed_at: str, ingested_at: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for txn in filing.derivative_transactions:
        value = (
            txn.shares * txn.price_per_share
            if txn.shares is not None and txn.price_per_share is not None
            else None
        )
        rows.append(
            {
                "accession_number": filing.accession_number,
                "txn_seq": txn.txn_seq,
                "filed_at": filed_at,
                "period_of_report": filing.period_of_report,
                "issuer_cik": filing.issuer_cik,
                "issuer_name": filing.issuer_name,
                "issuer_ticker": filing.issuer_ticker,
                "owner_cik": filing.owner_cik,
                "owner_name": filing.owner_name,
                "is_director": 1 if filing.is_director else 0,
                "is_officer": 1 if filing.is_officer else 0,
                "is_ten_percent_owner": 1 if filing.is_ten_percent_owner else 0,
                "is_other": 1 if filing.is_other else 0,
                "officer_title": filing.officer_title,
                "security_title": txn.security_title,
                "conversion_exercise_price": txn.conversion_exercise_price,
                "transaction_date": txn.transaction_date,
                "transaction_code": txn.transaction_code,
                "acquired_disposed": txn.acquired_disposed,
                "shares": txn.shares,
                "price_per_share": txn.price_per_share,
                "transaction_value": value,
                "exercise_date": txn.exercise_date,
                "expiration_date": txn.expiration_date,
                "underlying_security_title": txn.underlying_security_title,
                "underlying_shares": txn.underlying_shares,
                "shares_owned_following": txn.shares_owned_following,
                "direct_or_indirect": txn.direct_or_indirect,
                "is_10b5_1": 1 if filing.is_10b5_1 else 0,
                "not_subject_to_section16": 1 if filing.not_subject_to_section16 else 0,
                "ingested_at": ingested_at,
            }
        )
    return rows


def daily_cost_usd(engine: Engine, day_utc: str) -> float:
    """Sum estimated_cost_usd for all llm_calls whose emitted_at falls on `day_utc`.

    `day_utc` is the UTC calendar day in ISO format (`YYYY-MM-DD`). UTC is the
    fixed boundary so the aggregate is stable across the operator's local
    timezone changes — pre-tick checks against this value are deterministic
    regardless of when the tick fires (ADR 0029). Returns 0.0 when no rows match
    (a fresh DB, or a day before this table existed).
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0)
                  FROM llm_calls
                 WHERE substr(emitted_at, 1, 10) = :day
                """
            ),
            {"day": day_utc},
        ).scalar_one()
    return float(result)


def daily_token_usage(engine: Engine, day_utc: str) -> dict[str, int]:
    """Aggregate token counts for all llm_calls on `day_utc` (UTC calendar day).

    Returns input / output / cache-read / cache-creation totals. Tokens are
    the engineering metric we control — prompt size, caching effectiveness,
    output shape — and the right unit for analyzing trends across pricing
    changes (which would skew a cost-only view). Cap enforcement reads
    `daily_cost_usd`; engineering analysis reads this.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                    COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
                  FROM llm_calls
                 WHERE substr(emitted_at, 1, 10) = :day
                """
            ),
            {"day": day_utc},
        ).one()
    return {
        "input_tokens": int(row[0]),
        "output_tokens": int(row[1]),
        "cache_read_tokens": int(row[2]),
        "cache_creation_tokens": int(row[3]),
    }


# --- Periodic-filing ingest (ADR 0042) ---------------------------------------
#
# periodic_filings is the dedup anchor + completeness ledger (one row per
# processed 10-K); filing_blocks holds its segmented risk-factor blocks;
# periodic_ingest_cursor is the resumable high-water mark. Mirrors the Form-4
# envelope/cursor pattern (ADR 0038).

_PERIODIC_SECTION_RISK_FACTORS = "risk_factors"


def select_seen_periodic_accessions(engine: Engine, accessions: list[str]) -> set[str]:
    """Return the subset of `accessions` already processed, per periodic_filings.

    Periodic ingest dedups on the accession PK before fetching/segmenting. The
    anchor is periodic_filings (one row per processed 10-K, written even when the
    document yielded no blocks), so a non-markup or section-less filing is not
    re-fetched forever.
    """
    if not accessions:
        return set()
    sql = text(
        "SELECT accession_number FROM periodic_filings WHERE accession_number IN :accs"
    ).bindparams(bindparam("accs", expanding=True))
    with engine.begin() as conn:
        rows = conn.execute(sql, {"accs": accessions}).fetchall()
    return {row[0] for row in rows}


def read_periodic_cursor(engine: Engine) -> tuple[str, str] | None:
    """Return the periodic-ingest cursor as (accession_number, filed_at), or None.

    None means the singleton has never been written — the first tick scans only
    the current ET day (no backfill), mirroring the 8-K/Form-4 cursor contract.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT last_accession_number, last_filed_at "
                "FROM periodic_ingest_cursor WHERE id = 1"
            )
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def advance_periodic_cursor(engine: Engine, accession_number: str, filed_at: str) -> None:
    """Upsert the singleton periodic cursor to (accession_number, filed_at).

    Called only after an index date is FULLY ingested. An aborted tick never
    reaches the advance, so the next run resumes from the incomplete date.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO periodic_ingest_cursor
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


def insert_periodic_filing(
    engine: Engine,
    *,
    accession_number: str,
    cik: str,
    company_name: str | None,
    form: str,
    filed_at: str,
    period_of_report: str | None,
    fiscal_year: int | None,
    parsed: bool,
    blocks: list[RiskFactorBlock],
    ingested_at: str,
    section: str = _PERIODIC_SECTION_RISK_FACTORS,
) -> None:
    """Store one processed 10-K: the envelope row plus its risk-factor blocks.

    Envelope and blocks are written in a single transaction so a filing is either
    fully stored or not at all. Idempotent: the envelope upserts on the accession
    PK and the filing's blocks for this section are replaced (delete + insert), so
    a re-segmentation of the same accession does not accumulate stale blocks.
    `parsed=False` records a fetched-but-unsegmentable filing (non-markup/oversized)
    with zero blocks, so it stays anchored and is not re-fetched.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO periodic_filings (
                    accession_number, cik, company_name, form, filed_at,
                    period_of_report, fiscal_year, parsed, block_count, ingested_at
                ) VALUES (
                    :accession_number, :cik, :company_name, :form, :filed_at,
                    :period_of_report, :fiscal_year, :parsed, :block_count, :ingested_at
                )
                ON CONFLICT (accession_number) DO UPDATE SET
                    cik              = excluded.cik,
                    company_name     = excluded.company_name,
                    form             = excluded.form,
                    filed_at         = excluded.filed_at,
                    period_of_report = excluded.period_of_report,
                    fiscal_year      = excluded.fiscal_year,
                    parsed           = excluded.parsed,
                    block_count      = excluded.block_count,
                    ingested_at      = excluded.ingested_at
                """
            ),
            {
                "accession_number": accession_number,
                "cik": cik,
                "company_name": company_name,
                "form": form,
                "filed_at": filed_at,
                "period_of_report": period_of_report,
                "fiscal_year": fiscal_year,
                "parsed": 1 if parsed else 0,
                "block_count": len(blocks),
                "ingested_at": ingested_at,
            },
        )
        conn.execute(
            text(
                "DELETE FROM filing_blocks "
                "WHERE accession_number = :accession_number AND section = :section"
            ),
            {"accession_number": accession_number, "section": section},
        )
        for block in blocks:
            conn.execute(
                text(
                    """
                    INSERT INTO filing_blocks (
                        accession_number, section, block_index,
                        heading, block_text, block_hash
                    ) VALUES (
                        :accession_number, :section, :block_index,
                        :heading, :block_text, :block_hash
                    )
                    """
                ),
                {
                    "accession_number": accession_number,
                    "section": section,
                    "block_index": block.index,
                    "heading": block.heading,
                    "block_text": block.text,
                    "block_hash": block.block_hash,
                },
            )


# --- Block embeddings (ADR 0042, PR 3) ---------------------------------------
#
# One vector per (block, model), stored as a JSON array of floats. select_...
# returns blocks that have no embedding yet for a given model, so the embed step
# is a resumable reconciler (already-embedded blocks are skipped).


class UnembeddedBlock(NamedTuple):
    """A block awaiting an embedding for some model: its key plus the text to embed."""

    accession_number: str
    section: str
    block_index: int
    block_text: str


def select_unembedded_blocks(engine: Engine, model_id: str, limit: int) -> list[UnembeddedBlock]:
    """Return up to `limit` blocks that have no embedding for `model_id` yet.

    A left join against filing_block_embeddings for this model finds the gap, so
    the embed step drains a backlog across runs and is idempotent (re-running
    embeds only what is still missing).
    """
    sql = text(
        """
        SELECT b.accession_number, b.section, b.block_index, b.block_text
          FROM filing_blocks b
          LEFT JOIN filing_block_embeddings e
            ON e.accession_number = b.accession_number
           AND e.section          = b.section
           AND e.block_index       = b.block_index
           AND e.model_id          = :model_id
         WHERE e.accession_number IS NULL
         ORDER BY b.accession_number, b.section, b.block_index
         LIMIT :limit
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(sql, {"model_id": model_id, "limit": limit}).fetchall()
    return [UnembeddedBlock(str(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in rows]


def insert_block_embeddings(
    engine: Engine,
    *,
    model_id: str,
    items: list[tuple[UnembeddedBlock, list[float]]],
    embedded_at: str,
) -> int:
    """Store embeddings for a batch of blocks under `model_id`. Returns the count.

    Idempotent on the (accession, section, block_index, model_id) key — a re-embed
    of the same block/model overwrites, so a retried batch does not duplicate.
    """
    sql = text(
        """
        INSERT INTO filing_block_embeddings (
            accession_number, section, block_index, model_id,
            dim, embedding_json, embedded_at
        ) VALUES (
            :accession_number, :section, :block_index, :model_id,
            :dim, :embedding_json, :embedded_at
        )
        ON CONFLICT (accession_number, section, block_index, model_id) DO UPDATE SET
            dim            = excluded.dim,
            embedding_json = excluded.embedding_json,
            embedded_at    = excluded.embedded_at
        """
    )
    with engine.begin() as conn:
        for block, vector in items:
            conn.execute(
                sql,
                {
                    "accession_number": block.accession_number,
                    "section": block.section,
                    "block_index": block.block_index,
                    "model_id": model_id,
                    "dim": len(vector),
                    "embedding_json": json.dumps(vector),
                    "embedded_at": embedded_at,
                },
            )
    return len(items)
