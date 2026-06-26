# 0038. Form 4 ingest robustness — envelope anchor + resumable cursor

- **Status:** Accepted
- **Date:** 2026-06-26

## Context

The v1 Form-4 ingest (ADR 0037) shipped as a single daily-index pass for "today," with dedup keyed off `insider_transactions`. Operating it exposed gaps relative to the mature 8-K daily-index path (ADR 0021):

1. **Leaky dedup anchor.** `insider_transactions` only gets rows when a Form 4 has non-derivative transactions. Roughly half of Form 4s are option-only (derivative table) and store nothing there — so they were never recorded as "seen," re-fetched on every run, and made `entries_new` plateau above zero. There was no clean signal for "this index date is fully ingested."
2. **No catch-up.** The scan only ever processed "today," so a day left partial (a tick cut short by the timeout, a big day, transient EDGAR errors) was never revisited — the timer moved on and the tail was stranded.
3. **Not provably resumable.** An aborted tick could leave a silent gap.

The 8-K path does not have these problems because (a) every 8-K is anchored in the `filings` table regardless of content, and (b) a cursor advances only past fully-ingested dates, so an aborted tick resumes.

## Decision

Bring Form-4 ingest to 8-K-level robustness:

1. **`insider_filings` envelope** — one row per *processed* Form 4, written even for option-only filings (zero non-derivative rows) and fetched-but-unparseable documents (`parsed=0`, null issuer/owner). Dedup keys off this table, not `insider_transactions`. This is the anchor `filings` already provides for 8-K. Existing `insider_transactions` accessions are backfilled into it by the migration so they are not re-fetched.
2. **`form4_ingest_cursor`** — a separate singleton cursor (Form-4 progresses independently of the 8-K path). `scan-form4` scans from the cursor's date through today (ET), and **advances the cursor past an index date only once that date is fully ingested** — no per-filing errors, nothing deferred by the per-tick budget. An aborted tick never reaches the advance, so the next run (the evening cluster's later fires, or the next day) resumes from the incomplete date and fills the gap. Transient fetch failures hold the cursor and retry; a non-business day (403) is skipped without advancing and re-checked cheaply until a later date carries the cursor past it.

`--date` remains as a manual single-date override that neither reads nor advances the cursor.

## Alternatives considered

- **Rolling window (today + last N days)** instead of a cursor — simpler, but bounded by N and re-fetches a fixed span every run. The cursor catches up an arbitrary gap and only re-scans the trailing complete date. Chosen for true 8-K parity.
- **Store derivative transactions to fix dedup** — storing option transactions does shrink the gap (most option-only filings would then have rows), and it is worth doing for its own sake (the next change). But it does not cover transaction-less filings, and the dedup anchor should not depend on transaction content. The envelope is the clean, content-independent fix; derivative storage is complementary.
- **Advance the cursor past a date with errors (liveness over completeness)** — avoids a poison filing blocking the cursor, but silently drops the failed filing. Rejected for the completeness bias funds need; the persistent-poison case is the already-tracked dead-letter follow-up, made visible by the `form4_failed` events.

## Consequences

- `entries_new → 0` is now a true "date complete" signal; the evening cluster + cursor guarantee a day completes across fires even if any single tick is cut short.
- A persistently-unparseable-yet-fetched filing is anchored (`parsed=0`) so it does not re-fetch forever; a filing whose *fetch* keeps failing holds the cursor and retries — the known poison-filing/dead-letter case (parked, visible via `form4_failed`).
- `insider_filings` duplicates the filing-level fields also denormalized in `insider_transactions`. Accepted for now to avoid churning the v1 table; a later normalization can slim `insider_transactions` to transaction fields keyed by the envelope.
- This robustness is the precondition for the historical backfill (it can now know when a date/range is complete and not re-fetch redundantly).
