# 0010. 8-K classifier — LangGraph, Claude tool-use, per-Item granularity

- **Status:** Accepted
- **Date:** 2026-05-14

## Context

The first agentic capability turns a `FilingDocument` produced by the EDGAR layer (see ADRs 0006 and 0007) into structured classifications of the material events disclosed. Three design choices had to be made before any classifier code could be written:

1. **Orchestration shape**: how the classifier is wired together — a single Claude SDK call, a LangGraph, an Agents-style framework, or something else.
2. **Structured output technique**: how Claude's response is constrained to a typed schema rather than free text the caller must parse.
3. **Granularity**: whether classification operates per-filing, per-Item, or at some other unit.

These choices set the shape every downstream capability (entity extraction, brief generation, peer comparison, anomaly scoring) will build on.

## Decision

**Orchestration: LangGraph.** A `StateGraph` with a single classification node. The graph is intentionally trivial at v0; subsequent capabilities attach as additional nodes operating on the same state, and LangSmith tracing flows automatically through the LangChain integrations already configured.

**Structured output: Claude tool-use.** A single tool, `submit_classification`, whose input schema is the `Classification` Pydantic model. The model is bound with `tool_choice` forcing it to call the tool, and the call arguments are validated by Pydantic before being returned to the caller.

**Granularity: per-Item.** Each substantive Item section in the parsed document is classified independently. Pure-supporting Items (initially only Item 9.01 Financial Statements and Exhibits) are skipped. When the document parser produced no Item sections, the classifier falls back to one classification of the whole filing body.

**Taxonomy.** Seventeen categories, grounded in the SEC's Item structure where the mapping is clean, expanded where one Item covers materially distinct events (notably Item 5.02 — `exec_departure`, `exec_appointment`, and `exec_compensation` are separate signals corresponding to the SEC's own 5.02 sub-letters) and where high-signal events have no dedicated Item (notably `material_litigation`, typically disclosed under Item 8.01). Each label carries a description used in the system prompt so the model has consistent definitions to classify against. See "Taxonomy composition and the eval-set path" below for the rationale behind the specific categories chosen.

**Per-section text cap.** Input text is truncated at 12,000 characters per section before being sent. Bounds token cost on outlier filings without losing the substantive disclosure observed in normal 8-Ks.

## Alternatives considered

### Direct Anthropic SDK call, no orchestration framework

Rejected. For a single classification call today, plain SDK use is simpler. Future capabilities (entity extraction, brief generation, cross-filing correlation) will need state across multiple model calls and branching logic. Committing to the LangGraph orchestration framework up front carries a small abstraction overhead at one node; introducing it later, after callers depend on the simpler shape, would carry a larger migration cost.

### Prompt-engineered JSON output

Rejected. Claude can be prompted to emit JSON, and parsing that with `json.loads` and `Classification.model_validate` would work in most cases. Failure modes include trailing prose around the JSON, model-version drift in formatting habits, and silent acceptance of fields outside the schema. Tool-use enforces the schema at the API layer — the model cannot return a non-conforming structure.

### Response prefill / partial assistant message

Rejected. Prefilling `{"event_type":` and parsing what Claude completes is reliable for simple shapes, but requires an ad-hoc parser maintained alongside the schema, and degrades on schemas with optional fields or nested structures. Tool-use is the supported, documented path.

### Per-filing classification (one classification per 8-K)

Rejected. Multi-Item filings genuinely disclose multiple distinct events — Tesla's 2025-11-07 filing disclosed Items 5.02 and 5.07 in the same accession. Collapsing these to a single classification loses information the downstream consumers need.

### Per-paragraph or per-disclosure classification

Rejected as too fine-grained for v0. Items are the SEC's own organizing unit; classifying below that level multiplies token cost, multiplies the chance of mis-aggregating related sentences into separate classifications, and requires a paragraph-segmentation layer we do not have.

### A non-Anthropic structured-output technique (e.g., grammar-constrained decoding from a different model family)

Rejected for v0. The project's model layer is Anthropic-only at this stage. Adding a second model family for this single feature would mean a second API key, a second cost line item, and a second set of error-handling conventions.

## Consequences

- **Easier:** Tool-use schema enforcement gives the caller a typed `Classification` with no parsing layer to maintain. Schema changes are made to the Pydantic model in one place and flow into both the tool spec and the validation step.
- **Easier:** LangGraph state passes cleanly to future nodes. Entity extraction (Tier 1) attaches as a second node consuming the classification output without restructuring the orchestrator.
- **Easier:** Per-Item granularity preserves multi-event signal in the filings stream — a Tesla 5.02+5.07 filing produces two classifications, each independently consumable by alerts or the dashboard.
- **Harder:** A multi-Item filing makes N model calls instead of one. Sequential at v0; parallel-edge LangGraph or asyncio gather can replace the loop when classifier latency dominates wall-clock time.
- **Harder:** The taxonomy is finite and explicit. Events outside the seventeen categories collapse to `other_material`; expanding the taxonomy is a deliberate code change rather than a free-form output.
- **Harder:** Tool-use specifies a schema for what the model returns, not the *quality* of what's inside that schema. Confidence calibration and reasoning quality are model-bound, not framework-bound, and become the subject of the eval set work.
- **Accepted commitment:** The classification taxonomy is part of the public API of the orchestrator. Adding a new event type is a coordinated change across taxonomy, system prompt, eval set, and any downstream consumer that branches on event type.

## Taxonomy composition and the eval-set path

The seventeen categories are not derived from an authoritative source — they are an editorial selection across two considerations:

