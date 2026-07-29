"""CLI: synthesize a filing's material disclosure changes into a read (ADR 0043, PR 2).

The reduce stage of the change-detection funnel. A resumable reconciler: finds
filings whose material change verdicts (from the judge) have no synthesis yet for the
current versions, rolls up the headline direction and counts in code, asks Claude to
reduce the distilled verdicts into a thesis paragraph and top-effects list, and stores
the result.

LLM-bearing, so it is cost-capped like the judge (ADR 0029): the tick refuses new work
once the daily Anthropic spend reaches the cap, and each reduce's tokens are recorded
through the cost sink. Bounded per run via --limit; a backlog drains across runs, and
a filing already synthesized for the current versions is not redone (the gap query is
the state). Transient API failures retry in-call and, if still failing, leave the
filing for the next run.

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
    DEFAULT_SYNTHESIS_MODEL,
    DisclosureSynthesis,
    Finding,
    HeadlineDirection,
    HeadlineIntensity,
    build_standing_synthesizer,
    build_synthesizer,
    judge_version,
    standing_synthesis_version,
    synthesis_version,
    synthesize,
    synthesize_standing,
)
from filings_orchestrator.change_detection.taxonomy import (
    RiskChangeDirection,
    coerce_category,
    coerce_direction,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_int,
    load_config,
)
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    insert_change_synthesis,
    load_filing_section_text,
    load_material_verdicts,
    select_filings_needing_stable_synthesis,
    select_filings_needing_synthesis,
)

# Filings synthesized per run — one LLM reduce each; a backlog drains across runs.
_DEFAULT_MAX_PER_RUN = 100

# The stable path reads the current section text; bound it so a pathological section
# does not blow the prompt (a real Item 1A is well under this).
_STANDING_SECTION_CHAR_BUDGET = 120_000


def _synthesize_one(
    model: object, findings: list[Finding], model_name: str, accession_number: str
) -> DisclosureSynthesis:
    return synthesize(
        model,
        findings=findings,
        model_name=model_name,
        accession_number=accession_number,
    )


def synthesis_pass(
    engine: Engine,
    model: object,
    *,
    model_name: str,
    judge_ver: str,
    synth_ver: str,
    limit: int,
) -> dict[str, int]:
    """Synthesize up to `limit` filings needing it. A filing whose reduce keeps
    failing is left for the next run (no row stored), not fatal."""
    synthesized = failed = 0
    targets = select_filings_needing_synthesis(engine, judge_ver, synth_ver, limit)
    for target in targets:
        verdicts = load_material_verdicts(
            engine,
            accession_number=target.accession_number,
            section=target.section,
            model_id=target.model_id,
            judge_version=judge_ver,
        )
        if not verdicts:
            continue  # raced away between the gap query and the load; skip cleanly

        # Code owns the counts (facts, shown beside the label); the reduce owns the
        # direction (a holistic, severity-aware judgment a count cannot make).
        worse = sum(1 for v in verdicts if v.direction == RiskChangeDirection.WORSE)
        eased = sum(1 for v in verdicts if v.direction == RiskChangeDirection.EASED)
        neutral = len(verdicts) - worse - eased
        findings = [
            Finding(coerce_category(v.category), coerce_direction(v.direction), v.explanation)
            for v in verdicts
        ]

        try:
            synth = with_retries(
                partial(_synthesize_one, model, findings, model_name, target.accession_number),
                log_context={"accession": target.accession_number, "section": target.section},
            )
        except Exception as exc:
            failed += 1
            emit(
                "change_synthesis_failed",
                accession_number=target.accession_number,
                section=target.section,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        insert_change_synthesis(
            engine,
            target=target,
            judge_version=judge_ver,
            synthesis_version=synth_ver,
            headline_direction=synth.headline_direction.value,
            headline_intensity=synth.headline_intensity.value,
            material_count=len(verdicts),
            worse_count=worse,
            eased_count=eased,
            neutral_count=neutral,
            thesis=synth.thesis,
            top_effects=synth.top_effects,
            synthesized_at=datetime.now(UTC).isoformat(),
        )
        synthesized += 1
    return {"synthesized": synthesized, "failed": failed, "candidates": len(targets)}


def stable_synthesis_pass(
    engine: Engine,
    model: object,
    *,
    model_name: str,
    judge_ver: str,
    standing_ver: str,
    limit: int,
) -> dict[str, int]:
    """Summarize stable filings (diffed, zero material changes) into a standing-risk read.
    "No change" is not "no risk": these filings carry real, unchanged risk, so they earn a
    card summarizing what those standing risks ARE rather than falling off the radar (ADR
    0043). Code fixes the headline to STABLE / NONE (the change picture did not move); the
    reduce writes only the prose. A filing whose reduce keeps failing is left for the next
    run (no row stored)."""
    synthesized = failed = 0
    targets = select_filings_needing_stable_synthesis(engine, judge_ver, standing_ver, limit)
    for target in targets:
        section_text = load_filing_section_text(
            engine,
            accession_number=target.accession_number,
            section=target.section,
            max_chars=_STANDING_SECTION_CHAR_BUDGET,
        )
        if not section_text.strip():
            continue  # no block text (raced away / empty) — skip cleanly

        try:
            summary = with_retries(
                partial(
                    synthesize_standing,
                    model,
                    section_text=section_text,
                    model_name=model_name,
                    accession_number=target.accession_number,
                ),
                log_context={"accession": target.accession_number, "section": target.section},
            )
        except Exception as exc:
            failed += 1
            emit(
                "change_synthesis_failed",
                accession_number=target.accession_number,
                section=target.section,
                stable=True,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        insert_change_synthesis(
            engine,
            target=target,
            judge_version=judge_ver,
            synthesis_version=standing_ver,
            headline_direction=HeadlineDirection.STABLE.value,
            headline_intensity=HeadlineIntensity.NONE.value,
            material_count=0,
            worse_count=0,
            eased_count=0,
            neutral_count=0,
            thesis=summary.thesis,
            top_effects=summary.top_risks,
            synthesized_at=datetime.now(UTC).isoformat(),
        )
        synthesized += 1
    return {
        "stable_synthesized": synthesized,
        "stable_failed": failed,
        "stable_candidates": len(targets),
    }


def main() -> None:
    setup_otel()
    import argparse

    parser = argparse.ArgumentParser(
        prog="synthesize-changes",
        description="Synthesize each filing's material disclosure changes into a read.",
    )
    parser.add_argument("--model", help=f"Synthesis model (default {DEFAULT_SYNTHESIS_MODEL}).")
    parser.add_argument(
        "--judge-model",
        help=f"Judge model whose verdicts to summarize (default {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--limit", type=int, help=f"Max filings to synthesize (default {_DEFAULT_MAX_PER_RUN})."
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", source="synthesize", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    model_name = args.model or DEFAULT_SYNTHESIS_MODEL
    judge_model = args.judge_model or DEFAULT_JUDGE_MODEL
    limit = args.limit or get_config_int("MAX_SYNTHESIZE_FILINGS_PER_RUN", _DEFAULT_MAX_PER_RUN)
    engine = open_engine(config.filings_db_path)
    set_cost_sink(db_llm_call_sink(engine))

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="synthesize",
            started_at=started.isoformat(),
            model=model_name,
            limit=limit,
        )

        # Pre-tick spend gate (ADR 0029): refuse LLM work once today's spend hits the cap.
        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                source="synthesize",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
            )
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — change synthesis paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). Disclosure-change synthesis "
                    f"is paused until the cap resets at 00:00 UTC."
                ),
                dedup_key=f"cost_cap:{today_utc}",
            )
            sys.exit(1)

        judge_ver = judge_version(judge_model)
        model = build_synthesizer(model_name)
        counts = synthesis_pass(
            engine,
            model,
            model_name=model_name,
            judge_ver=judge_ver,
            synth_ver=synthesis_version(model_name),
            limit=limit,
        )

        # Stable filings (diffed, zero material changes) get a standing-risk summary from a
        # second reduce. It shares the per-run budget — change synthesis runs first, and the
        # stable pass takes whatever of `limit` remains — so both drain across runs without a
        # tick's LLM calls exceeding `limit`. Uses its own bound model + version.
        remaining = max(0, limit - counts["synthesized"])
        stable_counts = stable_synthesis_pass(
            engine,
            build_standing_synthesizer(model_name),
            model_name=model_name,
            judge_ver=judge_ver,
            standing_ver=standing_synthesis_version(model_name),
            limit=remaining,
        )
        counts.update(stable_counts)

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "synthesize")
        span.set_attribute("synthesized", counts["synthesized"])
        emit(
            "tick_completed",
            source="synthesize",
            duration_ms=duration_ms,
            model=model_name,
            **counts,
        )


if __name__ == "__main__":
    main()
