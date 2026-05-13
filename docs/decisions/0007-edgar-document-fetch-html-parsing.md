# 0007. EDGAR document body fetch and HTML parsing

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

The metadata feed established in ADR 0006 returns 8-K filings with a URL to the primary document, but not the document body. The classifier needs the prose under each Item to do anything more than mirror the SEC's coarse Item taxonomy.

EDGAR 8-K primary documents are HTML files with several practical complications:

- **Inline XBRL**: filings often embed structured XBRL tags inside the HTML, producing extra elements that contribute nothing to readable prose.
- **Layout-driven structure**: filings are formatted with nested tables and styling rather than semantic HTML; section boundaries are visual conventions, not `<section>` tags.
- **Heading variability**: Item headings appear as "Item 5.02 Departure of Directors...", "ITEM 5.02.", "Item 5.02 — Departure of Directors", or as separate paragraphs. There is no canonical form.
- **Boilerplate**: cover pages, signature lines, and exhibit lists surround the substantive disclosures.

The classifier consumes this output, so the parsing produces a structure the LLM can reason over: clean text plus a best-effort split of that text into per-Item sections.

## Decision

Body fetch and parsing live in `edgar/document.py`:

- HTTP fetch uses the existing `EdgarClient` (User-Agent + rate limit enforced).
- HTML parsing uses **BeautifulSoup 4 with the lxml backend**. Script, style, noscript, and HTML-comment nodes are stripped; the remaining text is extracted with newline-separated block boundaries.
- Whitespace normalization collapses runs of spaces, tabs, and non-breaking spaces, and limits blank lines to one between paragraphs.
- Item-section splitting matches an "Item N.NN" heading regex against each text line, then carves the text between consecutive headings into `ItemSection` entries. When no headings can be located (some filings render Items inline rather than as distinct lines), the section list is empty and callers fall back to the full body.

The output is a `FilingDocument` Pydantic model carrying the original `Filing` metadata, the cleaned full-text body, the per-Item sections, and the raw HTML byte count for observability.

## Alternatives considered

### Pure-text extraction with trafilatura or similar article-extraction libraries

Rejected. Tools like `trafilatura` are tuned for journalism — they aggressively strip "non-content" elements including tables, which 8-Ks rely on for substantive content (e.g., the exhibit list in Item 9.01). The aggressive cleanup that helps article extraction works against us here.

### html.parser (stdlib only, no third-party deps)

Rejected. The stdlib parser handles well-formed HTML but copes poorly with malformed real-world filings. The marginal dependency cost of bs4+lxml is justified by the improvement in robustness against the kinds of HTML EDGAR serves.

### Faster parsers (selectolax)

Rejected for v0. selectolax is meaningfully faster than bs4+lxml on large documents, but the hot path is bounded by network fetch and downstream LLM latency, not parser speed. The bs4 ecosystem is sufficient at this stage. The interface in `_extract_plain_text` is narrow enough that swapping parsers later is a small change.

### Structured Item extraction via XBRL tags

Rejected. EDGAR's inline-XBRL tags identify some structural elements but coverage is inconsistent across filings and filers. Relying on XBRL would work for filings with rich XBRL and fail silently on those without. The heading-based split is more uniform across the corpus.

### Strict parsing that errors on unknown structures

Rejected. The variability in real 8-K HTML means strict parsing would error on a large fraction of legitimate filings. Best-effort with a clear fallback (empty `items` list, but `text` always present) keeps the pipeline running on edge cases and lets the classifier still see the body.

## Consequences

- **Easier:** The classifier receives consistent structure across filings: clean text always, item sections when available. Logic that prefers per-Item context can use `document.items`; logic that prefers the full body uses `document.text`.
- **Easier:** Section splitting is a single regex against extracted text. The regex tolerates the visible variants of Item headings ("Item 5.02", "ITEM 5.02.", and forms with EN DASH or EM DASH separators).
- **Harder:** Filings whose HTML renders Item headers inside table cells or as inline span elements (not as their own text lines) won't split. The fallback to full-body text keeps the classifier functional but loses per-Item granularity. Future work: a secondary extraction pass operating on the parsed DOM rather than the extracted text could recover these cases.
- **Harder:** Parsing is synchronous and serial. Backfill across many filings will benefit from concurrency; the parser is pure-function except for the network fetch, so threading is straightforward when needed.
- **Accepted trade-off:** Some prose is lost during whitespace normalization (deliberate column alignment, ASCII-art separators). This material is rarely substantive and the simplified text is what the classifier needs.

## Deferred

- **Local caching of fetched documents.** Filings are immutable once filed; the URL content never changes. A simple on-disk cache keyed by accession number would eliminate repeat fetches during development and eval-set construction. Needed when the eval-set work begins.
- **Exhibit retrieval.** Many 8-Ks reference exhibits (e.g., the press release in Item 2.02 is attached as Exhibit 99.1) that live as separate documents in the same accession folder. V0 classification uses only the primary document; exhibits become relevant if the classifier later needs to read the press release verbatim.
- **PDF and non-HTML primaries.** A small fraction of filings have a PDF primary document. V0 ignores these and the empty-text result will surface them; a follow-up can add PDF extraction (`pypdf` or similar) when needed.
