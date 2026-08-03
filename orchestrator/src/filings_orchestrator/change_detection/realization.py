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

DEFAULT_REALIZATION_MODEL = "claude-haiku-4-5-20251001"


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
    "You are given ONE company-specific risk a company flagged in its 10-K, and the material "
    "8-K events the same company filed AFTER that 10-K. Decide whether any of those 8-K events "
    "DIRECTLY realizes THIS specific risk - i.e. the hypothetical risk has now materialized or is "
    "concretely advancing, and a particular 8-K disclosure draws a direct line to it.\n\n"
    "Be STRICT. Set is_realized=true ONLY when a specific 8-K discloses a concrete fact that "
    "plainly realizes THIS risk, and name that event in event_index. Do NOT count: a merely "
    "related topic, a generic quarterly earnings release, or speculation that results 'would' or "
    "'could' reflect the risk. If nothing draws a direct line, set is_realized=false and "
    "event_index=null.\n\n"
    "In evidence, cite the specific 8-K disclosure and state in one sentence how it realizes THIS "
    "risk. Submit your verdict with the tool, exactly once."
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


def _build_user_prompt(risk: str, events: Sequence[RealizationEvent]) -> str:
    lines = [
        f"{i}. [{e.filing_date}] {e.event_type}"
        f"{(' (item ' + e.item + ')') if e.item else ''}: {e.summary}"
        for i, e in enumerate(events, start=1)
    ]
    return (
        "FLAGGED RISK (declared in the 10-K):\n"
        + risk
        + "\n\nSUBSEQUENT 8-K EVENTS:\n"
        + "\n".join(lines)
    )


def judge_realization(
    model: Any,
    *,
    risk: str,
    events: Sequence[RealizationEvent],
    model_name: str,
    accession_number: str | None = None,
) -> RealizationVerdict:
    """Judge whether any subsequent 8-K realizes the flagged risk via the bound `model`.
    Records the call for cost accounting even if parsing fails."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(risk, events)
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
