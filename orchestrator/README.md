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
└── src/filings_orchestrator/
    ├── __init__.py
    └── smoke_test.py       single-node LangGraph + LangSmith trace
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

## Run the smoke test

```bash
uv run smoke-test
```

Expected output: a short answer from Claude about Form 8-K, plus a pointer to <https://smith.langchain.com/> where the trace will be visible in your `filings-watcher` project.

## Lint and type-check (matches CI)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
