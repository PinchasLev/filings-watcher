# 0042. Disclosure change-detection: surfacing material change in periodic filings

- **Status:** Proposed
- **Date:** 2026-07-23

## Context

We closed the signal chapter deliberately: a year of insider/event data showed no
robust, tradeable alpha (it evaporates the instant a public filing hits EDGAR).
That result pointed us away from prediction and toward what this system is
actually good at — **faithful comprehension of dense, high-stakes prose at scale,
run reliably.** The product thesis is now *AI for comprehension: surface the
issues a filing's consumers need*, not forecast the market.

The concrete job: a filing consumer cannot re-read a 300-page 10-K every year to
find the five paragraphs that changed. We do it for them — *"versus last year,
this company added going-concern language, disclosed a new credit-covenant default
risk, and dropped its product-recall risk."* The buyer is a **risk-monitoring**
consumer (credit, procurement, insurance, counterparty-risk) whose recurring job
is catching deterioration early, and who reads the sleepy sections nobody else
mines. This is a more defensible business than a signal that arbitrages away:
the moat is broad, reliable, evidenced coverage — engineering and data-assembly,
not secret alpha.

A filing changes in two fundamentally different places, and they need different
machinery. The **prose** (risk factors, MD&A narrative) changes in *language* —
a semantic problem. The **numbers** (revenue, margins, leverage) change in
*magnitude* — an arithmetic problem, where the figure often isn't even in the
prose. This decision scopes the **prose half**; the numbers half and their
intersection are named here but sequenced later.

## Decision

Build **disclosure change-detection** as the prose half (call it **A**) of a
combined risk-monitoring product, using an **embed-shortlist → LLM-adjudicate-
materiality** funnel over the diff-relevant sections of periodic filings. Start
narrow — **10-K Item 1A (Risk Factors) only** — and layer breadth (MD&A, 10-Q),
the numbers half (**B**), and their divergence signal (**A×B**) in later arcs.
The approach is validated by two throwaway spikes (below), not assumed.

### The two halves and their seam

- **A — prose change** (this arc): *the story changed.* Embeddings + LLM.
- **B — numeric change** (later): *the magnitude changed.* Deterministic pipeline
  over XBRL — see "Numbers are B's job" below. Embeddings are numeric-blind
  ("declined 5%" and "declined 10%" embed almost identically), and the number is
  often absent from the prose, so A structurally cannot own numeric change.
- **A×B — divergence** (later, the high-value composition): *the numbers got worse
  but the narrative didn't admit it.* B detects the numeric deterioration; A
  detects that the language failed to change to match. "Hard numbers deteriorating
  while soft language stays rosy" is a classic forensic red flag, and it needs both
  pipelines — neither surfaces it alone. This is why the product is fundamentally
  A+B, not A with a bolt-on.

### The funnel (A's pipeline)

A pipeline is a graph of *typed* steps, each assigned to the cheapest capable
actor (cost order: code < embedding < LLM), with **structured, validated hand-offs
between steps** and deterministic guardrails after every LLM step. For A:

1. **Segment** *(code)* — filing HTML → whole risk-factor blocks (bold-header
   split, size-merge fallback), each with a stable identity hash.
2. **Embed** *(embedding model)* — each block → a vector. This translates *meaning*
   into *numbers* so cheap code can compare blocks by meaning, not by keywords
   (a reworded-but-unchanged risk shares few words yet the same meaning).
3. **Diff / shortlist** *(code)* — keyed lookup of the prior period's block vectors,
   cosine-align, classify each block added / changed / carried / dropped. This is
   the **recall** stage: cheap, high-recall, narrows ~dozens-to-hundreds of blocks
   to the handful that moved.
4. **Judge materiality** *(LLM)* — for each shortlisted change, judge *"is the
   **change** material?"* given **both the new block and its closest prior match**,
   returning a structured `{verdict, confidence, flags, explanation}`. This is the
   **precision** stage. The LLM judges the *delta*, not the passage.
5. **Surface** *(code)* — store and display the material changes with citations.

Throughout, bounded-operator: code segments/aligns/stores/compares; the embedding
translates; the LLM only reads and judges prose; the LLM's output is *validated*
before code trusts it.

### What the spikes established (measured, not assumed)

