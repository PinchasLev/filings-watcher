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
contextless heuristics cannot separate the buckets reliably; the classifier (which has context) must
make the call, and per-exhibit recognition is enough.

## Decision

The classifier **recognizes a periodic financial report and defers it** by emitting it as one of a
set of **periodic leaves in the taxonomy** rather than forcing a material-event type. v1.3 adds a
tier-1 `periodic` domain whose tier-2 leaves are `periodic_annual`, `periodic_quarterly`,
`periodic_interim`, and a generic `periodic_report` catch-all. The model picks one leaf as usual and
the domain follows mechanically (ADR 0010 — post-hoc domain, not a hierarchical classifier).

The **defer/classify boundary lives at the domain**: the reduce stage excludes the entire `periodic`
domain from the events layer, so a periodic section is recorded but never collated into an event (a
6-K whose only exhibits are periodic reduces to zero events). Keying on the domain — not specific
leaves — means any future deferred document class added under it drops out of events automatically.

The periodic leaves are **offered only to 6-K**, via the existing per-call leaf-set mechanism
(ADR 0032): the live path withholds them from 8-K. Because they are declared last in `EventType`, the
8-K choice-set is byte-identical to before, so the 8-K prompt and `classifier_version` are unchanged.
The cadence split (`annual`/`quarterly`/`interim`) is deliberately **low-stakes metadata** — the
load-bearing decision is the domain (defer vs classify), which is the high-confidence call; a wrong
cadence changes nothing today and is advisory for a future extraction pass. `periodic_report` is the
catch-all when the period is unclear, and it doubles as the value the model naturally reaches for —
so its honest output validates instead of crashing.

**Scope boundary:** recognizing a periodical as periodic is *not* understanding it. This decision does
**not** parse, chunk, or extract anything from periodic filings. The persisted classifications
(`event_domain = 'periodic'`) are the queryable hand-off a future dedicated chunk-and-extract pass
(paired with the roadmap's structured-extraction component) will consume.

## Alternatives considered

### A separate `section_kind` axis + a validator (the first cut of this PR)

Rejected. It modeled periodic as an axis orthogonal to `event_type` — a separate field, a DB
migration, and a `model_validator` to reroute the value the model kept putting in `event_type`. The
validator was the tell: the two properties are **mutually exclusive in practice** (a 6-K exhibit is
either an event or a deferred report, never both), so they are one question — "what is this section?"
— whose answer space is the events plus the periodic leaves. A value, not an axis. The axis view only
earns its keep once the extraction pass surfaces events *inside* a report; it can add that structure
then (YAGNI).

### Cheap regex/heuristic routing (size, description, content markers)

Rejected as the decider. Phase 0 showed the free signals don't separate the buckets — descriptions
are boilerplate, size over-flags non-financial documents — and a contextless false "defer" silently
drops a real event. Heuristics may later serve a benign cost role (skip the call on tiny obvious
releases), never the routing judgment.

## Consequences

- **Easier:** event classification stays accurate (no forcing an event type onto an annual report),
  and the deferral rides the taxonomy + reduce we already have — no separate field, no migration, no
  validator. The model's natural `periodic_report` output is now simply correct.
- **Easier:** the future periodic-extraction pass has an exact, queryable work-list
  (`event_domain = 'periodic'`); the cadence leaf gives it a head start and gives us prevalence-by-type
  measurement now, nearly free.
- **Cost:** an additive taxonomy bump to v1.3 (a domain + four leaves), governed by ADR 0032; the
  `periodic` domain must stay out of event rollups/counts (it does — reduce drops it).
- **Accepted risk:** a mis-judged periodic leaf would drop a real event — mitigated by leaf
  descriptions that bias toward `earnings_release` for a results *press release*, by the rarity of
  mixed exhibits (Phase 0), and by keeping the load-bearing call at the easy domain level.
- **Deferred to follow-ups (not regressions):** (1) restoring a 6-K truncation *alert* scoped to
  event exhibits (rare — event exhibits are small); for now 6-K truncation stays telemetry-only
  (PR #124). (2) The dedicated periodic-content extraction pass. (3) Intra-exhibit segmentation for
  the rare event+financials bundle (Phase 0: 1/40).
