"""CLI: build the common-mode theme catalog from the corpus of material changes.

The calibration pass's corpus-reduce stage. Reads a digest of every material change the
judge produced (COMPANY | theme | explanation), asks Sonnet to name the common-mode
boilerplate themes across the cohort, and stores the result as a content-addressed,
versioned catalog. A later stage classifies each change specific-vs-boilerplate against
this catalog so a company's idiosyncratic changes surface (the common-mode noise is
discounted).

Unlike the per-filing reconcilers (judge, synthesize), this is a whole-corpus reduce run
on a cadence, not per-filing: one Sonnet call per invocation. Each run is SEEDED with the
current catalog and asked to evolve it — keep still-relevant themes verbatim, add only
genuinely-new themes, drop obsolete ones — so it is idempotent: an unchanged catalog comes
back with the same slugs/archetypes and thus the same content-addressed catalog_version and
writes nothing new (no downstream re-classification). A real drift is a new cut, leaving
prior cuts intact.

LLM-bearing, so it is cost-capped like the judge (ADR 0029): the tick refuses new work once
the daily Anthropic spend reaches the cap. Output is JSON-line events to stdout.
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
    DEFAULT_CATALOG_MODEL,
    DEFAULT_JUDGE_MODEL,
    build_catalog_extractor,
    canonical_themes,
    catalog_content_hash,
    catalog_version,
    extract_catalog,
    judge_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import (
    MissingConfigError,
    load_config,
)
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    insert_disclosure_catalog,
    latest_catalog_version,
    load_catalog_themes,
    load_material_change_digest,
)

# The digest is one Sonnet call, so it must fit one context. This bounds it generously
# (~150k tokens); at current coverage the whole corpus fits under it. If a future corpus
# exceeds it, the digest is truncated and the drop is emitted (never silent) — the signal
# to move to a stratified sample or hierarchical map-reduce (a documented later refinement).
_MAX_DIGEST_CHARS = 600_000


def build_catalog_pass(
    engine: Engine,
    model: object,
    *,
    model_name: str,
    judge_ver: str,
    corpus_label: str,
) -> dict[str, object]:
    """Reduce the corpus digest into the common-mode catalog and cut it. Returns counts;
    a reduce that keeps failing raises out of `with_retries` and the tick reports it."""
    digest_rows = load_material_change_digest(engine, judge_version=judge_ver)
    if not digest_rows:
        return {"material_changes": 0, "themes": 0, "catalog_version": None, "cut": False}

    lines = [f"{r.company} | {r.category} | {r.explanation}" for r in digest_rows]
    digest = "\n".join(lines)
    truncated = 0
    if len(digest) > _MAX_DIGEST_CHARS:
        digest = digest[:_MAX_DIGEST_CHARS]
        kept = digest.count("\n") + 1
        truncated = len(lines) - kept
        emit(
            "catalog_digest_truncated",
            total_changes=len(lines),
            kept_changes=kept,
            dropped_changes=truncated,
            budget_chars=_MAX_DIGEST_CHARS,
        )

    # Seed the reduce with the current catalog so unchanged themes carry forward verbatim
    # (stable catalog_version, no downstream re-classification churn). Empty on first build.
    current_ver = latest_catalog_version(engine)
    current_themes = (
        [(t.theme_slug, t.archetype) for t in load_catalog_themes(engine, current_ver)]
        if current_ver
        else []
    )
    catalog = with_retries(
        partial(
            extract_catalog,
            model,
            digest=digest,
            model_name=model_name,
            current_themes=current_themes,
        ),
        log_context={"stage": "catalog", "corpus_label": corpus_label},
    )
    themes = canonical_themes(catalog.themes)
    if not themes:
        return {
            "material_changes": len(lines),
            "themes": 0,
            "catalog_version": None,
            "cut": False,
        }

    version = catalog_version(themes, model_name)
    insert_disclosure_catalog(
        engine,
        catalog_version=version,
        model_id=model_name,
        corpus_label=corpus_label,
        content_hash=catalog_content_hash(themes),
        themes=[(t.theme_slug, t.archetype, t.prevalence) for t in themes],
        cut_at=datetime.now(UTC).isoformat(),
    )
    return {
        "material_changes": len(lines),
        "themes": len(themes),
        "catalog_version": version,
        "cut": True,
        "truncated_changes": truncated,
    }


def main() -> None:
    setup_otel()
    import argparse

    parser = argparse.ArgumentParser(
        prog="build-catalog",
        description="Build the common-mode theme catalog from the corpus of material changes.",
    )
    parser.add_argument("--model", help=f"Catalog reduce model (default {DEFAULT_CATALOG_MODEL}).")
    parser.add_argument(
        "--judge-model",
        help=f"Judge model whose material changes form the corpus (default {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--corpus-label",
        default="all",
        help="Label for the corpus reduced over, e.g. 'FY2025' (default 'all').",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", source="catalog", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    model_name = args.model or DEFAULT_CATALOG_MODEL
    judge_model = args.judge_model or DEFAULT_JUDGE_MODEL
    engine = open_engine(config.filings_db_path)
    set_cost_sink(db_llm_call_sink(engine))

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="catalog",
            started_at=started.isoformat(),
            model=model_name,
            corpus_label=args.corpus_label,
        )

        # Pre-tick spend gate (ADR 0029): refuse LLM work once today's spend hits the cap.
        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                source="catalog",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
            )
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — catalog build paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). Catalog build is paused "
                    f"until the cap resets at 00:00 UTC."
                ),
                dedup_key=f"cost_cap:{today_utc}",
            )
            sys.exit(1)

        judge_ver = judge_version(judge_model)
        model = build_catalog_extractor(model_name)
        try:
            counts = build_catalog_pass(
                engine,
                model,
                model_name=model_name,
                judge_ver=judge_ver,
                corpus_label=args.corpus_label,
            )
        except Exception as exc:
            emit(
                "tick_failed",
                source="catalog",
                error_class=type(exc).__name__,
                message=str(exc),
            )
            sys.exit(1)

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        themes_count = counts.get("themes")
        span.set_attribute("source", "catalog")
        span.set_attribute("themes", themes_count if isinstance(themes_count, int) else 0)
        emit(
            "tick_completed",
            source="catalog",
            duration_ms=duration_ms,
            model=model_name,
            **counts,
        )


if __name__ == "__main__":
    main()
