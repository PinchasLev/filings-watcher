# filings-server

Read-only HTTP service over the shared filings database. The Python orchestrator (`orchestrator/`) writes filings and classifications; this service serves them.

## Architecture

- **Language:** Go (stdlib `net/http`, no framework — see [ADR 0009](../docs/decisions/0009-go-service-with-stdlib-http.md))
- **Routing:** Go 1.22+ method+pattern routing on `http.ServeMux`
- **SQLite driver:** [`modernc.org/sqlite`](https://pkg.go.dev/modernc.org/sqlite) — pure Go, no CGo, no `gcc` required in CI
- **Database access:** read-only by convention; the orchestrator is the sole writer
- **Schema:** the SQL migrations in `../orchestrator/db/migrations/` are the single source of truth, applied by the orchestrator's `migrate-db` CLI before this service starts

## Layout

```text
service/
├── go.mod
├── cmd/filings-server/main.go   binary entry point
└── internal/
    ├── config/                  env var parsing
    ├── server/                  HTTP handlers, routing
    └── store/                   SQLite read queries
```

## Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{"status":"ok"}` for liveness |
| `GET` | `/filings?limit=N&offset=M` | Latest classifications, paginated, with company info joined |
| `GET` | `/filings/{accession}` | One filing with every classification across all versions |

`limit` defaults to 20 and is clamped to 100. `offset`-based pagination is intentional for v0; cursor-based can replace it with a small isolated change when scale demands.

## Running locally

```bash
# From the repo root:
cd orchestrator
uv run migrate-db                       # create the DB with the current schema
uv run classify-filing AAPL 0 --save    # populate at least one row

# Then in service/:
cd ../service
FILINGS_DB_PATH=$HOME/.filings-watcher/v0.db go run ./cmd/filings-server
```

The server listens on `:8080` by default. Set `LISTEN_ADDR=:9000` (or similar) to change.

```bash
curl http://localhost:8080/health
curl http://localhost:8080/filings
curl http://localhost:8080/filings/0000320193-26-000011 | jq
```

## Testing

```bash
go test ./...      # all tests
gofmt -l .         # format check (must be empty)
go vet ./...       # static analysis
```

Tests share the same migration SQL files used in production — single source of schema truth between the Python writer and the Go reader.

## Concurrency model

The SQLite database file is in WAL journal mode (set by the orchestrator's `open_engine`). WAL allows multiple readers and one writer to operate concurrently without blocking each other. The Go service can run alongside Python classifier runs without contention.
