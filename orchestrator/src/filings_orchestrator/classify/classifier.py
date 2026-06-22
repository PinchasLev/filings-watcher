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
    EventDomain,
    EventType,
    domain_for,
)
from filings_orchestrator.cost import emit_llm_call
from filings_orchestrator.edgar.document import FilingDocument, ItemSection

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Cap per-section text to keep tokens (and cost) bounded even on outlier
# filings. 12,000 chars ≈ 3,000 tokens on prose; comfortably under any
# context budget while preserving the substantive disclosure in every 8-K
# we have observed in development. This caps an 8-K Item section, where the
# exhibit (the bulk of the prose) is supplemental context, not the section.
_MAX_SECTION_CHARS = 12_000

# A 6-K section IS a furnished exhibit — the primary content, not supplemental
# (ADR 0033) — so it gets a much larger budget than an 8-K Item. A 6-K exhibit
# is commonly a full results announcement or half-year report; the 12k Item cap
# would drop most of it. 50,000 chars ≈ 12,500 tokens captures the substantive
# disclosure of nearly every real exhibit while still bounding outlier
# annual-report-length attachments. Tunable; the per-section red-flag scan over
# any dropped tail remains a deferred follow-up (ADR 0033).
_MAX_6K_SECTION_CHARS = 50_000


class _State(TypedDict):
    document: FilingDocument
    model: str
    # The leaves (EventType members) the classifier may choose from. None means
    # the full in-code taxonomy — the production default. A subset is used to
    # evaluate a taxonomy change (e.g. classify-ab) by offering one version's
    # choice-set; the model is constrained to it via both the prompt and the
    # tool-schema enum. See ADR 0032.
    leaves: list[EventType] | None
    # Per-leaf descriptions (keyed by leaf value) to render in the prompt. None
    # uses the in-code `EVENT_TYPE_DESCRIPTIONS`. A prior version's snapshot
    # descriptions are passed here so the baseline arm reproduces that version's
    # *exact* prompt — faithful even when a description later changed. See ADR 0032.
    descriptions: dict[str, str] | None
    items: list[ItemClassification]
    whole_filing: Classification | None


def _prompt_lead_in(form: str) -> list[str]:
    """The form-specific opening of the classify system prompt.

    8-K (the default) returns the original two paragraphs verbatim, so the
    8-K `classifier_version` hash is byte-identical to before this became
    form-aware. 6-K substitutes a foreign-private-issuer framing: a 6-K has no
    standardized Item structure, so the unit shown is one furnished exhibit,
    and many 6-K furnishings are routine foreign-market compliance disclosures
    that are not material. The shared materiality/confidence guidance below is
    identical across forms.
    """
    if form == "6-K":
        return [
            "You are an experienced securities analyst classifying disclosures in an SEC "
            "Form 6-K — a report furnished by a foreign private issuer. Unlike a domestic "
            "8-K, a 6-K has no standardized Item structure; its substance is carried in the "
            "exhibits it furnishes (press releases, interim or half-year results, "
            "announcements, circulars). You will be shown one such exhibit and must classify "
            "what it discloses into the taxonomy below using the provided tool.",
            "",
            "Classify based on what the exhibit actually discloses. A single 6-K often "
            "bundles several unrelated announcements across separate exhibits; judge this "
            "exhibit on its own content. Many 6-K furnishings are routine foreign-market "
            "compliance disclosures — annual-meeting notices, monthly share-buyback returns, "
            "administrative circulars — that are not material; reserve is_material for "
            "disclosures that would affect a reasonable investor's assessment. Some exhibits "
            "are not discrete events but periodic financial reports (the foreign-issuer "
            "equivalent of a 10-Q or 10-K) — classify those with the matching `periodic_*` "
            "type so they are deferred, never a results press release, which is an event.",
        ]
    return [
        "You are an experienced securities analyst classifying SEC Form 8-K material event "
        "disclosures. You will be shown one section of an 8-K filing — typically a single "
        "Item — and must classify it into the taxonomy below using the provided tool.",
        "",
        "Classify based on what the prose actually discloses, not on the Item number alone. "
        "An Item 5.02 filing may be a departure, an appointment, or both — choose the most "
        "salient event the prose centers on.",
    ]


