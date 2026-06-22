"""Reduce stage: collate a filing's per-Item classifications into events.

The classifier (map stage) assigns an event type to each Item in isolation.
This reduce stage takes those per-Item classifications — NOT the raw filing
text — and collates the Items that describe one real-world event into a single
filing-level event (ADR 0027). Working from the compact per-Item reasoning
rather than full text is the bounded-context win; the per-Item reasoning
already names entities and "incorporated by reference" pointers.

One Claude tool-use call per filing collates the Items; filings with fewer than
two substantive classifications are reduced without a model call (nothing to
merge). ADR 0028 covers how the resulting events are versioned and persisted as
a run — this module only produces the FilingEvents payload; it neither composes
with the classify graph nor persists (that is the wiring slice).
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from filings_orchestrator.classify.classifier import DEFAULT_MODEL
from filings_orchestrator.classify.schema import (
    FilingClassification,
    FilingEvents,
    ItemClassification,
    ReducedEvent,
    ReduceOutput,
)
from filings_orchestrator.classify.taxonomy import (
    EVENT_TYPE_DESCRIPTIONS,
    EventDomain,
    EventType,
    domain_for,
)
from filings_orchestrator.cost import emit_llm_call


def _reduce_prompt_body(form: str) -> list[str]:
    """The form-specific instructions of the reduce system prompt.

    8-K (the default) returns the original wording verbatim, so the 8-K
    `reducer_version` hash is unchanged. 6-K collates per-exhibit classifications
    instead of per-Item: each furnished exhibit is usually its own announcement
    and its own event, and only co-describing exhibits (a release plus its
    financial tables, or two-language pairs) merge. The shared event-types block
    is appended by the caller.
    """
    if form == "6-K":
        return [
            "You are an experienced securities analyst. You are given the per-exhibit "
            "classifications of a single SEC Form 6-K furnished by a foreign private "
            "issuer — for each furnished exhibit, its label, the assigned event type, "
            "whether it was judged material, and the 1-3 sentence reasoning behind it. "
            "You will NOT see the raw exhibit text; work from these classifications.",
            "",
            "Collate the exhibits into the distinct real-world events the report "
            "discloses. A single 6-K commonly furnishes several unrelated announcements, "
            "each its own exhibit and its own event; but sometimes several exhibits "
            "describe one event (a press release alongside its supporting financial "
            "tables, or the same announcement in two languages). Group every exhibit "
            "that describes the same event together, and keep genuinely distinct "
            "announcements separate.",
            "",
            "For each event:",
            "- Choose an anchor: the single primary exhibit the event centers on. "
            "Supporting tables, translations, and duplicate attachments are never anchors.",
            "- List every contributing exhibit, including the anchor.",
            "- Assign the event_type that best fits the consolidated event; it may "
            "differ from any single exhibit's type.",
            "- Write a 1-3 sentence summary of the consolidated event.",
            "- Set materiality, and a confidence reflecting genuine uncertainty.",
            "",
            "Merge conservatively: combine exhibits only when they clearly describe one "
            "event; keep genuinely distinct events separate. Every exhibit must belong "
            "to exactly one event.",
        ]
    return [
        "You are an experienced securities analyst. You are given the per-Item "
        "classifications of a single SEC Form 8-K filing — for each Item, its "
        "number, the assigned event type, whether it was judged material, and the "
        "1-3 sentence reasoning behind it. You will NOT see the raw filing text; "
        "work from these classifications.",
        "",
        "Collate the Items into the distinct real-world events the filing "
        "discloses. A single 8-K often describes one event across several Items: a "
        "substantive Item plus a Regulation FD (Item 7.01) furnishing of its press "
        "release, an exhibit (Item 9.01), or an Item that incorporates another by "
        "reference. Group every Item that describes the same event together.",
        "",
        "For each event:",
        "- Choose an anchor: the single primary substantive Item the event centers "
        "on (e.g. 5.02 for an officer appointment, 1.01 for a material agreement). "
        "Furnishings, exhibits, and incorporations by reference are never anchors.",
        "- List every contributing Item, including the anchor.",
        "- Assign the event_type that best fits the consolidated event; it may "
        "differ from any single Item's type. When one Item incorporates another by "
        "reference (e.g. Item 2.03 referring to Item 1.01), use the referenced "
        "Item's substance to determine the type.",
        "- Write a 1-3 sentence summary of the consolidated event.",
        "- Set materiality, and a confidence reflecting genuine uncertainty.",
        "",
        "Merge conservatively: combine Items only when they clearly describe one "
        "event; keep genuinely distinct events separate. Every Item must belong to "
        "exactly one event.",
    ]


def _build_reduce_system_prompt(form: str = "8-K") -> str:
    lines = [*_reduce_prompt_body(form), "", "Event types:"]
    lines.extend(
        f"- {event_type.value}: {EVENT_TYPE_DESCRIPTIONS[event_type]}" for event_type in EventType
    )
    return "\n".join(lines)


def _event_items(classification: FilingClassification) -> list[ItemClassification]:
    """The sections to collate into events: everything outside the `periodic` domain.

    6-K periodic financial reports (the `periodic` domain leaves, ADR 0034) are
    deferred — recorded but not events — so they are excluded from reduce here and
    from the reduce prompt, leaving the event sections to consolidate. Keying on the
    domain (not specific leaves) makes any future deferred document class drop out of
    the events layer automatically.
    """
    return [
        item
        for item in classification.items
        if domain_for(item.classification.event_type) != EventDomain.PERIODIC
    ]


def _build_reduce_user_message(classification: FilingClassification) -> str:
    # 6-K sections are furnished exhibits (item_number holds the exhibit label);
    # 8-K sections are Items. Label both the heading and each row accordingly.
    unit = "Exhibit" if classification.form == "6-K" else "Item"
    header = (
        f"Company: {classification.company_name} (CIK {classification.cik})\n"
        f"Filing date: {classification.filing_date}\n"
        f"Form: {classification.form}\n"
    )
    heading = "Per-exhibit classifications:" if unit == "Exhibit" else "Per-Item classifications:"
    lines = [header, heading]
    for item in _event_items(classification):
        c = item.classification
        title = f" ({item.item_title})" if item.item_title else ""
        materiality = "material" if c.is_material else "non-material"
        lines.append(
            f"\n{unit} {item.item_number}{title} — {c.event_type.value}, {materiality}, "
            f"confidence {c.confidence:.2f}\n  {c.reasoning}"
        )
    return "\n".join(lines)


def _bind_reducer(model_name: str) -> Any:
    """Build a Claude model bound to the submit_events tool, forced to call it."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_events",
        "description": (
            "Submit the consolidated filing-level events for this filing. "
            "Must be called exactly once per request."
        ),
        "input_schema": ReduceOutput.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_events"})


