"""Per-filing processing pipeline shared by both ingest CLIs.

Resolves a filing reference to a `Filing`, fetches the body, classifies it
(with Anthropic-side retries per ADR 0021), persists the classification, and
reduces it into the filing-level events layer (ADR 0027 / ADR 0028).

Path-specific bookkeeping — cursor advance for the daily-index path, none
for the Atom path (ADR 0029) — is the caller's responsibility. This module
is the strictly per-filing slice that both paths share.
"""

from __future__ import annotations

from sqlalchemy import Engine

from filings_orchestrator.classify import (
    FilingClassification,
    classify_filing,
    reduce_filing,
    reducer_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.edgar import EdgarClient, FilingDocument, fetch_filing_document
from filings_orchestrator.edgar.filing_resolver import resolve_filing
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence.repository import (
    complete_run,
    create_run,
    insert_classifications,
    insert_events,
    lookup_ticker_by_cik,
    upsert_filing_document,
)


def process_one(
    *,
    client: EdgarClient,
    engine: Engine,
    cik: str,
    accession_number: str,
    company_name: str,
    form: str,
    filed_at: str,
    submitted_at: str | None = None,
) -> int:
    """Resolve → fetch body → classify (with retry) → persist → reduce.

    Returns the number of reduce failures for this filing (0 or 1). A
    classify or fetch failure raises and the caller is expected to fail
    the tick; a reduce failure does not raise (see `_reduce_one`), so the
    count is surfaced rather than propagated. The classification — the
    irreplaceable map output — is persisted before reduce runs.

    `submitted_at` is the precise EDGAR-side filing timestamp (ISO 8601
    with offset). The Atom ingest path passes this from the feed's
    `<updated>` element; the daily-index path passes None because the
    master.idx file is date-only. Migration 006 stores it on `filings`.
    """
    emit(
        "filing_fetched",
        accession_number=accession_number,
        cik=cik,
        form=form,
        filed_at=filed_at,
        company_name=company_name,
    )
    filing = resolve_filing(
        cik=cik,
        accession_number=accession_number,
        company_name=company_name,
        form=form,
        filed_at=filed_at,
        client=client,
    )
    # Populate the ticker from the local CIK→ticker mirror before persisting.
    # Returns the filing unchanged if cik_tickers has no entry — common for
    # private subsidiaries, trusts, or fresh installs before scan-tickers
    # has been run. See ADR 0025.
    ticker = lookup_ticker_by_cik(engine, filing.cik)
    update_fields: dict[str, object] = {}
    if ticker is not None:
        update_fields["ticker"] = ticker
    if submitted_at is not None:
        update_fields["submitted_at"] = submitted_at
    if update_fields:
        filing = filing.model_copy(update=update_fields)
    document = fetch_filing_document(filing, client)
    upsert_filing_document(engine, document)

    return classify_and_reduce(engine, document)


def classify_and_reduce(engine: Engine, document: FilingDocument) -> int:
    """Classify a fetched-or-stored document and reduce it into events.

    The map→reduce tail shared by the live ingest path (`process_one`, which
    resolves and fetches first) and the classify reconciler (`reclassify-orphans`
    per ADR 0030, which loads the document from stored body text). Classifies
    with Anthropic-side retries, persists the classification — the irreplaceable
    map output — before reduce runs, and reduces best-effort. Returns the reduce
    failure count (0 or 1); a classify or persist failure raises.
    """
    accession_number = document.filing.accession_number
    cik = document.filing.cik
    emit(
        "classification_started",
        accession_number=accession_number,
        cik=cik,
        items_count=len(document.items),
    )
    result = with_retries(
        lambda: classify_filing(document),
        log_context={
            "accession_number": accession_number,
            "cik": cik,
        },
    )
    inserted = insert_classifications(engine, result)

    emit(
        "classification_completed",
        accession_number=accession_number,
        cik=cik,
        classifications_inserted=inserted,
        classifier_version=result.classifier_version,
        taxonomy_version=result.taxonomy_version,
    )

    return _reduce_one(engine, result)


def _reduce_one(engine: Engine, classification: FilingClassification) -> int:
    """Reduce a freshly-classified filing into events as its own run (ADR 0028).

    Best-effort and non-fatal: the classification — the irreplaceable map output
    — is already persisted and the caller will advance cursor / dedup-mark
    regardless. Reduce is a derived, replayable stage, so a failure here is
    logged and counted, not propagated; a later `reduce-corpus` sweep closes
    the resulting gap in the events layer. Returns 1 on failure, 0 on success.
    """
    run_id = create_run(
        engine,
        stage="reduce",
        config_version=reducer_version(),
        taxonomy_version=classification.taxonomy_version,
    )
    try:
        events = with_retries(
            lambda: reduce_filing(classification),
            log_context={
                "accession_number": classification.accession_number,
                "cik": classification.cik,
                "stage": "reduce",
            },
        )
        written = insert_events(engine, events, run_id=run_id)
    except Exception as exc:
        complete_run(engine, run_id, status="failed")
        emit(
            "reduce_failed",
            accession_number=classification.accession_number,
            cik=classification.cik,
            run_id=run_id,
            error_class=type(exc).__name__,
            message=str(exc),
        )
        return 1

    complete_run(engine, run_id, status="succeeded")
    emit(
        "reduce_completed",
        accession_number=classification.accession_number,
        cik=classification.cik,
        run_id=run_id,
        events=written,
    )
    return 0
