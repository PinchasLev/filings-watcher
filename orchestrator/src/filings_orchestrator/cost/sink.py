"""Module-level cost sink that the classify and reduce call sites route through.

The entry-point CLI installs a sink at startup (typically `db_cost_sink(engine)`
to persist + emit, or `set_cost_sink(None)` to no-op for tests and CLIs without
persistence). The classifier and reducer call `emit_cost(...)` after every
`model.invoke()`; emit_cost extracts the token-count breakdown from the
response's `usage_metadata`, estimates cost, and hands the observation to the
installed sink. With no sink installed, emit_cost is a no-op.

Keeping the sink module-level rather than threaded through the LLM call API
keeps the classifier and reducer unaware of the database, the structured-event
emitter, and any other observability concern — they only know that a call has
finished and they hand the response to emit_cost.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text

from filings_orchestrator.cost.pricing import estimate_cost_usd
from filings_orchestrator.log_events import emit


@dataclass(frozen=True)
class CostObservation:
    """One Anthropic call's observed cost, ready for a sink to consume.

    Fields mirror the cost_events table columns; `pricing_unknown` is True
    when the model was absent from the pricing table and the worst-case
    fallback rate was used. The sink decides whether to log, persist, alarm,
    or some combination.
    """

    model: str
    stage: str
    accession_number: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_cost_usd: float
    pricing_unknown: bool
    emitted_at: str


Sink = Callable[[CostObservation], None]

# Module-level installed sink. None means "do nothing" — emit_cost short-circuits.
_active_sink: Sink | None = None


def set_cost_sink(sink: Sink | None) -> None:
    """Install (or replace) the active cost sink for this process."""
    global _active_sink
    _active_sink = sink


def clear_cost_sink() -> None:
    """Remove any installed sink. Useful in tests."""
    set_cost_sink(None)


def emit_cost(
    *,
    model: str,
    stage: str,
    response: Any,
    accession_number: str | None = None,
) -> None:
    """Record one Anthropic call's cost via the installed sink, or no-op.

    Reads the `usage_metadata` attribute LangChain attaches to the response.
    A missing or empty metadata block is recorded with zero tokens — better
    to surface a zero-cost row than to drop the observation silently.
    """
    if _active_sink is None:
        return

    usage = getattr(response, "usage_metadata", None) or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    token_details = usage.get("input_token_details") or {}
    cache_read = int(token_details.get("cache_read") or 0)
    cache_creation = int(token_details.get("cache_creation") or 0)

    cost, known = estimate_cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )

    observation = CostObservation(
        model=model,
        stage=stage,
        accession_number=accession_number,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        estimated_cost_usd=cost,
        pricing_unknown=not known,
        emitted_at=datetime.now(UTC).isoformat(),
    )
    _active_sink(observation)


def db_cost_sink(engine: Engine) -> Sink:
    """Return a sink that persists each observation and emits a structured event.

    The persisted row is the input to `daily_cost_usd`'s aggregate; the
    structured `cost_observed` event lands in the same journald stream as
    tick_started / tick_completed / reduce_failed for OTel correlation.
    A pricing-unknown observation additionally emits `cost_pricing_unknown`
    so the operator notices the pricing table is stale.
    """
    insert_sql = text(
        """
        INSERT INTO cost_events (
            emitted_at, model, stage, accession_number,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            estimated_cost_usd
        ) VALUES (
            :emitted_at, :model, :stage, :accession_number,
            :input_tokens, :output_tokens,
            :cache_read_tokens, :cache_creation_tokens,
            :estimated_cost_usd
        )
        """
    )

    def _sink(observation: CostObservation) -> None:
        with engine.begin() as conn:
            conn.execute(
                insert_sql,
                {
                    "emitted_at": observation.emitted_at,
                    "model": observation.model,
                    "stage": observation.stage,
                    "accession_number": observation.accession_number,
                    "input_tokens": observation.input_tokens,
                    "output_tokens": observation.output_tokens,
                    "cache_read_tokens": observation.cache_read_tokens,
                    "cache_creation_tokens": observation.cache_creation_tokens,
                    "estimated_cost_usd": observation.estimated_cost_usd,
                },
            )
        emit(
            "cost_observed",
            model=observation.model,
            stage=observation.stage,
            accession_number=observation.accession_number,
            input_tokens=observation.input_tokens,
            output_tokens=observation.output_tokens,
            cache_read_tokens=observation.cache_read_tokens,
            cache_creation_tokens=observation.cache_creation_tokens,
            estimated_cost_usd=round(observation.estimated_cost_usd, 6),
        )
        if observation.pricing_unknown:
            emit(
                "cost_pricing_unknown",
                model=observation.model,
                stage=observation.stage,
                accession_number=observation.accession_number,
                note="model absent from pricing table; charged at worst-case fallback rate",
            )

    return _sink
