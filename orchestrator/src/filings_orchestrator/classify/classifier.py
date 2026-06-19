"""LangGraph-based 8-K classification with Claude tool-use.

Architecture:

- One LangGraph node per classification request. The graph itself is
  intentionally simple at v0; future capabilities (entity extraction,
  brief generation, peer comparison) attach as additional nodes operating
  on the same state.
- Structured output via Claude's tool-use API. We bind a single tool
  whose JSON Schema is the `Classification` Pydantic model, and force the
  model to call it. This is more reliable than prompt-engineered JSON and
  more robust to model upgrades than text parsing.
- Per-Item granularity. A filing's items are classified independently;
  filings with no extractable Items get a single whole-filing
  classification.

LangSmith tracing is automatic when LANGSMITH_TRACING=true is set in the
environment (loaded by `config.load_config()` at process startup).
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from filings_orchestrator.classify.exhibits import render_exhibits
from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.classify.taxonomy import (
    EVENT_TYPE_DESCRIPTIONS,
    NON_SUBSTANTIVE_ITEMS,
    TAXONOMY_VERSION,
    EventType,
)
from filings_orchestrator.cost import emit_llm_call
from filings_orchestrator.edgar.document import FilingDocument, ItemSection

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Cap per-section text to keep tokens (and cost) bounded even on outlier
# filings. 12,000 chars ≈ 3,000 tokens on prose; comfortably under any
# context budget while preserving the substantive disclosure in every 8-K
# we have observed in development.
_MAX_SECTION_CHARS = 12_000


class _State(TypedDict):
    document: FilingDocument
    model: str
    # The leaves (EventType members) the classifier may choose from. None means
    # the full in-code taxonomy — the production default. A subset is used to
    # evaluate a taxonomy change (e.g. classify-ab) by offering one version's
    # choice-set; the model is constrained to it via both the prompt and the
    # tool-schema enum. See ADR 0032.
    leaves: list[EventType] | None
    items: list[ItemClassification]
    whole_filing: Classification | None


def _build_system_prompt(leaves: list[EventType] | None = None) -> str:
    # `leaves is None` enumerates the full in-code taxonomy in declaration order
    # — byte-identical to the production prompt, so the default `classifier_version`
    # is unchanged. A subset offers only that version's choice-set.
    event_types = leaves if leaves is not None else list(EventType)
    lines = [
        "You are an experienced securities analyst classifying SEC Form 8-K material event "
        "disclosures. You will be shown one section of an 8-K filing — typically a single "
        "Item — and must classify it into the taxonomy below using the provided tool.",
        "",
        "Classify based on what the prose actually discloses, not on the Item number alone. "
        "An Item 5.02 filing may be a departure, an appointment, or both — choose the most "
        "salient event the prose centers on.",
        "",
        "Event types:",
    ]
    for event_type in event_types:
        lines.append(f"- {event_type.value}: {EVENT_TYPE_DESCRIPTIONS[event_type]}")
    lines.extend(
        [
            "",
            "Mark `is_material` true when the disclosure would affect a reasonable "
            "investor's assessment of the registrant — restatements, going-concern, "
            "executive changes, M&A activity, material impairments, delisting risks, "
            "and most earnings releases qualify. Routine administrative disclosures "
            "(e.g., bylaw amendments) do not.",
            "",
            "Confidence reflects genuine uncertainty about the event_type. Reasoning is "
            "1-3 sentences citing the specific phrases or named entities that drove your "
            "choice. Make every reasoning trace defensible to another analyst.",
        ]
    )
    return "\n".join(lines)


def _build_user_message(
    document: FilingDocument,
    item: ItemSection | None,
    exhibit_block: str = "",
) -> str:
    filing = document.filing
    header = (
        f"Company: {filing.company_name} (CIK {filing.cik}, ticker {filing.ticker or 'n/a'})\n"
        f"Filing date: {filing.filing_date.isoformat()}\n"
        f"Form: {filing.form}\n"
    )
    # Exhibits are shared context for whichever item (if any) they bear on, so
    # the same block is appended to every item's prompt and to the whole-filing
    # fallback. Empty when the filing has no EX-99 exhibits.
    suffix = f"\n\n{exhibit_block}" if exhibit_block else ""
    if item is not None:
        body = item.text[:_MAX_SECTION_CHARS]
        section_header = f"Item {item.number}"
        if item.title:
            section_header += f": {item.title}"
        return f"{header}\nSection under classification: {section_header}\n\n{body}{suffix}"
    body = document.text[:_MAX_SECTION_CHARS]
    return (
        f"{header}\nNo Item sections were extractable. Classify the whole filing body:"
        f"\n\n{body}{suffix}"
    )


def _tool_input_schema(leaves: list[EventType] | None) -> dict[str, Any]:
    """The Classification JSON schema, with event_type constrained to `leaves`.

    `leaves is None` returns the schema unchanged (the full taxonomy). A subset
    deep-copies the schema and narrows the `EventType` enum to those values, so
    the model cannot return a leaf outside the offered choice-set — the
    tool-schema counterpart to the prompt restriction.
    """
    schema = Classification.model_json_schema()
    if leaves is not None:
        schema = copy.deepcopy(schema)
        schema["$defs"]["EventType"]["enum"] = [leaf.value for leaf in leaves]
    return schema


def _bind_classifier(model_name: str, leaves: list[EventType] | None = None) -> Any:
    """Build a Claude model bound to the Classification tool, forced to call it."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_classification",
        "description": (
            "Submit the classification for the section. Must be called exactly once per request."
        ),
        "input_schema": _tool_input_schema(leaves),
    }
    return model.bind_tools(
        [tool_spec],
        tool_choice={"type": "tool", "name": "submit_classification"},
    )


