"""Risk materialization judge — the Risk Radar's first "track update" (Phase 2b).

The radar detects a company-specific risk in a 10-K (change_specificity). This stage tracks
that risk forward: for each flagged specific risk, given the material 8-K/6-K events the
company filed AFTER the 10-K, judge whether any 8-K DIRECTLY realizes THIS risk — uplifting it
from declared (hypothetical) to materialized.

The bar is strict, and deliberately so: a realization must draw a direct line from a specific
8-K disclosure to the specific flagged risk, binary and evidenced. Speculation, a generic
quarterly earnings release, and a merely-related topic all resolve to NOT realized — that is
exactly where an offline validation's weak cases failed. Each verdict cites the 8-K disclosure
and states how it realizes the risk.

Reuses the judge's structured-output discipline (forced single tool call, temperature 0,
cached system prompt).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, NamedTuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.cost import emit_llm_call

DEFAULT_REALIZATION_MODEL = "claude-sonnet-4-6"


class RealizationEvent(NamedTuple):
    """One subsequent 8-K/6-K material event offered to the realization judge as a
    candidate for realizing the flagged risk."""

    filing_date: str
    event_type: str
    item: str
    summary: str


class RealizationVerdict(BaseModel):
    """The judge's verdict for one flagged risk. Matches the bound tool schema — field
    order and descriptions are what the model reads."""

    is_realized: bool = Field(
        description=(
            "True ONLY if a specific 8-K event DIRECTLY realizes this flagged risk (the "
            "hypothetical has materialized or is concretely advancing); false otherwise."
        )
    )
    event_index: int | None = Field(
        default=None,
        description=(
            "The 1-based index of the 8-K event that realizes the risk; null when not realized."
        ),
    )
    evidence: str = Field(
        default="",
        description=(
            "One sentence citing the specific 8-K disclosure and how it realizes THIS risk."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the realized/not-realized judgment, 0..1."
    )

    @field_validator("event_index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, (float, str)):
            try:
                return int(float(value))
            except ValueError:
                return None
        return None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> float:
        if isinstance(value, bool):
            return 0.5
        if isinstance(value, (int, float)):
            return min(1.0, max(0.0, float(value)))
        if isinstance(value, str):
            try:
                return min(1.0, max(0.0, float(value)))
            except ValueError:
                return 0.5
        return 0.5


_SYSTEM_PROMPT = (
    "You are given ONE company-specific risk from a company's 10-K — the RISK FACTOR text plus a "
    "one-line note on this year's CHANGE — and the material 8-K/6-K events the same company filed "
    "AFTER that 10-K. Decide whether any single event shows THIS risk MATERIALIZING.\n\n"
    "A risk materializes when the ADVERSE CONSEQUENCE it warns about actually befalls the company "
    "and a subsequent event discloses that consequence: a strike or shutdown for a labor risk; the "
    "key person actually departing for a retention risk; a cyber breach for a security risk; a "
    "downgrade, covenant breach, default, or forced asset sale for a debt risk; an announced "
    "impairment charge; a lawsuit filed against the company; the loss of a major customer; a "
    "regulator's enforcement action. The harm has LANDED, and the event says so. Set is_realized="
    "true and name that one event.\n\n"
    "Materialization is about CONSEQUENCES, not activity. Set is_realized=false when the company "
    "merely does more of the risky thing or manages the exposure without harm having occurred: "
    "taking on, issuing, or refinancing debt; amending a credit facility; a leadership transition "
    "where the executive REMAINS with the company or on its board (moving from an executive to a "
    "non-executive or chairman role is a role change, not a departure — only an actual exit, such "
    "as resigning, being terminated, or leaving for a competitor, realizes a key-person or "
    "retention risk); adjusting severance or retention terms; raising capital. These change the "
    "exposure; they are NOT the consequence the risk warned about. Also reject: a merely related "
    "topic; a peripheral facet the factor mentions in passing (a labor-costs risk is NOT realized "
    "by a CEO appointment); a generic quarterly earnings release; a big salient event (a merger, a "
    "large acquisition) matched to a loosely-related risk merely because it is important; "
    "speculation that results 'would' or 'could' reflect the risk; and any link that requires "
    "INFERENCE rather than being explicitly disclosed.\n\n"
    "Anchor on the CORE subject of the RISK FACTOR. Point to the one event whose disclosure states "
    "the consequence. When in doubt, choose false. In evidence, name the event and state in one "
    "sentence what adverse consequence it discloses and how that realizes THIS risk. Submit your "
    "verdict with the tool, exactly once."
)


def realization_version(model_name: str = DEFAULT_REALIZATION_MODEL) -> str:
    """A reproducibility tag = model + a hash of the system prompt (mirrors judge_version).
    Changing the model or prompt yields a new version, so verdicts re-derive rather than
    being silently reinterpreted."""
    prompt_sha = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+realization-{prompt_sha}"


def build_realization_judge(model_name: str = DEFAULT_REALIZATION_MODEL) -> Any:
    """A Claude model bound to the realization tool, forced to call it once."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_realization",
        "description": "Submit the risk-realization verdict. Call exactly once.",
        "input_schema": RealizationVerdict.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_realization"})


def _build_user_prompt(risk_text: str, risk: str, events: Sequence[RealizationEvent]) -> str:
    lines = [
        f"{i}. [{e.filing_date}] {e.event_type}"
        f"{(' (item ' + e.item + ')') if e.item else ''}: {e.summary}"
        for i, e in enumerate(events, start=1)
    ]
    factor = risk_text.strip() or "(text unavailable)"
    return (
        "FLAGGED RISK (declared in the 10-K):\n"
        + f"Risk factor: {factor}\nWhat changed this year: {risk}"
        + "\n\nSUBSEQUENT 8-K EVENTS:\n"
        + "\n".join(lines)
    )


def judge_realization(
    model: Any,
    *,
    risk_text: str,
    risk: str,
    events: Sequence[RealizationEvent],
    model_name: str,
    accession_number: str | None = None,
) -> RealizationVerdict:
    """Judge whether any subsequent 8-K realizes the flagged risk via the bound `model`.
    The risk factor's text anchors the judgment to the core risk (rather than a poorly-parsed
    heading). Records the call for cost accounting even if parsing fails."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(risk_text, risk, events)
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    emit_llm_call(
        model=model_name,
        stage="realization",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract realization verdict")
    return RealizationVerdict.model_validate(tool_calls[0]["args"])