def _build_system_prompt(
    form: str = "8-K",
    leaves: list[EventType] | None = None,
    descriptions: dict[str, str] | None = None,
) -> str:
    # `leaves is None` enumerates the full in-code taxonomy in declaration order
    # — byte-identical to the production prompt, so the default `classifier_version`
    # is unchanged. A subset offers only that version's choice-set. `descriptions`
    # (None = in-code) lets a baseline arm render a prior version's exact text.
    event_types = leaves if leaves is not None else list(EventType)
    lines = [
        *_prompt_lead_in(form),
        "",
        "Event types:",
    ]
    for event_type in event_types:
        desc = (
            descriptions[event_type.value]
            if descriptions is not None
            else EVENT_TYPE_DESCRIPTIONS[event_type]
        )
        lines.append(f"- {event_type.value}: {desc}")
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
    # 6-K sections are the furnished exhibits, so label them "Exhibit"; 8-K (and
    # any Item-bearing form) labels them "Item". The unit key (item.number) holds
    # the Item number for 8-K and the exhibit label (e.g. "EX-99.1") for 6-K.
    # A 6-K exhibit is the primary content, so it gets the larger section budget.
    unit = "Exhibit" if filing.form == "6-K" else "Item"
    cap = _MAX_6K_SECTION_CHARS if filing.form == "6-K" else _MAX_SECTION_CHARS
    if item is not None:
        body = item.text[:cap]
        section_header = f"{unit} {item.number}"
        if item.title:
            section_header += f": {item.title}"
        return f"{header}\nSection under classification: {section_header}\n\n{body}{suffix}"
    body = document.text[:cap]
    no_sections = (
        "No exhibits were furnished. Classify the body of the report:"
        if filing.form == "6-K"
        else "No Item sections were extractable. Classify the whole filing body:"
    )
    return f"{header}\n{no_sections}\n\n{body}{suffix}"


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


def _exhibit_sections(document: FilingDocument) -> list[ItemSection]:
    """Map a 6-K's furnished EX-99 exhibits to classification sections.

    For a 6-K the substance lives in the exhibits, not a cover Item structure, so
    each exhibit becomes one section the classifier labels independently — the
    direct analogue of an 8-K Item. The section key reuses `ItemClassification`'s
    `item_number` slot and carries the exhibit label (e.g. "EX-99.1"); the rare
    case of two exhibits sharing a type is disambiguated with a "#n" suffix so the
    `(accession, item_number, classifier_version)` key stays unique.
    """
    sections: list[ItemSection] = []
    seen: dict[str, int] = {}
    for exhibit in document.exhibits:
        key = exhibit.exhibit_type
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        if occurrence:
            key = f"{key}#{occurrence + 1}"
        sections.append(ItemSection(number=key, title=exhibit.document, text=exhibit.text))
    return sections


def _sections_for(document: FilingDocument) -> tuple[list[ItemSection], str]:
    """Pick the classification sections and shared context block, by form.

    - 6-K: the furnished EX-99 exhibits are the sections themselves; there is no
      separate context block (the exhibits are the body, not supplemental).
    - 8-K (and any Item-bearing form): the substantive Items are the sections, and
      the EX-99 exhibits are rendered once as shared supporting context.

    Either way an empty section list routes the caller to the whole-filing
    fallback — a 6-K with no exhibits classifies its cover body, an 8-K with no
    extractable Items classifies its whole body with the exhibit context.

    See ADR 0033.
    """
    if document.filing.form == "6-K":
        return _exhibit_sections(document), ""
    substantive_items = [
        item for item in document.items if item.number not in NON_SUBSTANTIVE_ITEMS
    ]
    return substantive_items, render_exhibits(document).block


