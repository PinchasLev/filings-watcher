# 0004. Python via uv; monorepo with per-language subdirectories

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

The project's first code lands as a Python package: the agent orchestrator (LangGraph + LangSmith + Anthropic client). Two adjacent decisions had to be made before any code could be committed:

1. **Python tooling.** The Python ecosystem has converged on several competing tools for version management and dependency resolution (`pyenv` + `pip`/`pip-tools`, `poetry`, `pdm`, `hatch`, `uv`). The 2026 default tilts hard toward `uv`, but the user had `asdf-python` available and was familiar with it.
2. **Repo layout.** The vision doc planned `orchestrator/` (Python), `service/` (Go), `parser/` and `search/` (Rust). The decision was whether to keep these as subdirectories in one repo (monorepo) or split into per-language repos (polyrepo).

Both decisions had to land before `uv init` could produce `pyproject.toml` and the first PR could be opened.

## Decision

**Python tooling: `uv`** managed inside `orchestrator/`. The `orchestrator/.python-version` file pins to 3.13. `pyproject.toml` declares dependencies; `uv.lock` is committed for reproducible installs. Dev tooling (`ruff`, `mypy`) is in `[dependency-groups.dev]`. Anthropic SDK and LangSmith arrive via `langchain-anthropic` and `langsmith` rather than direct.

**Repo layout: monorepo with per-language subdirectories**. Each subdir owns its toolchain, lockfile, and CI gate. The top-level repo owns shared docs (`docs/`, `README.md`, `CONTRIBUTING.md`) and cross-cutting workflows (`.github/workflows/ci.yml`).

```text
filings-watcher/
├── orchestrator/   Python via uv
├── service/        Go (planned)
├── parser/         Rust (planned)
├── search/         Rust (planned)
└── docs/           shared docs (vision, ADRs)
```

## Alternatives considered

### Python via asdf-python (instead of uv)

Rejected. The user already uses `asdf` for Go and Node, so consistency was real. But `asdf-python` compiles Python from source (slow) and handles only versions, not dependencies — the user would still need pip+venv or poetry on top. `uv` handles versions, deps, and execution in one tool, downloads pre-built Python binaries, and is the modern default for new Python projects in 2026. The "consistent tooling" win didn't survive the comparison.

### Poetry / PDM (instead of uv)

Rejected. Both are credible but slower than uv, less actively developed in 2026, and don't ship Python version management. Choosing them means adopting a tool that's losing market share to uv among new projects.

### Polyrepo: one repo per component

Rejected. Polyrepo is the right answer when components have *different teams*, *different release cadences*, or *different access controls*. None of those hold for this project today — it's one person, one deploy cadence, one access boundary. Polyrepo's cost (4× the CI/branch-protection/issue-tracker setup, 4× the URLs to share in an interview, harder cross-component refactors) is real and immediate. The split can be done later via `git filter-repo` if the conditions for polyrepo ever appear.

### Monorepo, but with a Bazel/Buck-style build system

Rejected. Bazel-style polyglot build systems are powerful but bring substantial overhead (config language, build cache infra, CI changes). Worth it for a team of 50+ engineers; not worth it here. Per-language native tooling (`uv`, `go mod`, `cargo`) is the right granularity for a solo project.

## Consequences

- **Easier:** Each language uses its native ecosystem tooling. Lock files are real and reproducible. CI gates are independent per subdir — Python lint failures don't block Go work, and vice versa.
- **Easier:** One URL to share in an interview. One README. One CONTRIBUTING.md. One issue tracker.
- **Easier:** Cross-component changes (e.g., when the Python orchestrator starts calling the Rust search MCP server) land as single PRs.
- **Harder:** Each subdir's tooling drift is real — `uv.lock`, `go.sum`, `Cargo.lock` all need maintenance. Mitigated by Dependabot or Renovate once we reach the maintenance phase.
- **Harder:** Conditional path-filter CI to skip the Python job when only Go files change. Deferred until CI run time becomes an actual pain point — premature optimization today.
- **Accepted commitment:** The cost to "split into separate repos later" is bounded but real. Roughly: per-component `git filter-repo`, then reconfigure CI, branch protection, and the README for each new repo. We will pay this cost only when one of polyrepo's actual triggers (different teams, cadences, or access controls) emerges.
