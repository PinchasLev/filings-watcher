# 0037. Form 4 insider-transaction ingest

- **Status:** Accepted
- **Date:** 2026-06-25

## Context

The chosen monetization direction (see `product-monetization-direction`) is an insider-activity signal — insider transactions contextualized by the classified material-event layer. That requires ingesting **Form 4** (Section 16 insider transactions), a filing type we did not previously process.

Unlike 8-K/6-K, a Form 4 is filed as a **structured `ownershipDocument` XML** (issuer, reporting owner + role, and a table of transactions with codes, shares, prices, and a 10b5-1 affirmation). Recon against EDGAR (2026-06-25) confirmed: ~1,000 Form 4s/day (~5× the 8-K+6-K volume), the schema parses cleanly, the ticker is carried in the document, and the 10b5-1 affirmation is a structured `<aff10b5One>` element. Foreign private issuers (the 6-K universe) are exempt from Section 16 and file no Form 4, so this is inherently US-domestic.

## Decision

A dedicated `scan-form4` CLI ingests Form 4 from the EDGAR **daily index** and **deterministically parses** the ownership XML into a new flat `insider_transactions` table — **no LLM**. This keeps it entirely separate from the classify pipeline: no cost cap, no classifier slice (bounded-operator — code parses the facts; the LLM only ever contextualizes against the already-classified event layer, in a later join). One row per non-derivative transaction; **all** transaction codes are stored (sells are needed for later bidirectional signals), with open-market buys and the 10b5-1 flag captured. Idempotent on `(accession_number, txn_seq)`.

## Alternatives considered

### Route `type=4` through the existing classify pipeline

Rejected: Form 4 is structured data needing no semantic judgment, and its ~5× volume + (pointless) LLM cost would pollute the daily cost cap and the classifier slice. A separate, LLM-free path is cheaper and cleaner.

### Near-real-time atom ingest (`getcurrent?type=4`)

Deferred: the atom entry's CIK is the *reporting owner*, which does not host the full-submission `.txt`, so the document URL needs separate resolution. The daily index gives the exact `submission_path` with no guessing, and insider data is not latency-critical — daily is an acceptable v1, and it is the backfill path regardless.

### LLM extraction

Rejected: the XML is fully structured; deterministic parsing is cheaper, faster, and more reliable, and keeps correctness off the model.

## Consequences

- Insider ingest is cheap to run (no LLM) — the cost cap is irrelevant to it; the only constraints are EDGAR rate limits and DB volume.
- Establishes the structured-extraction/parse → store pattern future structured filings reuse.
- A Form 4 reporting only *derivative* (option) transactions persists no rows and so re-parses on a same-date re-run — harmless given idempotent inserts.
- Out of scope here, as follow-ons: scoring (cluster + conviction), the event-context join + queryable surface, the backtest; and permanently out: derivative transactions, 10-Q/10-K event context, and foreign insiders (structurally absent from EDGAR).
- This PR ships the CLI only; a systemd timer to run it in the evening cluster (after the daily index publishes) is a separate infra change. Until then it runs manually (`uv run scan-form4 --date YYYY-MM-DD`).