- **Spike 1** (Peloton FY22 vs FY21, Item 1A): embedding-similarity *alone* is the
  wrong detector. 69% of blocks carried verbatim (correctly parked), but the "most
  novel" passages were dominated by *new boilerplate* (ESG/DEI, a short-selling
  definition), not material risk — **semantic novelty ≠ business materiality**, and
  no absolute similarity threshold cleanly separates them. The embedding is a good
  candidate-*generator*, a bad materiality-*judge*.
- **Spike 2** (the full funnel + a distress positive control): whole-risk-factor
  chunking collapsed 617 noisy fragments to 87 clean blocks. The LLM materiality
  pass stripped the boilerplate and kept the real FY22 changes (restructuring,
  exiting in-house manufacturing, new covenant default risk). The **AMC FY20 vs
  FY19 control passed decisively** — the funnel surfaced AMC's known going-concern
  addition repeatedly and explicitly, alongside its workforce cut, deferred rent,
  and theatre closures. Cost is trivial (local/API embeddings + ~dozens of cheap
  LLM calls per filing pair). The one refinement it surfaced — *judge the delta,
  not the passage* — is baked into step 4 above.

### Numbers are B's job, handled as labeled records

Numeric change is out of scope for this arc but its design is fixed so A doesn't
try (and fail) to own it. A number is meaningless without its **label** — the
concept, period, unit, and basis; comparing requires matching on the *concept*, never
grabbing digits. Handled in layers: (1) financial statements → the label is
attached *at the source* by **XBRL** (SEC-mandated), so we read a tagged value,
never guess — the real work is normalizing the messy tag space to canonical
concepts; (2) numbers only in prose (non-GAAP, operational, guidance) → the LLM as
a **structured extractor** emits a labeled record `{metric, period, value, unit,
basis, source}` (reading + labeling is its strength, not arithmetic); (3) code
then compares records matched by `(concept, period)` — a join, where an unmatched
concept is a flagged absence, never a wrong comparison.

### Storage, orchestration, control flow

- **Storage:** a SQLite **feature store keyed by (company, period, section, block)**
  holding each block's text and embedding. Computing a diff *does* require the
  vector math — we compute cosine similarity between this filing's block vectors
  and the prior period's; that comparison is the diff. What we do **not** need is a
  **vector database**: those exist to *search* for similar vectors among millions
  using a specialized index, but here we already know exactly which vectors to
  compare against (the prior period's, fetched by a keyed lookup of a few dozen
  rows), so plain retrieval plus an in-memory cosine calculation is enough. Keying
  by period means pairwise diff and later trend queries both fall out of the same
  store.
- **Orchestration:** the pipeline is *designed as a DAG* but *implemented on the
  existing substrate* — imperative Python CLIs (the step sequence), systemd timers
  (scheduling), SQLite (state/cursor/idempotency), reconcilers (retry/backfill).
  No Airflow/Dagster: disproportionate for a handful of steps on one host. (Revisit
  only if B grows into a real data-asset graph where Dagster's asset model earns
  its keep.)
- **Control flow:** each step emits its answer *plus* validity/confidence signals,
  so deterministic code can branch: **advance** (validated + confident), **flag**
  (low confidence / method disagreement / failed cross-check → a first-class
  needs-review state, never a silent advance), or **retry up an escalation ladder**
  (reformulate → stronger model → decompose input → human). Retries are safe
  because the substrate is idempotent (existing PKs / unique indexes).

### Scope for this arc

- **10-K Item 1A (annual Risk Factors) only** — the cleanest case, matching the
  spikes. A **separate `scan-periodic` path** (not bolted onto the 8-K daily-index
  scanner, which runs a different classify/reduce pipeline).
- We segment and store **only the diff-relevant section**, *not* full-10-K
  comprehension — which sidesteps the size/cost reason we deferred periodic filings
  originally.

## Alternatives considered

### Feed both full filings to the LLM ("what changed?")

Rejected: 276k characters each is too large to compare reliably, expensive, and
uncitable. The LLM alone cannot do comparison-at-scale — this failure is *why* we
decompose into the funnel.

### Embedding-similarity threshold as the materiality detector

Rejected: spike 1 disproved it. Semantic novelty is polluted by boilerplate; no
absolute threshold separates material from immaterial. Embeddings shortlist; the
LLM judges.

### Fine-tune / train a classifier on labeled "material changes"

Rejected: we have no labeled corpus, it adds the full ML lifecycle (labels,
training, drift), and the prompt-driven funnel already works from instructions
alone — no training examples needed. A trained model becomes worth revisiting only
if we accumulate proprietary *outcome* labels years out (changes that preceded real
distress).