def _call_classifier(
    model: Any,
    system: str,
    user: str,
    *,
    model_name: str,
    accession_number: str | None,
) -> Classification:
    # The system prompt + taxonomy descriptions repeat verbatim across
    # every classification call. Marking the system block as ephemeral
    # tells Anthropic to cache it server-side; subsequent calls within
    # the cache window read it back at ~10x lower input-token cost. See
    # ADR 0022. Cache misses (first call, post-eviction) are billed
    # normally — the marker is safe to leave on regardless of hit rate.
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    # Record the call regardless of whether the response parses — even a
    # malformed tool-call cost us tokens and counts against the cap (ADR 0029).
    emit_llm_call(
        model=model_name,
        stage="classify",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract classification")
    args = tool_calls[0]["args"]
    return Classification.model_validate(args)


def _classify_node(state: _State) -> _State:
    """One LangGraph node that classifies every substantive Item.

    Keeping the graph single-node keeps the LangSmith trace easy to read.
    Concurrent per-item classification can replace this with parallel
    edges later if classifier latency dominates wall-clock time.
    """
    document = state["document"]
    model_name = state["model"]
    leaves = state["leaves"]
    model = _bind_classifier(model_name, leaves)
    system = _build_system_prompt(leaves)
    accession_number = document.filing.accession_number

    # Render the EX-99 exhibit context once and share it across every item's
    # prompt (and the whole-filing fallback). Empty string when no exhibits.
    exhibit_block = render_exhibits(document).block

    substantive_items = [
        item for item in document.items if item.number not in NON_SUBSTANTIVE_ITEMS
    ]

    items: list[ItemClassification] = []
    whole_filing: Classification | None = None

    if substantive_items:
        for item in substantive_items:
            user = _build_user_message(document, item, exhibit_block)
            classification = _call_classifier(
                model,
                system,
                user,
                model_name=model_name,
                accession_number=accession_number,
            )
            items.append(
                ItemClassification(
                    item_number=item.number,
                    item_title=item.title,
                    classification=classification,
                )
            )
    else:
        user = _build_user_message(document, None, exhibit_block)
        whole_filing = _call_classifier(
            model,
            system,
            user,
            model_name=model_name,
            accession_number=accession_number,
        )

    return {**state, "items": items, "whole_filing": whole_filing}


def _build_graph() -> Any:
    graph: StateGraph[_State, None, _State, _State] = StateGraph(_State)
    graph.add_node("classify", _classify_node)
    graph.add_edge(START, "classify")
    graph.add_edge("classify", END)
    return graph.compile()


def classifier_version(
    model_name: str = DEFAULT_MODEL, leaves: list[EventType] | None = None
) -> str:
    """Compose the classifier_version string for persistence.

    Combines the model name with a short hash of the system prompt. Any
    change to the prompt or the chosen model produces a new version string,
    which the persistence layer uses to keep classifications immutable and
    version-tagged. See ADR 0011. The `leaves` subset (if given) changes the
    prompt, so each A/B arm gets a distinct version automatically.
    """
    prompt = _build_system_prompt(leaves)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+prompt-{prompt_sha}"


def classify_filing(
    document: FilingDocument,
    model_name: str = DEFAULT_MODEL,
    leaves: list[EventType] | None = None,
) -> FilingClassification:
    """Classify every substantive Item in `document` via Claude tool-use.

    Returns a `FilingClassification` carrying per-Item results when items
    were extractable, or a single whole-filing classification when they
    were not. Tracing is automatic if LangSmith env vars are set.

    `leaves` restricts the offered choice-set to a subset of the taxonomy
    (default `None` = the full in-code taxonomy, the production behavior); used
    to classify a sample under a specific taxonomy version for evaluation.
    """
    graph = _build_graph()
    initial: _State = {
        "document": document,
        "model": model_name,
        "leaves": leaves,
        "items": [],
        "whole_filing": None,
    }
    result: _State = graph.invoke(initial)
    return FilingClassification(
        accession_number=document.filing.accession_number,
        cik=document.filing.cik,
        company_name=document.filing.company_name,
        filing_date=document.filing.filing_date.isoformat(),
        items=result["items"],
        whole_filing=result["whole_filing"],
        classified_at=datetime.now(UTC),
        model=model_name,
        classifier_version=classifier_version(model_name, leaves),
        taxonomy_version=TAXONOMY_VERSION,
    )
