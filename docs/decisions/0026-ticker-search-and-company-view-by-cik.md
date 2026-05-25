# 0026. Ticker search resolves to a CIK-keyed company view

- **Status:** Accepted
- **Date:** 2026-05-25

## Context

ADR 0025 established `cik` as the stable join key and mirrored SEC's current
CIK → ticker mapping into `cik_tickers`. Filings now carry a ticker, and the
home page displays it, but there is no way to navigate by it: the only filters
are event type and pagination. The natural user query — "show me filings for
AAPL" — has no entry point.

People know tickers, not CIKs, so the search box must accept a ticker. But a
ticker is a poor key to *group* an entity's filings on, and a company name is
worse:

- **Names drift.** `filings.company_name` is the name as reported on each
  filing. Meta and Facebook are one CIK; a substring search for "meta" misses
  every pre-rebrand filing and "facebook" misses every post-rebrand one — the
  same entity splits across name strings.
- **Names collide.** "apple" substring-matches *Apple Inc.* and *Apple
  Hospitality REIT* — distinct CIKs — merging unrelated entities.
- **Tickers are mutable** and historically reusable, safe today only because we
  backfill them from CIK (ADR 0025). They are a presentation label, not an
  identity.

So the design must separate *discovery* (fuzzy, may span entities) from
*grouping* (exact, one entity). Conflating the two — e.g. `WHERE ticker = ? OR
company_name LIKE ?` presented as "a company's filings" — is the specific bug
to avoid.

## Decision

Ticker search is a discovery step that resolves to the stable key, then hands
off to a CIK-keyed grouping view. Two surfaces, one safe key.

- **`GET /?ticker=<symbol>`** — the home handler resolves the symbol to a CIK
  via `cik_tickers` (`LookupCIKByTicker`, exact match on an uppercased input;
  multi-class symbols like GOOG/GOOGL both point at one CIK) and **302-redirects
  to `/companies/{cik}`**. The resulting page is canonical on the stable
  identifier, shareable, and gives "ticker at time of search" semantics for free
  — the symbol is only ever a lookup key.
- **`GET /companies/{cik}`** — the company view. Lists that CIK's material
  classifications, newest filing first, paginated — the same signal-dense
  framing as the home list (`CompanyByCIK`, `WHERE f.cik = ? AND is_material =
  1`). CIK is the only grouping key; never name, never ticker.
- **Identity resolution** prefers the canonical name and ticker from
  `cik_tickers`; for filers absent from SEC's ticker file (funds, foreign
  filers, trusts) it falls back to the as-filed name/ticker on the company's
  most recent filing.
- **Miss handling** distinguishes three outcomes rather than collapsing them
  into one 404:
  - Unresolvable symbol → re-render the home listing with a "no company found"
    notice (HTTP 200; a search that matched nothing is not an error).
  - CIK known to the mirror but with no classified filings yet → render the
    company header with a "tracked, nothing classified yet" state (HTTP 200).
  - CIK unknown to both the mirror and the filings table → HTTP 404.
- The search box is a plain `GET` form in the shared layout (no JavaScript, per
  ADR 0023) and appears on every page.

## Alternatives considered

### Company-name substring search (`/?q=apple`)

Rejected for now. It is a genuine *discovery* feature for the "I know the name,
not the symbol" case, but it reintroduces the drift and collision problems
above, and there is no evidence yet anyone needs it. A ticker fully discharges
the stated motivation. If demand appears, it can be added as a discovery list
that links *into* the same CIK-keyed view — never as a grouping query.

### Render the company listing inline at `/?ticker=AAPL` (no redirect)

Rejected. It bakes a mutable key into the URL, gives the company view no
canonical shareable address, and would need a second code path that filters by
ticker rather than CIK. The redirect keeps one grouping key and one canonical
URL.

### Group the company view on ticker instead of CIK

Rejected. Ticker is unique in the current snapshot only; grouping on it inherits
ticker's instability and would split a company across a rebrand. CIK is the
ADR 0025 join key precisely so downstream features don't have to relitigate
this.

### Show all of a company's filings, including non-material

Deferred. The company view mirrors the home page's material-only stance to stay
signal-dense and reuse the existing query shape. A non-material toggle is a
later UX choice, not part of establishing the view.

## Consequences

- **Easier:** the company view is a single CIK-scoped query reusing the home
  page's ranking and pagination; the search path adds one reverse lookup and a
  redirect. No schema change — `cik_tickers` and the filing columns already
  exist.
- **Easier:** searching a current ticker returns the company's whole material
  history regardless of the ticker on each individual filing, because the join
  goes through CIK (the ADR 0025 payoff, now reachable from the UI).
- **Harder / deferred:** company names and tickers in the home and detail
  listings do not yet *link* to the company view — reaching it requires the
  search box or a direct URL. Linking from listings needs `cik` denormalized
  onto the classification list rows; tracked as a focused follow-up so this
  change stays atomic.
- **Accepted commitment:** searching a *historical* ticker (e.g. `FB`) resolves
  to nothing, same limitation ADR 0025 records — current-state mirror only.
- **Accepted commitment:** a CIK present in `cik_tickers` but with no classified
  filings renders a real "tracked, nothing yet" page, not a 404 — common, since
  the mirror has ~10,000 companies and the corpus covers a small subset.
