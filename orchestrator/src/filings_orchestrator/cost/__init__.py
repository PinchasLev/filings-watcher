"""LLM-call observability surface for Anthropic API calls (ADR 0029).

The pipeline classify and reduce stages each issue Anthropic calls through
LangChain. This package centralizes the per-call observability concerns:
tokens (the engineering metric we control) and cost (a derived value computed
from tokens x pricing). The module is named `cost` because the load-bearing
operational mechanism is the cost cap, but the recorded surface is
LLM-call-shaped — every per-call observation carries the full token breakdown
alongside the derived cost.

- `pricing` — per-model per-million-token rates, with ADR 0022's prompt-cache
  read/write multipliers, and the `estimate_cost_usd` helper.
- `sink` — the module-level sink an entry-point CLI installs at startup
  (typically `db_llm_call_sink(engine)`); `emit_llm_call` is called from the
  classify and reduce call sites and routes through the installed sink, or
  no-ops when none is installed (tests, CLIs without persistence).

The split keeps the classifier and reducer modules unaware of the database
and the structured-event surface — they call `emit_llm_call`, the sink does
the side-effect.

Retention (deferred): the per-call rows the sink persists are telemetry-
shaped, not transactional. At v0 scale they co-locate with the transactional
DB for dev-friendly queryability; the disciplined long-term shape is
aggregate-in-DB / detail-in-logs (or a separate telemetry store). See the
migration comment in `005_llm_calls.sql` and the `telemetry-vs-transactional`
memory note.
"""

from filings_orchestrator.cost.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICING_USD_PER_MILLION_TOKENS,
    estimate_cost_usd,
)
from filings_orchestrator.cost.sink import (
    LLMCallObservation,
    clear_cost_sink,
    db_llm_call_sink,
    emit_llm_call,
    set_cost_sink,
)

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "PRICING_USD_PER_MILLION_TOKENS",
    "LLMCallObservation",
    "clear_cost_sink",
    "db_llm_call_sink",
    "emit_llm_call",
    "estimate_cost_usd",
    "set_cost_sink",
]
