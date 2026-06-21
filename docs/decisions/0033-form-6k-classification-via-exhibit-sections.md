# 0033. Form 6-K classification via per-exhibit sections

- **Status:** Accepted
- **Date:** 2026-06-21

## Context

V0 classified only Form 8-K (ADR 0001). The pipeline built since — EX-99 exhibit
ingestion (ADR 0031), the classify→reduce map/reduce (ADR 0027), the governed
taxonomy (ADR 0032), runs/versioning, the cost cap, and alerting — is form-neutral
enough that a second form is now cheap. Form 6-K is the natural next form: it is the
report a foreign private issuer furnishes, and it carries the same kinds of material
events an 8-K does (earnings, dividends, M&A, executive changes).

The one structural difference forces the decision: **a 6-K has no standardized Item
structure.** An 8-K is sectioned by SEC-defined Items (1.01, 5.02, …); a 6-K is a
cover wrapper whose substance lives in the EX-99 exhibits it furnishes (press releases,
interim/half-year results, announcements, circulars), and a single 6-K commonly bundles
several unrelated announcements across separate exhibits. The classifier's unit of work
— "one section per call, then reduce" — needs a section definition for a form that has
no Items.

## Decision

For a 6-K, **each furnished EX-99 exhibit is one classification section** — the direct
analogue of an 8-K Item. The classifier labels each exhibit independently, then the
existing reduce stage collates them into filing-level events. A 6-K furnishing no EX-99
exhibits falls back to classifying its cover body, reusing the whole-filing path.

Both ingest paths (daily-index and the near-real-time Atom feed) are widened to pull
6-K alongside 8-K; the shared per-filing pipeline, persistence, and read path are
unchanged. The classify and reduce system prompts gain a form-specific lead-in, which
flows into the `classifier_version`/`reducer_version` hashes — so 6-K rows are
version-tagged distinctly from 8-K and the 8-K version strings are byte-for-byte
unchanged. The per-section key (the `classifications.item_number` column) carries the
exhibit label (e.g. `EX-99.1`) for a 6-K; no schema migration is required.

## Alternatives considered

### One classification per 6-K (cover + exhibits as a single blob)

Rejected as the primary design. Simplest and cheapest, but it collapses a
multi-announcement 6-K into a single `event_type`, losing signal exactly where 6-Ks are
richest, and it strains the per-section length budget. Per-exhibit sectioning reuses the
reduce stage that already exists to consolidate multi-section filings, so the faithful
option is barely more work than the lossy one.

### A separate 6-K classifier / taxonomy

Rejected. The material-event taxonomy (ADR 0032) is form-neutral; a 6-K dividend or
earnings release is the same event a domestic filer discloses. A parallel taxonomy would
duplicate maintenance and fragment the events layer. Form-specific *prompts* over one
shared taxonomy capture the difference that matters.

### Renaming `item_number` to a generic `section_key`

Rejected for this change. Semantically cleaner, but it ripples through the schema, the
reduce grounding, the Go read path, and every fixture. The column already stores an
opaque string under a unique key; reusing it for the exhibit label is sound and keeps the
change migration-free. A rename can be a later, isolated refactor if a third form needs it.

## Consequences

- **Easier:** 6-K rides the entire existing pipeline — ingest, resolve, fetch, exhibits,
  classify, reduce, persist, render — with form-aware prompts as the only behavioral
  change. No migration, no new taxonomy.
- **Easier:** Adding further forms now has a worked pattern: define the form's sectioning
  in `_sections_for`, give it a prompt lead-in, widen the ingest filter.
- **Harder / watch:** 6-K adds meaningful filing volume and LLM cost. The existing daily
  cost cap (ADR 0029) guards spend; actual 6-K volume/cost is measured from real ticks and
  the cap tuned after, rather than predicted up front.
- **Section budget:** a 6-K exhibit gets a dedicated, larger per-section cap (50k chars
  vs the 8-K Item's 12k) because the exhibit *is* the content, not supplemental — local
  verification showed the 12k cap dropping ~87% of a real exhibit and missing most of its
  disclosure. Outlier annual-report-length exhibits still truncate.
- **Truncation signal is form-aware, and the truncation ALERT is 8-K-only for now.**
  Truncation is measured against the basis each form actually uses — the 16k shared
  context budget for 8-K, the 50k per-section cap for 6-K — so 6-K telemetry reports what
  the classifier truly missed (the original 16k basis overstated it into the 800k's and
  fired on fully-read exhibits). Because large periodic-report exhibits make truncation the
  *steady state* for 6-K, the red-flag truncation alert (ADR 0031) is suppressed for 6-K
  (telemetry-only via `exhibit_context`) rather than routed to any Discord channel — it
  would just relocate the flood. The forthcoming triage stage recognizes and defers those
  periodic reports, after which a 6-K truncation alert can return, firing only on *event*
  exhibits we genuinely tried to classify.
- **Image/scanned exhibits:** some exhibits (seen with foreign filers) are page images in
  an HTML wrapper and extract to ~no text, so the classifier can't read them. The pipeline
  degrades gracefully (low confidence, dropped or low-signal in reduce) and emits an
  `exhibit_no_extractable_text` signal so the unread content is visible. OCR is deferred;
  the signal is the measure-first step toward sizing how often it matters.
- **Follow-up:** an offline-eval/A-B sample over real 6-Ks to measure classification
  quality, the multi-exhibit bundling rate, the `exhibit_no_extractable_text` rate, and any
  6-K-specific taxonomy gaps (half-year/interim results vs `earnings_release`,
  AGM/proxy-style notices, foreign buyback returns, going-concern buried in an AGM notice),
  iterating the taxonomy data-driven under ADR 0032 if warranted.
