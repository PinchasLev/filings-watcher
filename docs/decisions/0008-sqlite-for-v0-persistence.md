# 0008. SQLite for v0 persistence

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

V0 needs persistent storage for filings (metadata + parsed body) and classifications produced by the orchestrator. Two processes touch the data:

- **Python orchestrator** — writes new filings as they are ingested from EDGAR; writes classifications as Claude produces them.
- **Go service** — reads filings and classifications to serve the dashboard.

Both run on the same machine (locally during development, and on a single deploy target when first deployed). The expected v0 corpus is in the low thousands of filings — a 20-company watchlist with ~5-15 8-Ks per company per year.

The mainstream choices for the storage layer are SQLite (file-based, embedded) or Postgres (network-attached, managed via Aurora Serverless on AWS).

## Decision

V0 uses **SQLite** as the persistence layer. The database file lives in a path configured by env var (`FILINGS_DB_PATH`, defaulting to `~/.filings-watcher/v0.db` for local dev). The Python orchestrator and Go service open the same file. Schema is defined in a single SQL migration file checked into the repo.

To keep the eventual migration to Postgres mechanical:

- SQL is written in portable form — no SQLite-specific extensions or quirks
- Both the Python and Go sides use thin abstraction layers (Python: SQLAlchemy Core; Go: `sqlc` with the standard `database/sql` driver) that target standard SQL
- Schema avoids SQLite's permissive type affinity by using explicit, typed columns

## Alternatives considered

### Aurora Serverless Postgres

Rejected for v0. Costs ~$15-20/month minimum and adds operational surface: security groups, connection strings managed as secrets, dev environments needing a remote DB or local Postgres install, backup/restore drills, migration tooling configuration. None of Postgres's distinguishing features (concurrent multi-writer access, network access from multiple services, advanced query planning, JSONB / full-text search) are needed in v0.

### DynamoDB

Rejected. Document-store ergonomics fit poorly for the relational shape of the data (filings have a clean N:1 with classifications, both have time-series query patterns). The cost of modeling filings on a key-value substrate exceeds the benefit. DynamoDB is also more expensive at small scale than people expect once on-demand pricing kicks in.

### Postgres on a small EC2 / RDS instance (not Serverless)

Rejected. All of Aurora Serverless's downsides plus continuous billing during idle, manual backup configuration, and a real upgrade dance. Worse than Aurora Serverless on every axis except predictability of cost.

### Local DuckDB or LMDB

Rejected. DuckDB is excellent for analytics but its concurrent-write story for a long-running writer process is not what we need. LMDB is a key-value store; same modeling complaint as DynamoDB.

### No persistence — recompute classifications on each request

Rejected. Each Claude call costs real money and adds seconds of latency. Persisting results once written is the obvious move.

## Consequences

- **Easier:** Zero operational overhead. The DB file is in the repo's working directory during dev; backup is `cp file.db file.db.bak`. Schema lives in source control.
- **Easier:** Dev environment setup is one less step. New contributors don't need to install or run a database server.
- **Easier:** No connection strings, no secret rotation, no network hops between the service and the storage layer.
- **Harder:** A migration to Postgres will happen eventually if the product matures past single-machine scope. Mitigated by writing portable SQL and using DB abstractions from day one — the migration becomes a config + connection change, not a SQL rewrite.
- **Harder:** SQLite is single-writer at the file level. Concurrent write attempts block briefly. Acceptable when only one writer (the orchestrator) ever writes.
- **Accepted commitment:** No SQLite-specific features in SQL queries. No reliance on permissive typing. Schema migrations applied via a portable runner (Alembic for Python, golang-migrate for Go, or a shared SQL-file approach).

## Migration triggers (when to revisit)

- The dashboard takes writes (e.g., user adds tickers to a watchlist via UI) — multiple writer paths
- Data volume crosses the threshold where single-file performance is concerning (typically tens of GB)
- The deploy shape requires DB access from multiple service instances or regions
- A Postgres-specific feature becomes load-bearing (tsvector full-text search, GIN on JSONB for nested classification metadata)

Until any of these is true, the cost of Postgres is paid without benefit, and the discipline is to wait.
