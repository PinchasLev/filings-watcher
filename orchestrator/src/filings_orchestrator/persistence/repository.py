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

from sqlalchemy import Connection, Engine, bindparam, text

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
    domain_for,
)
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
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
