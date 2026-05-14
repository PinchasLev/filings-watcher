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

**Taxonomy.** Eleven categories, grounded in the SEC's Item structure where the mapping is clean, expanded only where one Item covers materially distinct events (notably Item 5.02 — `exec_departure` and `exec_appointment` are separate signals). Each label carries a description used in the system prompt so the model has consistent definitions to classify against.

**Per-section text cap.** Input text is truncated at 12,000 characters per section before being sent. Bounds token cost on outlier filings without losing the substantive disclosure observed in normal 8-Ks.

## Alternatives considered

### Direct Anthropic SDK call, no orchestration framework

Rejected. For a single classification call today, plain SDK use is simpler. Future capabilities (entity extraction, brief generation, cross-filing correlation) will need state across multiple model calls and branching logic. LangGraph commits to the orchestration story up front; the abstraction overhead at one node is small, and the migration cost of adding LangGraph later, after callers depend on the simpler shape, would be larger.

### Prompt-engineered JSON output

Rejected. Claude can be prompted to emit JSON, and parsing that with `json.loads` and `Classification.model_validate` would work most of the time. Failure modes include trailing prose around the JSON, model-version drift in formatting habits, and silent acceptance of fields outside the schema. Tool-use enforces the schema at the API layer — the model literally cannot return a non-conforming structure.

### Response prefill / partial assistant message

Rejected. Prefilling `{"event_type":` and parsing what Claude completes is reliable for simple shapes, but introduces an ad-hoc parser the team maintains, and degrades on schemas with optional fields or nested structures. Tool-use is the supported, documented path.

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
- **Harder:** The taxonomy is finite and explicit. Events outside the eleven categories collapse to `other_material`; expanding the taxonomy is a deliberate code change rather than a free-form output.
- **Harder:** Tool-use specifies a schema for what the model returns, not the *quality* of what's inside that schema. Confidence calibration and reasoning quality are model-bound, not framework-bound, and become the subject of the eval set work.
- **Accepted commitment:** The classification taxonomy is part of the public API of the orchestrator. Adding a new event type is a coordinated change across taxonomy, system prompt, eval set, and any downstream consumer that branches on event type.

## Deferred

- **Parallel per-Item classification.** Sequential calls are simple and sufficient at v0 traffic. When the worker pool processes many filings or one filing has many Items, parallel edges through LangGraph or asyncio gather are the upgrade path.
- **Confidence calibration.** The model emits a 0..1 confidence; whether that score is *calibrated* (e.g., classifications at 0.9 confidence are correct 90% of the time) is a measurable property of the eval set, not the framework. Captured in the eval-set work.
- **Exhibit retrieval.** Some Items reference disclosures in attached exhibits (e.g., Item 2.02 cites a press release as Exhibit 99.1). V0 classifies on the primary document only; richer classification of the actual press release prose is a follow-up that requires the exhibit-fetch capability deferred from ADR 0007.
- **Self-consistency / multiple-sample voting.** Running the classifier N times at higher temperature and taking the majority is a known technique for improving accuracy on borderline cases. Deferred until the eval set surfaces accuracy gaps worth that token cost.
