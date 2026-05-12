# 0001. Limit v0 to Form 8-K filings

- **Status:** Accepted
- **Date:** 2026-05-11

## Context

The project ingests SEC filings to surface material events. EDGAR publishes ~20 distinct form types relevant to public companies: annual reports (10-K), quarterly reports (10-Q), current reports (8-K), insider trades (Form 4), beneficial ownership (13D/G), institutional holdings (13F), proxies (DEF 14A), registration statements (S-1), and others. Each has its own ingestion shape, structure, and signal density.

V0 needs to prove an end-to-end loop — ingestion → parse → classify → brief → live dashboard → eval — and ship publicly. Trying to support multiple form types from day one spreads classifier and parser effort thinly across forms before any single form is done well.

## Decision

V0 supports **Form 8-K only**. Additional forms (10-K, 10-Q, S-1, 13D/G, 13F, Form 4, DEF 14A, amendments) are explicit Tier 1 follow-ups but out of scope for v0.

## Alternatives considered

### Multi-form from v0 (8-K + 10-K + 10-Q)

Rejected. Triples the classification, parsing, and eval surface before any one form is shipped. Annual/quarterly reports are long (100–300 pages each) and dominated by boilerplate — chunking and section extraction become real work before any classification can be evaluated.

### Form 4 (insider trades) only

Rejected as the v0 starting form, despite arguably richer signal. Form 4 outputs are short structured XML — the agent loop becomes "extract transaction details," which doesn't exercise the classifier or RAG components that the project's broader story needs to demonstrate.

### Macro / fundamentals (FRED, no filings)

Rejected. Different problem shape entirely (time-series, not document classification), doesn't establish the filings infrastructure the rest of the project requires.

### All-form coverage from day one

Rejected. Each new form roughly doubles ingest/parse/classify scope. Not credible for v0 timelines.

## Consequences

- **Easier:** Single ingestion path, single parser shape, one classification taxonomy (8-K Items are SEC-defined — taxonomy comes free). Eval set is small and tractable.
- **Easier:** Demo aliveness. 8-Ks fire throughout the trading day, peaking near 5pm ET. The dashboard updates live during a demo call. Quarterly forms make for sleepy demos.
- **Harder:** Tier 1 expansion will require care. Code that hardcodes 8-K assumptions everywhere will need rework. Mitigation: name things generically (`classifier.go`, not `8k_classifier.go`), even if only the 8-K classifier is shipped inside v0.
- **Accepted loss:** Several genuinely signal-rich forms (Form 4 insider trades, 10-K/Q amendments) are deferred. This is deliberate — v0 priority is proving the loop end-to-end, not maximum signal coverage.
