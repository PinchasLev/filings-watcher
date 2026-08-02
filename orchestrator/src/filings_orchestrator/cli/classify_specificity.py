"""CLI: classify each material risk-factor change specific-vs-boilerplate (calibration).

The classify stage of the change-detection funnel's calibration pass. A resumable
reconciler: finds filings whose material changes have no specificity verdict yet for the
current catalog and prompt versions, asks Claude — given the common-mode theme catalog — to
tag each change company-specific vs common-mode boilerplate, and stores the per-change
verdicts. One LLM call per filing, with the catalog in the cached system prompt.

Requires a catalog (migration 023 / the build-catalog stage): with no catalog cut yet there
is nothing to classify against, so the tick is a clean no-op until one exists.

LLM-bearing, so it is cost-capped like the judge (ADR 0029): the tick refuses new work once
the daily Anthropic spend reaches the cap, and each call's tokens are recorded through the
cost sink. Bounded per run via --limit; a backlog drains across runs. Output is JSON-line
events to stdout.
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
    DEFAULT_SPECIFICITY_MODEL,
    SpecificityInput,
    build_specificity_classifier,
    classify_specificity,
    judge_version,
    render_specificity_system_prompt,
    sanitize_batch,
    specificity_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import MissingConfigError, get_config_int, load_config
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    insert_change_specificity,
    latest_catalog_version,
    load_catalog_themes,
    load_material_changes_for_specificity,
    select_filings_needing_specificity,
)

# Filings classified per run — one LLM call each; a backlog drains across runs.
_DEFAULT_MAX_PER_RUN = 100


def specificity_pass(
    engine: Engine,
    model: object,
    *,
    system_prompt: str,
    allowed_slugs: set[str],
    model_name: str,
    judge_ver: str,
    catalog_ver: str,
    spec_ver: str,
    limit: int,
) -> dict[str, int]:
    """Classify up to `limit` filings needing specificity verdicts. A filing whose call keeps
    failing, or returns nothing usable, is left for the next run (no rows stored)."""
    classified = failed = 0
    targets = select_filings_needing_specificity(engine, judge_ver, catalog_ver, spec_ver, limit)
    for target in targets:
        changes = load_material_changes_for_specificity(
            engine,
            accession_number=target.accession_number,
            section=target.section,
            model_id=target.model_id,
            judge_version=judge_ver,
        )
        if not changes:
            continue  # raced away between the gap query and the load; skip cleanly

        inputs = [SpecificityInput(c.category, c.explanation) for c in changes]
        try:
            batch = with_retries(
                partial(
                    classify_specificity,
                    model,
                    system_prompt=system_prompt,
                    changes=inputs,
                    model_name=model_name,
                    accession_number=target.accession_number,
                ),
                log_context={"accession": target.accession_number, "section": target.section},
            )
        except Exception as exc:
            failed += 1
            emit(
                "specificity_failed",
                accession_number=target.accession_number,
                section=target.section,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        by_index = sanitize_batch(batch, n_changes=len(changes), allowed_slugs=allowed_slugs)
        rows = [
            (change.change_seq, v.is_specific, v.matched_theme, v.confidence, v.explanation)
            for i, change in enumerate(changes, start=1)
            if (v := by_index.get(i)) is not None
        ]
        if not rows:
            failed += 1  # model returned nothing mappable — leave for the next run
            emit(
                "specificity_failed",
                accession_number=target.accession_number,
                section=target.section,
                error_class="empty_batch",
                message="no verdict mapped to any change",
            )
            continue

        insert_change_specificity(
            engine,
            target=target,
            judge_version=judge_ver,
            catalog_version=catalog_ver,
            specificity_version=spec_ver,
            verdicts=rows,
            classified_at=datetime.now(UTC).isoformat(),
        )
        classified += 1
    return {"classified": classified, "failed": failed, "candidates": len(targets)}


def main() -> None:
    setup_otel()
    import argparse

    parser = argparse.ArgumentParser(
        prog="classify-specificity",
        description="Classify each material risk-factor change specific-vs-boilerplate.",
    )
    parser.add_argument("--model", help=f"Specificity model (default {DEFAULT_SPECIFICITY_MODEL}).")
    parser.add_argument(
        "--judge-model",
        help=f"Judge model whose material changes to classify (default {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--limit", type=int, help=f"Max filings to classify (default {_DEFAULT_MAX_PER_RUN})."
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", source="specificity", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    model_name = args.model or DEFAULT_SPECIFICITY_MODEL
    judge_model = args.judge_model or DEFAULT_JUDGE_MODEL
    limit = args.limit or get_config_int("MAX_SPECIFICITY_FILINGS_PER_RUN", _DEFAULT_MAX_PER_RUN)
    engine = open_engine(config.filings_db_path)
    set_cost_sink(db_llm_call_sink(engine))

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="specificity",
            started_at=started.isoformat(),
            model=model_name,
            limit=limit,
        )

        catalog_ver = latest_catalog_version(engine)
        if catalog_ver is None:
            emit(
                "tick_completed",
                source="specificity",
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                model=model_name,
                classified=0,
                failed=0,
                candidates=0,
                no_catalog=True,
            )
            return

        # Pre-tick spend gate (ADR 0029): refuse LLM work once today's spend hits the cap.
        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                source="specificity",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
            )
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — specificity classification paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). Specificity classification "
                    f"is paused until the cap resets at 00:00 UTC."
                ),
                dedup_key=f"cost_cap:{today_utc}",
            )
            sys.exit(1)

        themes = load_catalog_themes(engine, catalog_ver)
        system_prompt = render_specificity_system_prompt(
            [(t.theme_slug, t.archetype) for t in themes]
        )
        allowed_slugs = {t.theme_slug for t in themes}
        model = build_specificity_classifier(model_name)

        counts = specificity_pass(
            engine,
            model,
            system_prompt=system_prompt,
            allowed_slugs=allowed_slugs,
            model_name=model_name,
            judge_ver=judge_version(judge_model),
            catalog_ver=catalog_ver,
            spec_ver=specificity_version(model_name),
            limit=limit,
        )

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "specificity")
        span.set_attribute("classified", counts["classified"])
        emit(
            "tick_completed",
            source="specificity",
            duration_ms=duration_ms,
            model=model_name,
            catalog_version=catalog_ver,
            **counts,
        )


if __name__ == "__main__":
    main()
