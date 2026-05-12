# 0003. Position v0 as a research tool; investment-grade signal product is the end-state

- **Status:** Accepted
- **Date:** 2026-05-12

## Context

The project's ultimate value is an early-warning system for corporate deterioration — detecting companies showing leading indicators of subsequent material events before those events fully surface in the filings stream. Audience for the end-state: hedge funds, credit analysts, short-sellers, M&A diligence teams.

V0 produces a 8-K classifier with human-readable briefs on a watchlist. That is *not* the end-state product. The question for positioning was whether the doc, README, and interview narrative should:

- **(a)** Describe v0 as the product (and risk the project reading as smaller than it is)
- **(b)** Describe the end-state as the product (and risk the project reading as overpromised against what v0 actually ships)
- **(c)** Frame v0 as a deliberate intermediate state on a credible phased path to the end-state

This affects the product story, the interview pitch, and — non-trivially — the legal surface, since publicly labeling specific companies as "likely to fail" carries defamation exposure that descriptive research tooling does not.

## Decision

Option (c). V0 is positioned as a **research tool** — human-readable briefs over public filings, no recommendations, not investment advice. The vision doc explicitly names the end-state as an early-warning signal product and treats v0 as foundation, not destination.

Tier 2 (cross-filing correlation, anomaly scoring, calibrated materiality probability, peer comparison, etc.) is named explicitly as the layer that turns the research tool into the signal product. Tier 2 capabilities earn their seat by contributing to the end-state, not by sounding interesting.

The methodology constraints required for the end-state — no survivorship bias in training data, no look-ahead bias in features, evaluation calibrated to low base rates, output framing aware of legal surface — are stated in the vision doc up front.

## Alternatives considered

### Describe v0 as the product

Rejected. Reads as smaller ambition than the project actually is. Interviewers and future-self lose the gravitational pull that the end-state provides on Tier 2 prioritization.

### Describe the end-state as the product

Rejected. Creates a credibility gap: someone reading "early-warning signal product" and seeing a demo of "I classify 8-Ks into 5 buckets" walks away skeptical. Also creates legal exposure: claiming a signal product on free public-filings data invites scrutiny we don't want at the portfolio stage.

### Stay deliberately vague about end-state

Rejected. Vague positioning is a weaker story everywhere. Naming the end-state up front and committing to a phased path is the senior framing.

## Consequences

- **Easier:** Interview narrative writes itself — "v0 is the foundation; here's the disciplined path to the signal product." Tier 2 prioritization has a clear north star.
- **Easier:** Legal posture stays clean. V0 is descriptive research tooling on public data; signal-style claims arrive only when methodology supports them.
- **Harder:** The discipline of *not* shipping signal/scoring in v0 must be held. Every "but a quick score would be cool" temptation must be resisted. The vision doc and this ADR are the receipts.
- **Accepted commitment:** Tier 2 work must rigorously honor the stated methodology constraints (survivorship-bias-free training data, feature timestamps respected, calibration for low base rates, legally aware output framing). Shortcuts here would invalidate the entire positioning.
