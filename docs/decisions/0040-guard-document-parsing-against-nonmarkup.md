# 0040. Guard document parsing against non-markup / oversized content

- **Status:** Accepted
- **Date:** 2026-07-10

## Context

A production incident: a Ternium 6-K (`0001554855-26-001510`, filed 07-08) carried a **27.6 MB PDF** as Exhibit 99.1. The exhibit-ingestion path fetched it and handed it straight to BeautifulSoup/lxml — an HTML parser — because `fetch_filing_document` decoded every fetched document to a string and parsed it with no content-type check. lxml on 27 MB of binary builds a pathological tree and BeautifulSoup wraps every node in a heavyweight object; RSS climbed to **~2.08 GB** and the process was OOM-killed at the classifier slice's 2 GB cap.

Because the OOM (SIGKILL) leaves the filing uncommitted, both the atom feed and the daily-index reconciler re-fetched it and died again every run — **freezing all ingestion for ~2 days**. The atom feed only recovered because the filing aged out of EDGAR's rolling `getcurrent` window; the daily-index re-scans a fixed date's index, so it stayed stuck.

The resolver keys exhibits on their **type** ("EX-99.1"), not a filename, so there was no reliable extension to filter on either.

## Decision

Detect the document type **from content, before parsing**, and route accordingly:

- `EdgarClient.get_bytes(url)` returns raw bytes + the `Content-Type` header.
- `_document_kind(content, content_type)` classifies content-first (magic bytes, then Content-Type): `pdf` / `binary` / `oversized` / `markup`.
- `fetch_filing_document` parses only `markup`; `pdf`/`binary`/`oversized` documents (primary or exhibit) are **skipped**, recorded via a `document_skipped` event, and the filing ingests as metadata-only.
- A **per-document byte cap** (`_MAX_PARSE_BYTES`, 25 MB) backstops oversized *markup* — a markup parser's tree dwarfs its input, so no single document may exhaust the slice regardless of type.

## Consequences

- No single filing can OOM ingestion through the parser again — the type check stops the misparse, the byte cap stops the bomb.
- PDF (and other binary) exhibits are skipped rather than classified. Bounded PDF **text extraction** — with its own memory guards, since a PDF extractor can OOM the same way — is a deferred enhancement; most 6-K PDF exhibits are periodic financial reports we defer anyway.
- This deployed fix is also the unblock for the stuck daily-index: on its next run it processes the Ternium filing (PDF skipped), commits it, advances the cursor, and reconciles the 2-day gap — no manual "mark seen" needed.
- The incident also exposed that the OOM storm (192 handler firings/hour) never paged: an ingest outage ran ~2 days silent. Fixing that OOM → alert → delivery path is a separate follow-up.
