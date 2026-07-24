"""CLI: judge the materiality of shortlisted risk-factor changes (ADR 0042, PR 5).

The precision stage of the change-detection funnel. A resumable reconciler: finds
shortlisted changes (from the diff) with no verdict yet for the current judge
version, asks Claude whether each *change* is material (judging the delta — a
changed block is shown with both its new and prior text), and stores a validated
verdict. Code, not the model, then flags a low-confidence verdict for review.

LLM-bearing, so it is cost-capped like the classify path (ADR 0029): the tick
refuses new work once the daily Anthropic spend reaches the cap, and each judgment's
tokens are recorded through the cost sink. Bounded per run via --limit; a backlog
drains across runs, and a filing whose changes are judged is not re-judged (the gap
query is the state). Transient API failures retry in-call and, if still failing,
leave the change for the next run.

Run as a one-shot (a systemd timer wiring is a separate infra step). Output is
JSON-line events to stdout.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from functools import partial

from opentelemetry import trace
from sqlalchemy import Engine

from filings_orchestrator.alerting import ALERT, emit_alert
from filings_orchestrator.change_detection import (
    DEFAULT_JUDGE_MODEL,
    MaterialityVerdict,
    build_judge,
    judge_change,
    judge_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_float,
    get_config_int,
    load_config,
)
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    ChangeToJudge,
    daily_cost_usd,
    insert_change_verdict,
    select_changes_needing_verdict,
)

# Changes judged per run — LLM-bearing, so kept modest; a backlog drains across runs.
_DEFAULT_MAX_PER_RUN = 200
# Below this confidence, a verdict is flagged for human review rather than trusted.
_DEFAULT_REVIEW_CONFIDENCE = 0.6


def _judge_one(model: object, change: ChangeToJudge, model_name: str) -> MaterialityVerdict:
    return judge_change(
        model,
        change_type=change.change_type,
        current_text=change.current_text,
        prior_text=change.prior_text,
        model_name=model_name,
        accession_number=change.accession_number,
    )


def judge_pass(
    engine: Engine,
    model: object,
    *,
    model_name: str,
    version: str,
    limit: int,
    review_below: float,
) -> dict[str, int]:
    """Judge up to `limit` un-judged changes with the bound `model`. A change whose
    judgment keeps failing is left for the next run (no verdict stored), not fatal."""
    judged = material = flagged = failed = 0
    changes = select_changes_needing_verdict(engine, version, limit)
    for change in changes:
        try:
            verdict = with_retries(
                partial(_judge_one, model, change, model_name),
                log_context={
                    "accession": change.accession_number,
                    "change_seq": change.change_seq,
                },
            )
        except Exception as exc:
            failed += 1
            emit(
                "change_judge_failed",
                accession_number=change.accession_number,
                change_seq=change.change_seq,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        needs_review = verdict.confidence < review_below
        insert_change_verdict(
            engine,
            change=change,
            judge_version=version,
            verdict=verdict,
            needs_review=needs_review,
            judged_at=datetime.now(UTC).isoformat(),
        )
        judged += 1
        material += int(verdict.is_material)
        flagged += int(needs_review)
    return {
        "judged": judged,
        "material": material,
        "needs_review": flagged,
        "failed": failed,
        "candidates": len(changes),
    }


def main() -> None:
    setup_otel()
    import argparse

    parser = argparse.ArgumentParser(
        prog="judge-changes",
        description="Judge the materiality of shortlisted risk-factor changes.",
    )
    parser.add_argument("--model", help=f"Judge model (default {DEFAULT_JUDGE_MODEL}).")
    parser.add_argument(
        "--limit", type=int, help=f"Max changes to judge (default {_DEFAULT_MAX_PER_RUN})."
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", source="judge", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    model_name = args.model or DEFAULT_JUDGE_MODEL
    limit = args.limit or get_config_int("MAX_JUDGE_CHANGES_PER_RUN", _DEFAULT_MAX_PER_RUN)
    review_below = get_config_float("MATERIALITY_REVIEW_CONFIDENCE", _DEFAULT_REVIEW_CONFIDENCE)
    engine = open_engine(config.filings_db_path)
    set_cost_sink(db_llm_call_sink(engine))

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="judge",
            started_at=started.isoformat(),
            model=model_name,
            limit=limit,
        )

        # Pre-tick spend gate (ADR 0029): refuse LLM work once today's spend hits the
        # cap. The judge is the only LLM-bearing stage of change-detection, so this is
        # where its cost is bounded; the reconciler picks up where it left off next run.
        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                source="judge",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
            )
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — materiality judging paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). Change materiality judging "
                    f"is paused until the cap resets at 00:00 UTC."
                ),
                dedup_key=f"cost_cap:{today_utc}",
            )
            sys.exit(1)

        model = build_judge(model_name)
        counts = judge_pass(
            engine,
            model,
            model_name=model_name,
            version=judge_version(model_name),
            limit=limit,
            review_below=review_below,
        )
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "judge")
        span.set_attribute("judged", counts["judged"])
        emit("tick_completed", source="judge", duration_ms=duration_ms, model=model_name, **counts)


if __name__ == "__main__":
    main()
