# filings-orchestrator

Python agent layer for filings-watcher. Owns LangGraph orchestration, LLM-based classification, brief generation, and LangSmith tracing.

## Status

Scaffold only. The smoke test proves the wiring (Anthropic API key → LangGraph node → LangSmith trace) works end-to-end. Real classification logic lands in follow-up PRs (see [docs/vision.md](../docs/vision.md)).

## Layout

```text
orchestrator/
├── pyproject.toml          uv-managed project; Python 3.13 pinned
├── uv.lock                 dependency lockfile
├── .env.example            copy to .env (gitignored) and fill in real values
├── src/filings_orchestrator/
│   ├── __init__.py
│   ├── config.py           secrets/config seam
│   ├── smoke_test.py       single-node LangGraph + LangSmith trace
│   ├── edgar/              EDGAR client and filing data structures
│   └── cli/                command-line entry points
└── tests/                  pytest suite with respx-mocked EDGAR responses
```

## Setup

```bash
# From the repo root:
cd orchestrator
uv sync                     # installs deps from uv.lock into .venv
cp .env.example .env        # then edit .env with your real keys
```

Required env vars (see [.env.example](.env.example)):

- `ANTHROPIC_API_KEY` — from <https://console.anthropic.com/settings/keys>
- `LANGSMITH_API_KEY` — from <https://smith.langchain.com/settings>
- `EDGAR_USER_AGENT` — descriptive string ending in your contact email (SEC requires this on every request)

## Commands

```bash
uv run smoke-test                       # verify LangGraph + LangSmith + Anthropic wiring
uv run fetch-edgar AAPL                 # list recent 8-K filings for a ticker
uv run fetch-edgar AAPL --limit 5       # limit to 5 most recent
uv run fetch-edgar AAPL --detail 0      # also fetch the body of the first filing
uv run pytest                            # run the test suite
```

## Lint and type-check (matches CI)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
