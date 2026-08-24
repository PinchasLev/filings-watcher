"""CLI: track whether flagged risks materialize in subsequent 8-K/6-K filings (Phase 2b).

The Risk Radar's first "track update." A resumable reconciler: finds flagged specific risks
that need a realization (re)check — no verdict yet, or a not-realized verdict whose company
has filed a new material 8-K since — and for each, asks Claude whether any subsequent 8-K
DIRECTLY realizes that risk (a strict, evidenced, segment-to-risk line). The verdict is
stored with a checked_through watermark, so the risk keeps being tracked as filings land
rather than being judged once and frozen.

Depends on the specificity stage (only specific risks are tracked) and on ingested 8-K/6-K
events. LLM-bearing, so it is cost-capped like the judge (ADR 0029). Bounded per run via
--limit; a backlog drains across runs. Output is JSON-line events to stdout.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import UTC, datetime
from functools import partial

from opentelemetry import trace
from sqlalchemy import Engine

from filings_orchestrator.alerting import ALERT, emit_alert
from filings_orchestrator.change_detection import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_REALIZATION_MODEL,
    RealizationEvent,
    build_realization_judge,
    judge_realization,
    judge_version,
    realization_evidence_is_grounded,
    realization_is_grounded,
    realization_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import MissingConfigError, get_config_int, load_config
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    insert_risk_realization,
    load_filing_document,
    load_subsequent_material_events,
    select_risks_needing_realization,
)

# Risks checked per run — one LLM call each; a backlog drains across runs.
_DEFAULT_MAX_PER_RUN = 100
# Subsequent 8-K events offered to the judge per risk (newest realizations still fit).
_MAX_EVENTS = 20
# Per-event disclosure text fed to the judge to quote from. The material 8-K disclosure sits
# in the anchored Item body; this bounds one event's contribution so a firehose of candidates
# cannot blow the context (the judge only needs the sentence stating the consequence).
_MAX_EVENT_SOURCE_CHARS = 4000


def _event_source_text(engine: Engine, accession_number: str, item: str) -> str:
    """The disclosure text the judge quotes from for one candidate 8-K: the anchored Item body
    (bounded), falling back to all Item bodies then the document text when the anchor is absent.
    Empty when the body was not stored, in which case the judge falls back to the summary."""
    doc = load_filing_document(engine, accession_number)
    if doc is None:
        return ""
    anchored = [s.text for s in doc.items if item and s.number == item and s.text.strip()]
    parts = anchored or [s.text for s in doc.items if s.text.strip()] or [doc.text]
    return "\n\n".join(p.strip() for p in parts if p.strip())[:_MAX_EVENT_SOURCE_CHARS]


def realization_pass(
    engine: Engine,
    model: object,
    *,
    model_name: str,
    judge_ver: str,
    realization_ver: str,
    limit: int,
    recheck_not_realized: bool = False,
) -> dict[str, int]:
    """Judge up to `limit` risks needing a realization (re)check. A risk whose call keeps
    failing is left for the next run (no row stored)."""
    realized = not_realized = failed = 0
    quote_rejected = evidence_rejected = 0
    risks = select_risks_needing_realization(
        engine,
        judge_version=judge_ver,
        realization_version=realization_ver,
        limit=limit,
        recheck_not_realized=recheck_not_realized,
    )
    # Selection orders by accession, so a 10-K's risks are judged back-to-back and the events
    # block one of them writes to the cache is the block the next one reads. A 10-K that
    # contributes a single risk has no second reader, and paying the write premium for an entry
    # nothing reads costs more than sending the block plainly.
    risks_per_filing = Counter(r.accession_number for r in risks)
    for risk in risks:
        events = load_subsequent_material_events(
            engine, cik=risk.cik, after=risk.filed_at, limit=_MAX_EVENTS
        )
        if not events:
            continue  # raced away — the gap required subsequent events

        candidates = [
            RealizationEvent(
                e.filing_date,
                e.event_type,
                e.item,
                e.summary,
                _event_source_text(engine, e.accession_number, e.item),
            )
            for e in events
        ]
        try:
            verdict = with_retries(
                partial(
                    judge_realization,
                    model,
                    risk_text=risk.risk_text,
                    risk=risk.explanation,
                    events=candidates,
                    model_name=model_name,
                    accession_number=risk.accession_number,
                    cache_shared_prefix=risks_per_filing[risk.accession_number] > 1,
                ),
                log_context={"accession": risk.accession_number, "change_seq": risk.change_seq},
            )
        except Exception as exc:
            failed += 1
            emit(
                "realization_failed",
                accession_number=risk.accession_number,
                change_seq=risk.change_seq,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        # Realized only when the model's event_index maps to a real candidate (strict).
        idx = verdict.event_index
        realizing = (
            events[idx - 1] if (verdict.is_realized and idx and 1 <= idx <= len(events)) else None
        )
        # The claim exactly as the judge made it, kept whether or not a gate goes on to refuse
        # it. A refused claim is stored with is_realized=0 and never reaches the page, but it is
        # the only record of what the gate turned down — without it a rejection is
        # indistinguishable in the data from the judge simply finding nothing.
        claimed = realizing
        rejected_by: str | None = None
        rejected_detail = ""
        # Bounded-operator gate: a realized verdict must cite a quote that appears verbatim in the
        # realizing 8-K's disclosure. An ungrounded "quote" (fabricated or paraphrased) is
        # downgraded to not-realized, so an unverifiable materialization is never surfaced.
        if realizing is not None and not realization_is_grounded(verdict, candidates):
            emit(
                "realization_quote_ungrounded",
                accession_number=risk.accession_number,
                change_seq=risk.change_seq,
                realizing_accession=realizing.accession_number,
            )
            quote_rejected += 1
            # No detail to record: the gate's whole objection is that the stored quote is not a
            # span of the realizing filing, and the quote and that filing are both kept below.
            rejected_by = "quote"
            realizing = None
        # Second gate, on the sentence the page actually renders. A verified quote proves the
        # citation is real; it does not stop the surrounding sentence from asserting a title,
        # figure, or attribution the filing never made. Evidence that cannot be traced back to
        # the quote, the risk factor, or the disclosure is not shown.
        if realizing is not None:
            grounded, unsupported = realization_evidence_is_grounded(
                verdict, candidates, risk_text=risk.risk_text
            )
            if not grounded:
                emit(
                    "realization_evidence_ungrounded",
                    accession_number=risk.accession_number,
                    change_seq=risk.change_seq,
                    realizing_accession=realizing.accession_number,
                    unsupported=", ".join(unsupported),
                )
                evidence_rejected += 1
                rejected_by = "evidence"
                rejected_detail = ", ".join(unsupported)
                realizing = None
        is_realized = realizing is not None
        insert_risk_realization(
            engine,
            risk=risk,
            judge_version=judge_ver,
            realization_version=realization_ver,
            is_realized=is_realized,
            realizing_accession=claimed.accession_number if claimed else None,
            realizing_event_type=claimed.event_type if claimed else None,
            realizing_item=(claimed.item or None) if claimed else None,
            evidence=verdict.evidence if claimed else "",
            quote=verdict.quote if claimed else "",
            confidence=verdict.confidence,
            checked_through=max(e.filing_date for e in events),
            judged_at=datetime.now(UTC).isoformat(),
            rejected_by=rejected_by,
            rejected_detail=rejected_detail,
        )
        if is_realized:
            realized += 1
        else:
            not_realized += 1
    return {
        "realized": realized,
        "not_realized": not_realized,
        "failed": failed,
        "candidates": len(risks),
        # Verdicts the model returned as realized and a gate downgraded. Folded into
        # not_realized they are indistinguishable from a genuine miss, which is how an
        # over-strict gate stayed hidden; counted here, a gate that starts eating true
        # positives shows up in the run summary.
        "quote_rejected": quote_rejected,
        "evidence_rejected": evidence_rejected,
    }


def main() -> None:
    setup_otel()
    import argparse

    parser = argparse.ArgumentParser(
        prog="track-realizations",
        description="Track whether flagged risks materialize in subsequent 8-K/6-K filings.",
    )
    parser.add_argument("--model", help=f"Realization model (default {DEFAULT_REALIZATION_MODEL}).")
    parser.add_argument(
        "--judge-model",
        help=f"Judge model whose material risks to track (default {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--limit", type=int, help=f"Max risks to check (default {_DEFAULT_MAX_PER_RUN})."
    )
    parser.add_argument(
        "--recheck-not-realized",
        action="store_true",
        help=(
            "Re-judge every not-realized verdict against the events it has already seen, "
            "instead of only those whose company has filed since. Run once by hand after a "
            "change to the grounding gates; not for the timer."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", source="realization", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    model_name = args.model or DEFAULT_REALIZATION_MODEL
    judge_model = args.judge_model or DEFAULT_JUDGE_MODEL
    limit = args.limit or get_config_int("MAX_REALIZATION_RISKS_PER_RUN", _DEFAULT_MAX_PER_RUN)
    engine = open_engine(config.filings_db_path)
    set_cost_sink(db_llm_call_sink(engine))

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="realization",
            started_at=started.isoformat(),
            model=model_name,
            limit=limit,
        )

        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                source="realization",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
            )
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — risk realization tracking paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). Risk realization tracking is "
                    f"paused until the cap resets at 00:00 UTC."
                ),
                dedup_key=f"cost_cap:{today_utc}",
            )
            sys.exit(1)

        counts = realization_pass(
            engine,
            build_realization_judge(model_name),
            model_name=model_name,
            judge_ver=judge_version(judge_model),
            realization_ver=realization_version(model_name),
            limit=limit,
            recheck_not_realized=args.recheck_not_realized,
        )

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "realization")
        span.set_attribute("realized", counts["realized"])
        emit(
            "tick_completed",
            source="realization",
            duration_ms=duration_ms,
            model=model_name,
            **counts,
        )


if __name__ == "__main__":
    main()
