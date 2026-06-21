"""Per-filing processing pipeline shared by both ingest CLIs.

Resolves a filing reference to a `Filing`, fetches the body, classifies it
(with Anthropic-side retries per ADR 0021), persists the classification, and
reduces it into the filing-level events layer (ADR 0027 / ADR 0028).

Path-specific bookkeeping — cursor advance for the daily-index path, none
for the Atom path (ADR 0029) — is the caller's responsibility. This module
is the strictly per-filing slice that both paths share.
"""

from __future__ import annotations

import sys

from anthropic import AuthenticationError, BadRequestError, PermissionDeniedError
from sqlalchemy import Engine

from filings_orchestrator.alerting import ALERT, emit_alert
from filings_orchestrator.classify import (
    FilingClassification,
    classify_filing,
    reduce_filing,
    reducer_version,
)
from filings_orchestrator.classify.exhibits import render_exhibits, scan_red_flags
from filings_orchestrator.classify.retry import is_retryable_error, with_retries
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
from filings_orchestrator.persistence.taxonomy_snapshot import (
    TaxonomyIntegrityError,
    ensure_taxonomy_snapshot,
)

# Below this many characters of extracted text, an exhibit is treated as carrying
# no readable content — almost always an image/scanned attachment (page images in
# an HTML wrapper) rather than prose. Not zero: titles, page numbers, and other
# boilerplate survive extraction even on an all-image exhibit. See ADR 0033.
_MIN_EXHIBIT_TEXT_CHARS = 200


def verify_taxonomy(engine: Engine) -> None:
    """Reconcile the taxonomy snapshot at classify-CLI startup (ADR 0032).

    Cuts the current `TAXONOMY_VERSION` if unseen, else verifies the in-code and
    stored-row hashes against its anchor. Aborts the process (exit 2) on drift, so
    a classify run cannot proceed against a taxonomy whose version label no longer
    matches its choice-set — the same guard `migrate-db` applies, now also
    enforced at classify startup (outside a deploy). Call once per process, after
    `open_engine`, before classifying.
    """
    try:
        ensure_taxonomy_snapshot(engine)
    except TaxonomyIntegrityError as exc:
        emit("taxonomy_integrity_failed", message=str(exc))
        sys.exit(2)


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

    # Exhibit instrumentation (ADR 0031, measure-first): record how much exhibit
    # context the classifier received and whether the volume budget cut any of
    # it. Emitted whenever the filing carries EX-99 exhibits, so we can later
    # measure both how often we truncate and whether exhibits lift classification
    # (joining this against the classification's confidence/event_type).
    rendered = render_exhibits(document)
    if rendered.exhibit_count:
        emit(
            "exhibit_context",
            accession_number=accession_number,
            cik=cik,
            exhibit_count=rendered.exhibit_count,
            total_chars=rendered.total_chars,
            used_chars=rendered.used_chars,
            truncated=rendered.truncated,
            dropped_chars=rendered.dropped_chars,
        )
        # Don't let a filer bury bad news past the budget: scan the *dropped*
        # tail for curated adverse terms and raise an ALERT if any are there —
        # the classifier never saw that text, so it needs human eyes.
        if rendered.truncated:
            flags = scan_red_flags(rendered.dropped_text)
            if flags:
                emit_alert(
                    engine,
                    ALERT,
                    "Adverse content truncated from exhibit",
                    body=f"{accession_number}: exhibit content exceeded the volume "
                    f"budget and the dropped tail contains adverse terms "
                    f"({', '.join(flags)}). The classifier did not see all of it — "
                    f"review the full exhibit.",
                    dedup_key=f"exhibit_truncated_redflag:{accession_number}",
                    accession_number=accession_number,
                    terms=", ".join(flags),
                    dropped_chars=rendered.dropped_chars,
                )

        # Image/scanned exhibits (e.g. a press release furnished as page images
        # wrapped in HTML) extract to ~no text, so the classifier can't read them.
        # OCR is deferred (ADR 0033); for now emit a measure-first signal so we can
        # see how often a filing's content — especially a 6-K's, where the exhibit
        # IS the content — is locked in images a human should open instead.
        image_only = [
            ex.exhibit_type
            for ex in document.exhibits
            if len(ex.text.strip()) < _MIN_EXHIBIT_TEXT_CHARS
        ]
        if image_only:
            emit(
                "exhibit_no_extractable_text",
                accession_number=accession_number,
                cik=cik,
                form=document.filing.form,
                exhibits=image_only,
                count=len(image_only),
            )

    try:
        result = with_retries(
            lambda: classify_filing(document),
            log_context={
                "accession_number": accession_number,
                "cik": cik,
            },
        )
    except Exception as exc:
        # A classify call that propagates here has already exhausted in-call
        # retries, so the condition is sustained, not a blip — and the only
        # record of it otherwise is a `tick_failed` structured log, which never
        # reaches Discord. Raise an operator ALERT so a halted pipeline (drained
        # credit, bad key, upstream outage) is visible. The per-cause dedup_key
        # lets the drainer page once and re-page only once per repeat window
        # while it keeps firing, so a multi-day outage doesn't spam.
        _alert_on_classify_failure(engine, exc, accession_number=accession_number)
        raise
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


def _classify_failure_alert_params(exc: BaseException) -> tuple[str, str, str]:
    """Map a propagated classify failure to (cause, title, body) for an alert.

    The cause becomes the per-cause dedup_key suffix; the operator acts on each
    category differently. A drained credit balance and an auth/permission failure
    both halt classification until a human intervenes (top up / fix the key). A
    sustained upstream outage — retryable errors (rate-limit / 5xx / network)
    that still propagated after in-call retries — resolves on its own. Anything
    else is an unexpected failure worth eyes on the logs.
    """
    message = str(exc).lower()
    if isinstance(exc, BadRequestError) and "credit balance" in message:
        return (
            "anthropic_credit_exhausted",
            "Anthropic credit exhausted — classification halted",
            "A classification call was rejected because the Anthropic account's credit "
            "balance is too low. New filings will not be classified until the balance is "
            "topped up at console.anthropic.com; the next tick then resumes automatically.",
        )
    if isinstance(exc, AuthenticationError | PermissionDeniedError):
        return (
            "anthropic_auth_failed",
            "Anthropic auth failed — classification halted",
            "A classification call was rejected with an authentication or permission error. "
            "Check ANTHROPIC_API_KEY on the host; classification stays halted until it is fixed.",
        )
    if is_retryable_error(exc):
        return (
            "anthropic_upstream_outage",
            "Classification failing — Anthropic API outage",
            "Classification calls are still failing after in-call retries (rate limits, 5xx, "
            "or network). This is most likely a transient Anthropic outage and should resume "
            "on its own once upstream recovers.",
        )
    return (
        "classify_unexpected_error",
        "Classification failing — unexpected error",
        "A classification call failed with an unexpected error and the tick has stopped; "
        "filings are not being classified. Check the orchestrator logs.",
    )


def _alert_on_classify_failure(
    engine: Engine, exc: BaseException, *, accession_number: str
) -> None:
    """Raise the operator ALERT for a halted/failing classification pipeline."""
    cause, title, body = _classify_failure_alert_params(exc)
    emit_alert(
        engine,
        ALERT,
        title,
        body=body,
        dedup_key=f"classify_failure:{cause}",
        accession_number=accession_number,
        error_class=type(exc).__name__,
    )


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
        config_version=reducer_version(form=classification.form),
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