### A vector database

Rejected: change-detection is pairwise and keyed (this filing vs its own prior),
not corpus nearest-neighbor search. SQLite storing vectors for keyed retrieval is
sufficient; `sqlite-vec` in-process covers the later cross-sectional rung if ever
needed.

### A DAG framework (Airflow / Dagster)

Rejected for now: a scheduler service + metadata DB + UI + workers is a large
operational surface, disproportionate to a handful of steps on one 2 GB host that
we just hardened. systemd + SQLite is the right-sized orchestrator.

### Extend the 8-K daily-index scanner to periodic filings

Rejected: 10-K/10-Q go through segment → embed → diff, not classify → reduce
(event taxonomy). A separate path keeps the two pipelines clean.

## Consequences

**Easier / what we gain:** material disclosure changes surfaced automatically and
cited; heavy reuse of existing machinery (classify/LLM infra, the Go web surface,
systemd, SQLite, reconcilers, the cost cap); a *light* footprint (no vector DB, no
model training, no new orchestration platform); and a feature store keyed by
(company, period, block) so **trends fall out later as a query, not a rebuild.**

**Harder / costlier — the new burdens we take on:** a new *external dependency*, the
embedding provider — an API key to manage, config to carry, and an outside service
that can fail or add cost; *ongoing segmentation robustness* work, because the
bold-header chunking that worked on our two test filings must generalize to filers
who format their 10-Ks differently (the size-merge fallback and a growing set of
fixture tests exist for exactly this); a small *recurring LLM cost per filing* for
the materiality-judge calls (fractions of a cent, but a cost we didn't have before);
and *new operational surface*, since periodic-filing ingest is something we've never
run — a new scanner, new tables, and new failure modes to build, operate, and
monitor.

**Committed to:** the two-stage funnel; judging the *delta* not the passage;
bounded-operator boundaries with validation after every LLM step; A+B as one
product (B and A×B are planned, not hypothetical); and measure-first — a
corpus-wide precision/recall evaluation before we make coverage claims.

**Accepted losses / deferrals:** numeric change is invisible until B ships; MD&A,
10-Q, the trend/baseline layer, and cross-sectional clustering are later arcs;
first-time filers (fresh IPOs) have no prior to diff and are flagged "no baseline";
and a proprietary trained model is *not* our moat now (the moat is pipeline,
coverage, and reliability — a model is a maybe-someday only if we earn the outcome
labels).

## PR sequence

Built one at a time, off fresh `main`, in order. Each is small, independently
reviewable, and labelled by its dominant actor.

1. **Section-segmentation module** *(code; no DB, no network)* — bold-header
   risk-factor segmentation with the size-merge fallback and stable block-identity
   hashes, unit-tested against saved 10-K HTML fixtures. Retires the "will
   segmentation generalize" risk first.
2. **Periodic-filing ingest + block persistence** *(code; no LLM)* — migration for
   a periodic-filings envelope + a `filing_blocks` table keyed by
   (company, period, section, block_index, block_hash, text); a `scan-periodic` CLI
   that discovers 10-Ks from the daily index (reusing the fetch/parse/cursor
   machinery), fetches, segments (PR 1), and stores blocks. Resumable, with a
   bounded `--since/--until` backfill driver.
3. **Block embeddings (feature store)** *(introduces the embedding dependency)* —
   migration for keyed embedding storage; an embed step over stored blocks using an
   API embedding provider (choose Voyage vs OpenAI here); provider/key config.
4. **Pairwise diff engine** *(code; the recall stage)* — given a filing, look up
   its prior comparable period, cosine-align blocks, classify each
   added/changed/carried/dropped, store the shortlist. Tested against the spike
   cases as fixtures.
5. **LLM materiality judge** *(LLM; the precision stage)* — for each shortlisted
   change, judge *"is the change material?"* given new block + prior match,
   returning `{verdict, confidence, flags, explanation}`; advance high-confidence,
   route low-confidence/disagreement to a review state, escalate malformed cases.
   Reuses the classifier infra, bounded-operator discipline, and the cost cap.
6. **Surface (Go read side)** — a company-page section showing material disclosure
   changes period-over-period, each with its explanation and a citation back to the
   block.

**Explicitly deferred to later arcs:** MD&A + 10-Q support · the "dropped-risk"
materiality pass · the numeric half (B) and the A×B divergence signal · the
trend/baseline layer · cross-sectional clustering · corpus-wide precision/recall
evaluation.
