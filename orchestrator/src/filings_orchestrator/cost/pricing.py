"""Per-model pricing for cost estimation (ADR 0029).

Prices are USD per million tokens. The values in `PRICING_USD_PER_MILLION_TOKENS`
are the operator's responsibility to keep current — Anthropic publishes pricing
on its console; verify the values for each model in production use before
relying on the spend cap. An unknown model is treated as a worst-case rate so
the cap engages early rather than silently understating spend; a structured
`cost_pricing_unknown` event is emitted by `sink.emit_cost` when this fallback
fires, so the operator notices and updates the table.

Cache multipliers reflect Anthropic's prompt-caching pricing model (ADR 0022):
writing the cache costs a small premium over a regular input token; reading
from the cache is heavily discounted. The token-count breakdown
(`input_tokens`, `cache_read_tokens`, `cache_creation_tokens`) comes from
the response's `usage_metadata` and is recorded for the operator to inspect.
"""

from __future__ import annotations

# Verify against Anthropic's published pricing before deploy; update when
# pricing changes. Values here are conservative starting estimates suitable
# for the cap to engage before unbounded spend, not authoritative billing.
PRICING_USD_PER_MILLION_TOKENS: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-7": {"input": 15.00, "output": 75.00},
}

# Anthropic's prompt-cache pricing: writing to the cache costs ~1.25x a regular
# input token (cache_creation_tokens), reading from the cache costs ~0.10x
# (cache_read_tokens). The breakdown lives in usage_metadata.input_token_details.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# Fallback for unknown models: charge at the most expensive known model's rates
# so an undeclared model triggers the cap early instead of silently bypassing it.
_UNKNOWN_MODEL_FALLBACK = {"input": 15.00, "output": 75.00}


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> tuple[float, bool]:
    """Estimate the USD cost of one call given the response's token counts.

    `input_tokens` is the total Anthropic reports (regular + cached); the
    cache-served subsets are subtracted out and priced at their respective
    multipliers. Returns the cost and a flag indicating whether the model was
    known to the pricing table; the caller may surface the unknown-model case
    as a structured event.
    """
    pricing = PRICING_USD_PER_MILLION_TOKENS.get(model)
    known = pricing is not None
    if pricing is None:
        pricing = _UNKNOWN_MODEL_FALLBACK

    input_rate_per_token = pricing["input"] / 1_000_000
    output_rate_per_token = pricing["output"] / 1_000_000

    # The cache-served tokens are already counted inside input_tokens; subtract
    # them out before pricing the regular portion. Clamp at zero defensively
    # for the case where a misreporting client double-counts.
    regular_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)

    cost = (
        regular_input * input_rate_per_token
        + cache_creation_tokens * input_rate_per_token * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * input_rate_per_token * CACHE_READ_MULTIPLIER
        + output_tokens * output_rate_per_token
    )
    return cost, known
