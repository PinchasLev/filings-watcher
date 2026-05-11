# 8-K Material Event Watcher — Project Vision

**Status:** pre-v0, committed 2026-05-11
**Repo:** TBD
**Live URL:** TBD
**One-line elevator pitch:** A live agent-driven monitor over SEC filings that classifies material events on a personal watchlist, with transparent model reasoning and a public dashboard.

---

## What it is

A real-time service over SEC EDGAR that ingests Form 8-K filings as they publish, classifies each by material event type, generates a concise human-readable brief, and serves a live single-page dashboard. Built as a portfolio piece aimed at FinTech / hedge fund / bank engineering roles, and as the seed of a future B2B research product.

## Who it's for

- **Today (v0):** me — tracking ~20 companies on a personal watchlist
- **Later:** investment analysts, hedge fund researchers, compliance teams — anyone who reads filings manually today or pays for the convenience layer over free SEC data

## v0 scope — build this, ship this, then stop

- Ingest 8-K filings from EDGAR's submissions feed
- Parse filings to extract company, item numbers, filing date, item text
- Classify each filing into a small taxonomy of event types (executive departure, M&A, restatement, going concern, auditor change, other)
- Persist filings + classifications in Postgres (Aurora Serverless v2)
- Server-rendered single-page dashboard (HTMX + SSE) showing recent filings on the watchlist with live updates
- Surface the agent's reasoning trace per classification (interpretability story)
- LangSmith tracing on every classification
- Eval harness: ~50 hand-labeled filings, target classification F1 ≥ 0.80
- Public deploy: AWS App Runner behind CloudFront + WAF, custom domain, HTTPS
- Kill-switch flag verified to stop inference

**Explicit non-goals for v0:** scoring, signal generation, multi-form support, cross-filing correlation, peer comparison, anomaly detection, backtesting, alerts beyond the dashboard, auth, multi-tenancy, paid API.

## Why 8-K only, for v0

- Smallest vertical slice that proves the loop end-to-end
- Classification taxonomy comes free (8-K Items are SEC-defined labels)
- Best signal-to-noise per filing (1-10 pages, real events, low boilerplate)
- Most demo-able (filings land throughout the day, dashboard updates live during a call)
- Personal usefulness from day one — not a contrived starter

## Vision tiers — for narrative, not for v0 architecture

**Tier 1 — natural extensions (slot into v0 shape, no redesign):**

- Form coverage: 10-K, 10-Q, S-1, 13D/G, 13F, Form 4, DEF 14A
- Amendments (`*/A` filings) — restatement signal in their own right
- Watchlists with email / Slack / webhook alerts

**Tier 2 — intelligence layer (where this becomes a real product):**

- Cross-filing correlation (Form 4 + 8-K linkages, executive trajectories)
- Insider trading + event correlation
- Peer-group comparison on disclosed risk factors
- Sector-level NLP trend mining over Risk Factors / MD&A (Tantivy MCP server earns its keep here)
- Anomaly scoring vs. company's historical disclosure pattern
- Calibrated materiality probability
- Filing-similarity clustering

**Tier 3 — product surface (architectural rework, defer hard):**

- Multi-tenancy with auth, isolation, per-user watchlists
- Custom natural-language alert rules
- Paid API with rate limiting + billing
- Backtesting / signal evaluation framework
- White-label B2B feed

**Discipline rule:** Tier 1 may inform v0 *code organization* (don't write `8k_classifier.go`; write `classifier.go` and ship only the 8-K classifier inside it). Tier 2 informs the README and interview pitch but never touches v0 code. Tier 3 is not designed for.

## Positioning

This is a **research tool**, not an investment-signal product or a compliance system. The output is human-readable briefs; it makes no recommendations and explicitly does not constitute investment advice. Positioning matters for the product story (research tools have a different go-to-market than signals or recos) and for the regulatory surface (citing and summarizing public filings is straightforward; "signal" or "recommendation" services have different liability shape).

## Stack rationale — each language earns its seat

- **Python (LangGraph + LangSmith):** agent orchestration, classification, brief generation. Native ecosystem for the agentic layer.
- **Go:** HTTP API, SSE streaming, server-rendered dashboard, scheduled EDGAR poller. Right tool for a small concurrent service.
- **Rust:** filings parser (burst-tolerant for backfill) + Tantivy-backed MCP server exposing filing search as a tool to the Python agents. Rust sits *inside the agent loop*, not hidden in a backend job — which is the whole point.

## Success criteria for v0

1. Public HTTPS URL on a custom domain returns the dashboard
2. New 8-K filings appear within 5 minutes of EDGAR publication
3. Classification F1 ≥ 0.80 on a hand-labeled 50-filing eval set
4. Every classification on the page has a clickable LangSmith trace
5. Monthly cost ≤ $50 with the budget alarm in place
6. Kill-switch flag verified end-to-end (flip it, inference stops)
7. README + this vision doc are publishable as portfolio artifacts

## "Done" looks like

A 2-minute cold demo: *"I track these 20 companies. Here's what's landed this week. Here's the model's reasoning on this restatement 8-K. Here's the LangSmith trace. Here's the cost breakdown. Here's where this goes next."* If I can give that demo, v0 is done.