def _call_reducer(
    model: Any,
    system: str,
    user: str,
    *,
    model_name: str,
    accession_number: str,
) -> ReduceOutput:
    # The reduce system prompt + taxonomy repeats verbatim across calls; mark it
    # ephemeral so Anthropic caches it server-side (ADR 0022), as the classifier
    # does for its own system block.
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    # Record the call even on a malformed response — the tokens are spent regardless.
    emit_llm_call(
        model=model_name,
        stage="reduce",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract events")
    return ReduceOutput.model_validate(tool_calls[0]["args"])


def reducer_version(model_name: str = DEFAULT_MODEL, *, form: str = "8-K") -> str:
    """Compose the reducer_version string (model + reduce-prompt hash).

    Mirrors classifier_version: any change to the reduce prompt or the chosen
    model yields a new version string. It is recorded as run metadata, not row
    identity — every deliberate re-run is a new run regardless (ADR 0028).

    `form` selects the form-specific reduce prompt, so a 6-K reduce run carries a
    distinct version from an 8-K one. Keyword-only, defaulting to "8-K", so the
    existing 8-K version string is unchanged.
    """
    prompt_sha = hashlib.sha256(_build_reduce_system_prompt(form).encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+reduce-{prompt_sha}"


def reduce_filing(
    classification: FilingClassification,
    model_name: str = DEFAULT_MODEL,
) -> FilingEvents:
    """Collate a filing's per-Item classifications into filing-level events.

    Filings with fewer than two substantive Item classifications are reduced
    without a model call — there is nothing to merge. Otherwise one tool-use
    call collates the Items, and the result is grounded so anchor and
    contributing Item numbers refer only to Items that were actually classified.

    Only event-kind sections are collated; `periodic_report` sections are deferred
    (recorded, not events — ADR 0034), so a filing whose only sections are periodic
    reduces to zero events.
    """
    items = _event_items(classification)
    accession = classification.accession_number

    if not items:
        # Whole-filing fallback: the single classification is the event, if any.
        if classification.whole_filing is None:
            return FilingEvents(accession_number=accession, events=[])
        wf = classification.whole_filing
        return FilingEvents(
            accession_number=accession,
            events=[
                ReducedEvent(
                    event_type=wf.event_type,
                    is_material=wf.is_material,
                    confidence=wf.confidence,
                    summary=wf.reasoning,
                    anchor_item_number=None,
                    contributing_item_numbers=[],
                )
            ],
        )
    if len(items) == 1:
        only = items[0]
        c = only.classification
        return FilingEvents(
            accession_number=accession,
            events=[
                ReducedEvent(
                    event_type=c.event_type,
                    is_material=c.is_material,
                    confidence=c.confidence,
                    summary=c.reasoning,
                    anchor_item_number=only.item_number,
                    contributing_item_numbers=[only.item_number],
                )
            ],
        )

    system = _build_reduce_system_prompt(classification.form)
    user = _build_reduce_user_message(classification)
    output = _call_reducer(
        _bind_reducer(model_name),
        system,
        user,
        model_name=model_name,
        accession_number=accession,
    )

    valid_items = {item.item_number for item in items}
    return FilingEvents(
        accession_number=accession,
        events=_drop_subsumed_events(_ground_events(output.events, valid_items)),
    )


def _ground_events(events: list[ReducedEvent], valid_items: set[str]) -> list[ReducedEvent]:
    """Keep events anchored in Items that were actually classified.

    Drops contributing Item numbers the model invented; re-anchors on the first
    surviving contributing Item when the model's anchor is not a real Item, and
    drops an event that references no real Item at all. The raw classifications
    remain the safety net for anything the reduce stage mishandles (ADR 0028).
    """
    grounded: list[ReducedEvent] = []
    for event in events:
        contributing = [n for n in event.contributing_item_numbers if n in valid_items]
        anchor = (
            event.anchor_item_number
            if event.anchor_item_number in valid_items
            else (contributing[0] if contributing else None)
        )
        if anchor is None and not contributing:
            continue
        if anchor is not None and anchor not in contributing:
            contributing = [anchor, *contributing]
        grounded.append(
            event.model_copy(
                update={
                    "anchor_item_number": anchor,
                    "contributing_item_numbers": contributing,
                }
            )
        )
    return grounded


def _drop_subsumed_events(events: list[ReducedEvent]) -> list[ReducedEvent]:
    """Drop events whose contributing Items are wholly contained in another event.

    The reduce prompt requires every Item to belong to exactly one event, but the
    model sometimes also emits a smaller, separately-anchored event whose Items
    are a subset of a larger event's — e.g. a merger 8-K where the Certificate-of-
    Designation Item appears both inside the collated merger event and again as a
    standalone single-Item event. That smaller event is backed by the same
    classifications already subsumed by the larger one, so emitting it double-
    counts the same map output in downstream views and counts.

    An event is kept only when its contributing-Item set is maximal: not a proper
    subset of any other event's set. Exact-duplicate sets (identical contributing
    Items) are mutual subsets — keep the first occurrence, drop the rest. Events
    with partially overlapping but non-nested sets are left untouched: a shared
    Item that genuinely contributes to two distinct events is not over-emission.
    Order is otherwise preserved.
    """
    item_sets = [frozenset(e.contributing_item_numbers) for e in events]
    kept: list[ReducedEvent] = []
    for i, event in enumerate(events):
        subsumed = any(
            item_sets[i] < item_sets[j]  # proper subset of a larger event
            or (item_sets[i] == item_sets[j] and j < i)  # earlier-kept duplicate set
            for j in range(len(events))
            if j != i
        )
        if not subsumed:
            kept.append(event)
    return kept
