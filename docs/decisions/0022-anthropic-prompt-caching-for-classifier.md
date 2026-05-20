# 0022. Anthropic prompt caching for the classifier system block

- **Status:** Accepted
- **Date:** 2026-05-19

## Context

Every classification call sends the same system prompt: instructions, the event-type taxonomy, the materiality definition, the reasoning expectations. That block is on the order of 1.5K-2K input tokens and never varies between calls (only `classifier_version`'s hash changes when the prompt itself changes — see [ADR 0011](0011-classification-history-and-reclassification.md)).

The per-tick workload during a post-close burst classifies dozens to hundreds of 8-Ks back-to-back. Each filing contributes one Anthropic call per substantive Item; an average filing produces 2-3 Items. On a heavy day the orchestrator issues hundreds of calls that re-send an identical system block, paying full input-token cost every time.

Anthropic's [prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) marks a content block as `cache_control: {"type": "ephemeral"}`. The first call writes the cached prefix; subsequent calls within the cache window read it back at ~10× lower input-token cost. The cache key is the exact byte prefix of the request, so the same system block followed by different user messages is the hit pattern the orchestrator naturally exhibits.

## Decision

The system block is marked `cache_control: {"type": "ephemeral"}` on every classifier invocation. The user block (which carries the per-filing prose and varies on every call) is left uncached. Hit rate is governed entirely by Anthropic's cache-window semantics; the orchestrator does not manage cache state.

The marker is applied unconditionally — there is no feature flag, no per-environment gate. A cache miss (first call after eviction, or after a `classifier_version` change) is billed at the normal input-token rate; the marker only enables the discount, it does not penalize miss traffic.

## Alternatives considered

### External semantic-response cache (LangCache, RedisVL, GPTCache)

Deferred, not rejected on merit. Semantic caching matches near-duplicate prompts against cached responses via embedding lookup. The current workload classifies one unique filing body per call; near-duplicate hit rate is effectively zero, so the layer would add infrastructure without throughput or cost benefit. Genuine fits exist in *future* workloads — open-ended user Q&A against the same filing-derived features, repeat-similar hypothesis prompts — and the decision to adopt one is gated on observed traffic in those workloads, not on architectural elegance now.

### In-process exact-match response cache

Rejected for this surface. Exact-match repeats of `(system, user)` are also near-zero given the per-filing prose varies. The cache hit pattern we have is on the *prefix*, not the full request — exactly what Anthropic's native prompt caching is shaped for.

### Pre-compute classifier_version once per process

Tangential. `classifier_version()` already hashes the system prompt and is cheap; the cost lives in the Anthropic call, not the local hash. Caching the hash would not move the meter.

### Wait for measured cost pressure before enabling

Rejected. The change is ~5 lines and adds no operational surface (no infrastructure, no failure mode unique to caching, no observability gap created). The cost of enabling it now is negligible against the cost of paying full input rate for every burst-day call until "measured pressure" arrives.

## Consequences

- **Easier:** Burst-day input-token spend on the system block drops to roughly 10% of pre-change after the first call of each cache window. A 500-filing burst that previously paid full system-block cost on every call now pays it once per cache-window cycle.
- **Easier:** No code or operator action is required to keep the cache warm. The cache window is governed by Anthropic; the orchestrator's natural call cadence inside a tick (a few seconds between calls) keeps the prefix hot for the duration of a backlog drain.
- **Harder:** Observability of cache hit/miss is now a meaningful signal. The Anthropic response carries `cache_creation_input_tokens` and `cache_read_input_tokens` fields; surfacing these in structured logs or the observability layer ([ADR 0018](0018-observability-otel-native-operator-controlled.md)) is a follow-up if hit-rate verification becomes load-bearing.
- **Accepted commitment:** Any change to the system prompt invalidates the cache prefix; the next call after the change pays the write-cost. This is the right behavior — a changed prompt means changed semantics — and matches how `classifier_version` already treats the prompt hash as a versioning boundary.
- **Accepted commitment:** The minimum cacheable prefix size is set by Anthropic and may exceed our system block on short prompts. If the prompt shrinks below the threshold the marker becomes a no-op rather than an error; no defensive code is needed.