def _classify_node(state: _State) -> _State:
    """One LangGraph node that classifies every section of a filing.

    A "section" is an 8-K substantive Item or a 6-K furnished exhibit (see
    `_sections_for`). Keeping the graph single-node keeps the LangSmith trace easy
    to read. Concurrent per-section classification can replace this with parallel
    edges later if classifier latency dominates wall-clock time.
    """
    document = state["document"]
    model_name = state["model"]
    leaves = state["leaves"]
    descriptions = state["descriptions"]
    model = _bind_classifier(model_name, leaves)
    system = _build_system_prompt(document.filing.form, leaves, descriptions)
    accession_number = document.filing.accession_number

    sections, context_block = _sections_for(document)

    items: list[ItemClassification] = []
    whole_filing: Classification | None = None

    if sections:
        for section in sections:
            user = _build_user_message(document, section, context_block)
            classification = _call_classifier(
                model,
                system,
                user,
                model_name=model_name,
                accession_number=accession_number,
            )
            items.append(
                ItemClassification(
                    item_number=section.number,
                    item_title=section.title,
                    classification=classification,
                )
            )
    else:
        user = _build_user_message(document, None, context_block)
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
    model_name: str = DEFAULT_MODEL,
    leaves: list[EventType] | None = None,
    descriptions: dict[str, str] | None = None,
    *,
    form: str = "8-K",
) -> str:
    """Compose the classifier_version string for persistence.

    Combines the model name with a short hash of the system prompt. Any
    change to the prompt or the chosen model produces a new version string,
    which the persistence layer uses to keep classifications immutable and
    version-tagged. See ADR 0011. The `leaves` subset and `descriptions` (if
    given) change the prompt, so each A/B arm — including a faithful prior-version
    baseline reconstructed from its snapshot — gets the matching version string.

    `form` selects the form-specific prompt lead-in; a 6-K therefore carries a
    distinct version from an 8-K classified by the same model. It is keyword-only
    and defaults to "8-K" so the existing 8-K version string is unchanged.
    """
    prompt = _build_system_prompt(form, leaves, descriptions)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+prompt-{prompt_sha}"


def _default_leaves(form: str) -> list[EventType]:
    """The choice-set offered when the caller passes no explicit `leaves` (the live path).

    The `periodic` document-class leaves are offered ONLY to 6-K. For every other form
    they are excluded; because they are declared last in `EventType`, the excluded set
    is byte-identical to the pre-v1.3 taxonomy in the same order — so 8-K's enumerated
    prompt, and thus its `classifier_version`, are unchanged (ADR 0034). A/B callers that
    pass an explicit `leaves` bypass this entirely.
    """
    if form == "6-K":
        return list(EventType)
    return [event for event in EventType if domain_for(event) != EventDomain.PERIODIC]


def classify_filing(
    document: FilingDocument,
    model_name: str = DEFAULT_MODEL,
    leaves: list[EventType] | None = None,
    descriptions: dict[str, str] | None = None,
) -> FilingClassification:
    """Classify every substantive Item in `document` via Claude tool-use.

    Returns a `FilingClassification` carrying per-Item results when items
    were extractable, or a single whole-filing classification when they
    were not. Tracing is automatic if LangSmith env vars are set.

    `leaves` restricts the offered choice-set to a subset of the taxonomy and
    `descriptions` overrides the per-leaf prompt text (default `None`/`None` = the
    full in-code taxonomy, the production behavior). Together they reproduce a
    specific taxonomy version — including a prior version's *exact* descriptions
    from its snapshot — for evaluation.
    """
    # leaves=None means "the live default for this form": periodic leaves offered to
    # 6-K, withheld from 8-K (ADR 0034). An explicit `leaves` (A/B evaluation) is honored
    # as-is. The same effective set drives the prompt, the tool-schema enum, and the
    # recorded classifier_version, so all three stay consistent.
    effective_leaves = leaves if leaves is not None else _default_leaves(document.filing.form)
    graph = _build_graph()
    initial: _State = {
        "document": document,
        "model": model_name,
        "leaves": effective_leaves,
        "descriptions": descriptions,
        "items": [],
        "whole_filing": None,
    }
    result: _State = graph.invoke(initial)
    return FilingClassification(
        accession_number=document.filing.accession_number,
        cik=document.filing.cik,
        company_name=document.filing.company_name,
        filing_date=document.filing.filing_date.isoformat(),
        form=document.filing.form,
        items=result["items"],
        whole_filing=result["whole_filing"],
        classified_at=datetime.now(UTC),
        model=model_name,
        classifier_version=classifier_version(
            model_name, effective_leaves, descriptions, form=document.filing.form
        ),
        taxonomy_version=TAXONOMY_VERSION,
    )
