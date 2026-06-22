# 0034. Recognize and defer periodic (10-Q/10-K-equivalent) 6-K content

- **Status:** Accepted
- **Date:** 2026-06-21

## Context

A 6-K is a catch-all envelope: a foreign private issuer uses it for what domestic filers split
across 8-K (discrete events), 10-Q (quarterly), and 10-K (annual). We scoped 6-K as the
**8-K-equivalent** (ADR 0033) — material events. But 6-Ks also carry the periodic financial reports
that ADR 0001 deliberately deferred. Classifying the first 50k chars of an 800k-char annual report
yields a poor, partial classification, and after 6-K shipped these reports flooded the
exhibit-truncation alert channel (PR #124 suppressed that as a stopgap).

Phase 0 (40 recent 6-Ks) showed the large-exhibit problem is a ~13% tail (median exhibit 6.4k), that
exhibit Descriptions are mostly boilerplate and size over-flags non-financial large docs (risk
policies, proxy notices), and that mixing an event and financials in one exhibit is rare (1/40). So
contextless heuristics cannot separate the buckets reliably, and per-exhibit recognition is enough.

## Decision

The **classifier itself recognizes** a periodic financial report and **defers** it instead of
forcing an event type. A new `section_kind` field on the classification output takes `event` or
`periodic_report`; the LLM — which already reads the exhibit — judges which, biased to choose `event`
unless the exhibit clearly *is* the financial statements / report (a results press release is an
`event`, not a `periodic_report`). `periodic_report` sections are **recorded but not collated into
events**, and `section_kind` is **persisted** so the deferral is a durable, queryable hand-off.

`section_kind` is a document class, **not** a material-event type, so it lives outside `EventType` and
does not change `TAXONOMY_VERSION`. The 6-K classify prompt gains the recognition instruction (so the
6-K `classifier_version` changes); the 8-K prompt is untouched.

**Scope boundary:** recognizing a periodical as periodic is *not* understanding it. This decision does
**not** parse, chunk, or extract anything from periodic filings. It produces the
`section_kind = 'periodic_report'` work-list that a future, dedicated periodic-content pass
(chunk-and-extract, paired with the roadmap's structured-extraction component, when we take on
10-Q/10-K for real) will consume.

## Alternatives considered

### Cheap regex/heuristic routing (size, description, content markers)

Rejected as the decider. Phase 0 showed the free signals don't separate the buckets — descriptions are
boilerplate, size over-flags non-financial documents — and a contextless false "defer" silently drops
a real event. Heuristics may later serve a benign cost role (skip the call on tiny obvious releases),
never the routing judgment.

### A separate cheap LLM triage call before classifying

Rejected for now. Folding recognition into the existing classify call is cost-neutral — it's the same
call we already make on every exhibit, with one more output option — and adds no pipeline stage. A
cheap pre-check that reads only the first few-k to avoid feeding a 50k periodic report is a possible
later cost optimization, not needed for correctness.

### Add a `periodic_report` value to the event taxonomy (`EventType`)

Rejected. It is a document class, not an event; putting it in `EventType` would pollute the taxonomy,
the domain mapping, and `TAXONOMY_VERSION` (ADR 0032). A separate `section_kind` field is the honest model.

## Consequences

- **Easier:** event classification stays accurate (no more forcing an event type onto an annual
  report), and the alert channel stays quiet without losing the truncation telemetry.
- **Easier:** the future periodic-extraction pass has an exact, queryable work-list
  (`classifications.section_kind = 'periodic_report'`) rather than having to rediscover which filings
  it must process.
- **Accepted risk:** a mis-judged `periodic_report` would drop a real event — mitigated by biasing the
  model to `event` when unsure and by the rarity of mixed exhibits (Phase 0). Monitored via the
  classifications data and the offline-eval sample.
- **Deferred to follow-ups (not regressions):** (1) restoring a 6-K truncation *alert* scoped to
  `event` exhibits we genuinely tried to classify (rare — event exhibits are small); for now 6-K
  truncation stays telemetry-only (PR #124). (2) The dedicated periodic-content extraction pass — the
  real "understand the report" work. (3) Intra-exhibit segmentation for the rare event+financials
  bundle (Phase 0: 1/40).
