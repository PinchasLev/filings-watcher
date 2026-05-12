# filings-watcher

Real-time SEC filings monitor with agent-driven event classification and visible model reasoning.

## Status

**Pre-v0** — actively scoping. See [docs/vision.md](docs/vision.md) for the project vision, scope, and success criteria.

## What it does (target v0)

Ingests Form 8-K filings from SEC EDGAR as they publish, classifies each by material event type (executive departure, M&A, restatement, going concern, auditor change), generates a concise brief, and serves a live single-page dashboard for a personal watchlist. Every classification surfaces the agent's reasoning trace alongside the output.

## Stack

| Layer | Language | Role |
|---|---|---|
| Orchestration | Python | LangGraph agents, classification, brief generation, LangSmith tracing |
| Service | Go | HTTP API, SSE streaming, server-rendered dashboard, EDGAR poller |
| Parser | Rust | Filing parsing (burst-tolerant for backfill) |
| Search | Rust | Tantivy-backed MCP server, exposes filing search as an agent tool |

Deploy target: AWS App Runner behind CloudFront + WAF, on a custom domain.

## Planned layout

```text
filings-watcher/
├── docs/               vision, decisions, runbooks
├── orchestrator/       Python: LangGraph agents
├── service/            Go: API, dashboard, SSE, poller
├── parser/             Rust: filings parser crate
└── search/             Rust: Tantivy-backed MCP search server
```

Not all directories exist yet — the repo grows as the project is built.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit conventions, and the PR workflow.

## License

[MIT](LICENSE)
