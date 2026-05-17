# filings-watcher build recipes.
# Install just (https://just.systems) then run `just --list` to see all targets.

set shell := ["bash", "-euo", "pipefail", "-c"]

# Default recipe — print the menu.
default:
    @just --list

# Run all tests across Python and Go.
test:
    cd orchestrator && uv run pytest
    cd service       && go test -race ./...

# Run every lint + format + type check across both languages and the docs.
lint:
    cd orchestrator && uv run ruff check .
    cd orchestrator && uv run ruff format --check .
    cd orchestrator && uv run mypy src
    cd service       && test -z "$(gofmt -l .)" || (gofmt -l . && exit 1)
    cd service       && go vet ./...
    npx --yes markdownlint-cli2@0.22.1 "**/*.md"

# Lint + test — the gauntlet to run before pushing.
check: lint test

# Build the Go service binary into ./service/filings-server.
build:
    cd service && go build -o filings-server ./cmd/filings-server

# Apply pending DB migrations against the configured FILINGS_DB_PATH.
migrate:
    cd orchestrator && uv run migrate-db

# Sync Python dependencies after a pyproject.toml change.
sync-py:
    cd orchestrator && uv sync

# Sync Go dependencies after a go.mod change.
sync-go:
    cd service && go mod tidy

# Sync both language dependency manifests.
sync: sync-py sync-go

# Delete local branches whose upstream branch is gone (squash-merged, deleted on GitHub, etc.).
cleanup:
    git fetch --prune --quiet
    git branch -vv | awk '/: gone\]/ {print $1}' | xargs -r git branch -D
