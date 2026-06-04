"""Cost-observability surface for Anthropic API calls (ADR 0029).

The pipeline classify and reduce stages each issue Anthropic calls through
LangChain. This package centralizes the cost-side concerns of those calls:

- `pricing` — per-model per-million-token rates, with ADR 0022's prompt-cache
  read/write multipliers, and the `estimate_cost_usd` helper.
- `sink` — the module-level sink an entry-point CLI installs at startup
  (typically `db_cost_sink(engine)`); `emit_cost` is called from the classify
  and reduce call sites and routes through the installed sink, or no-ops when
  none is installed (tests, CLIs without persistence).

The split keeps the classifier and reducer modules unaware of the database
and the structured-event surface — they call `emit_cost`, the sink does the
side-effect.
"""

from filings_orchestrator.cost.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICING_USD_PER_MILLION_TOKENS,
    estimate_cost_usd,
)
from filings_orchestrator.cost.sink import (
    CostObservation,
    clear_cost_sink,
    db_cost_sink,
    emit_cost,
    set_cost_sink,
)

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "PRICING_USD_PER_MILLION_TOKENS",
    "CostObservation",
    "clear_cost_sink",
    "db_cost_sink",
    "emit_cost",
    "estimate_cost_usd",
    "set_cost_sink",
]
