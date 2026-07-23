"""CLI: embed risk-factor blocks for change-detection (ADR 0042, PR 3).

A resumable reconciler: finds stored risk-factor blocks that have no embedding yet
for the configured model, embeds them via Voyage in batches, and stores the
vectors keyed by (block, model). Idempotent — an already-embedded block is skipped
by the gap query, so a backlog drains across runs and a re-run embeds only what is
still missing. No cursor: the "gap" is the work set.

Decoupled from ingest on purpose: `scan-periodic` stores blocks with no external
dependency, and this step adds the vectors, so the ingest path never depends on
the embedding vendor.

Run as a one-shot (a systemd timer wiring is a separate infra step). Output is
JSON-line events to stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import Engine

from filings_orchestrator.change_detection import DEFAULT_MODEL, Embedder, VoyageEmbedder
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_int,
    get_config_str,
    get_secret,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    insert_block_embeddings,
    select_unembedded_blocks,
)

# Blocks per Voyage request. Risk-factor blocks are up to a few thousand characters,
# so a modest batch stays well inside Voyage's per-request token budget.
_DEFAULT_BATCH_SIZE = 32
# Blocks embedded per run, a backstop that bounds a single invocation; a backlog
# drains across runs. A backfill can raise it via --limit.
_DEFAULT_MAX_PER_RUN = 2000


def embed_pass(
    engine: Engine, embedder: Embedder, *, batch_size: int, limit: int
) -> dict[str, int]:
    """Embed up to `limit` un-embedded blocks in batches of `batch_size`.

    Returns per-run counts. Each batch is stored before the next is fetched, so an
    interrupted run leaves completed batches persisted and the rest for next time.
    """
    embedded = 0
    batches = 0
    remaining = limit
    while remaining > 0:
        batch = select_unembedded_blocks(engine, embedder.model_id, min(batch_size, remaining))
        if not batch:
            break
        vectors = embedder.embed([b.block_text for b in batch])
        insert_block_embeddings(
            engine,
            model_id=embedder.model_id,
            items=list(zip(batch, vectors, strict=True)),
            embedded_at=datetime.now(UTC).isoformat(),
        )
        embedded += len(batch)
        batches += 1
        remaining -= len(batch)
    return {"embedded": embedded, "batches": batches}


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="embed-blocks",
        description="Embed risk-factor blocks that lack a vector for the configured model.",
    )
    parser.add_argument(
        "--model",
        help=f"Embedding model id (default: env VOYAGE_MODEL or {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Max blocks to embed this run (default: env MAX_EMBED_BLOCKS_PER_RUN or "
        f"{_DEFAULT_MAX_PER_RUN}); raise for a one-off backfill.",
    )
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    try:
        api_key = get_secret("VOYAGE_API_KEY")
    except MissingConfigError as e:
        emit("tick_failed", source="embed", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    model = args.model or get_config_str("VOYAGE_MODEL", default=DEFAULT_MODEL)
    limit = args.limit or get_config_int("MAX_EMBED_BLOCKS_PER_RUN", _DEFAULT_MAX_PER_RUN)
    engine = open_engine(db_path)
    embedder = VoyageEmbedder(api_key, model)

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="embed",
            started_at=started.isoformat(),
            model=model,
            limit=limit,
            batch_size=args.batch_size,
        )
        try:
            counts = embed_pass(engine, embedder, batch_size=args.batch_size, limit=limit)
        except Exception as exc:
            # A batch failed (auth, rate limit, transient). Completed batches are
            # persisted; the rest are picked up next run (the gap query is the state).
            duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            emit(
                "tick_failed",
                source="embed",
                duration_ms=duration_ms,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            sys.exit(1)

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "embed")
        span.set_attribute("embedded", counts["embedded"])
        emit("tick_completed", source="embed", duration_ms=duration_ms, model=model, **counts)


if __name__ == "__main__":
    main()
