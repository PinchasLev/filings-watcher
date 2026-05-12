# 0002. Polyglot stack — Python orchestrates, Go serves, Rust sharpens

- **Status:** Accepted
- **Date:** 2026-05-11

## Context

The project has three roughly separable concerns:

1. **Agent orchestration** — LLM calls, classification, brief generation, tracing
2. **Service layer** — HTTP API, SSE streaming, server-rendered dashboard, EDGAR poller
3. **Heavy text work** — burst-tolerant filings parsing, full-text search over an indexed corpus

A single-language stack is the default move for solo projects. This project resists that default deliberately.

## Decision

Each concern is implemented in the language best suited for it:

- **Python (LangGraph + LangSmith)** — agent orchestration, classification, brief generation
- **Go** — HTTP API, SSE streaming, server-rendered dashboard, scheduled poller
- **Rust** — filings parser (burst-tolerant for backfill) and a Tantivy-backed MCP server exposing filing search as a tool the Python agents call

Each language's role is load-bearing: removing any one leaves a real gap or forces a worse tool into the wrong job.

## Alternatives considered

### Single-language Python

Rejected. Python is right for the agent layer but a poor fit for the HTTP/SSE service and a worse one for high-throughput parsing. The Tantivy-class search component has no native Python equivalent at the same performance tier.

### Single-language Go

Rejected. The agent ecosystem (LangGraph, LangSmith, model SDKs, eval tooling) is Python-native. Building this in Go means reinventing or wrapping that ecosystem; it's possible but it sacrifices the project's whole agent-orchestration story for ideological language uniformity.

### Python + Rust (drop Go)

Rejected. Python could in principle host the HTTP/SSE service via FastAPI + Uvicorn, but the small concurrent service shape is exactly where Go shines. More importantly, Go is in the user's existing toolkit; dropping it for "simplicity" would cut a tool the user already knows in favor of stretching another tool into a worse fit.

### Add a fourth language (e.g., TypeScript for the frontend)

Rejected for v0. The frontend is server-rendered HTML + HTMX + SSE — no SPA, no build step, no TS compiler. Adding TS would mean adding a build pipeline for a frontend that doesn't need one.

## Consequences

- **Easier:** Each component is built in the language it's best at. Each language's role is defensible in an interview ("why is this in Rust?" → measured perf win on backfill + Tantivy doesn't have a Python equivalent at this tier).
- **Easier:** The polyglot story itself becomes interview signal. Each language earns its seat through architecture, not vanity.
- **Harder:** Three toolchains to maintain (Python via `uv`, Go modules, Rust via `cargo`). Three sets of CI gates. Three deploy artifacts.
- **Harder:** Inter-language boundaries — MCP between Python agents and the Rust search server, HTTP between Python and Go for any cross-component calls. Each boundary is a place serialization can go wrong.
- **Accepted commitment:** This decision implies every component must demonstrably benefit from its language choice. A Rust component that doesn't measurably outperform a Python one is a Rust component that hasn't earned its seat — and should either be moved or replaced.