1. **SEC Item alignment where clean.** Items 2.02, 5.02, 4.02, 4.01, 2.06, 5.07, 3.01, 1.03, 2.04, 1.05, 3.02 each map cleanly to a single category. Item 5.02 is split three ways into `exec_departure`, `exec_appointment`, and `exec_compensation`, matching the SEC's own sub-letter structure for the Item (5.02(b)/(c) departures and appointments, 5.02(e) compensatory arrangements). Items 1.01, 1.02, 2.01, 5.01 collapse into `ma_activity` because the distinctions between them rarely matter for our use case.
2. **High-signal events without a dedicated Item.** `material_litigation` and `going_concern` have no single Item code — they appear in catchalls (most commonly Item 8.01) but the language is unmistakable. Surfacing them as named categories is the classifier's primary value over Item-number-only routing.

The taxonomy deliberately leaves some events as `other_material` rather than guessing at the granularity that will be useful later. Examples currently routed there: material customer loss, ordinary debt issuance, reverse stock splits, material related-party transactions. These may become their own categories once eval-set data shows which patterns cluster meaningfully under `other_material`.

**Taxonomy expansion rule.** Add a new category when (a) the SEC's own sub-Item structure supports the distinction, *or* (b) distribution monitoring across the corpus shows a recurring pattern with distinct downstream consequences. Avoid adding categories from intuition alone. The `exec_compensation` category was added under rule (a) after a run on Tesla's 2025-11-07 8-K surfaced the Item 5.02(e) gap — the classifier self-flagged that the disclosed Musk performance award lacked a more specific category match.

The path forward is data-driven: build an eval set covering a diverse sample of filers (large-cap, mid-cap, small-cap, distressed) and measure how `other_material` distributes. Patterns that recur and carry distinct downstream consequences earn their own category in a follow-up; categories that prove low-volume or hard to distinguish from neighbors get rolled back.

## Determinism and the nature of `confidence`

The classifier sets `temperature=0`, which instructs Claude to always select the highest-probability token at each step. This is "deterministic in intent" but not bit-deterministic in practice on a production API endpoint, for three reasons:

- **Floating-point non-associativity.** GPU arithmetic isn't associative; the order of operations in batched inference can shift logit values by small amounts.
- **Batched serving.** Production endpoints batch concurrent requests; the batch composition affects kernel execution paths.
- **Cluster-level routing.** Requests may land on instances with minor differences during rolling deployments.

Observed effects: the `event_type` is highly stable for clear-cut filings (the categorical decision rarely flips); the `confidence` value varies by approximately ±0.02 across repeated runs on the same filing. The reasoning prose paraphrases differently with stable substance.

A related and load-bearing distinction: `confidence` in this system is the model's **self-reported** assessment, generated as part of the tool-call response, not an internal probability metric. The LLM does have token-level probabilities (the softmax distribution over the next token at the moment of generating, say, `earnings_release`), but those probabilities are not exposed through Anthropic's tool-use API as of this writing. What gets returned is the model's *description* of how confident it is, written as a number, based on its training to produce plausible-sounding confidence expressions.

Practical implications:

- `confidence` is interpretable as a coarse signal (high / medium / low) but is not a calibrated probability. A self-reported 0.98 does not imply 98% accuracy of the corresponding `event_type`.
- Fine differences (0.97 vs 0.99) are within the noise floor and should not drive decisions.
- Threshold-based downstream logic (e.g., "auto-alert when confidence > X") needs margin to absorb the observed variance.
- Empirical calibration is a measurable property of the eval set: run the classifier multiple times on the same filing, observe how often `event_type` flips and how `confidence` distributes, and verify whether high-confidence predictions are in fact more often correct than low-confidence ones.

Alternatives that would expose a real internal metric — logprobs (not available via tool-use), self-consistency voting (N samples at temperature>0), verifier-model agreement — are listed under Deferred below. The self-report is sufficient for v0 surface (dashboard display, alert thresholds) when treated as the coarse signal it is.

## Deferred

- **Taxonomy distribution monitoring.** Once persistence lands, classification results across the running corpus must be inspected for distribution shape. Two patterns warrant monitoring: (1) `other_material` share above ~15-20% indicates taxonomy gaps that should be filled with new categories; (2) any single category absorbing a disproportionate share of filings indicates either the classifier selecting the easiest-fitting label rather than the most precise, or a category description in the prompt that is too broad. The monitoring is straightforward — a periodic SQL query over the classifications table — and the threshold-driven re-evaluation of the taxonomy is the closing loop on the eval-set path described above.
- **Parallel per-Item classification.** Sequential calls are simple and sufficient at v0 traffic. When the worker pool processes many filings or one filing has many Items, parallel edges through LangGraph or asyncio gather are the upgrade path.
- **Confidence calibration.** The model emits a 0..1 confidence; whether that score is *calibrated* (e.g., classifications at 0.9 confidence are correct 90% of the time) is a measurable property of the eval set, not the framework. Captured in the eval-set work.
- **Exhibit retrieval.** Some Items reference disclosures in attached exhibits (e.g., Item 2.02 cites a press release as Exhibit 99.1). V0 classifies on the primary document only; richer classification of the actual press release prose is a follow-up that requires the exhibit-fetch capability deferred from ADR 0007.
- **Self-consistency / multiple-sample voting.** Running the classifier N times at higher temperature and taking the majority is a known technique for improving accuracy on borderline cases. Deferred until the eval set surfaces accuracy gaps worth that token cost.
