"""LLM materiality judge for risk-factor changes (ADR 0042, PR 5).

The diff (PR 4) is a good candidate-generator but a poor materiality judge: its
shortlist of changed/added/dropped blocks is dominated by real but immaterial
change (reworded boilerplate, new ESG language). This stage reads each shortlisted
change and asks Claude whether the *change* is material — judging the delta, not the
passage: a changed block is shown with BOTH its new and prior text so the model
reasons about what actually shifted, not just whether the text sounds important.

Reuses the classifier's structured-output discipline (forced single tool call,
temperature 0, cached system prompt) so the model returns a validated verdict, never
free prose. Code, not the model, decides the control flow downstream: a low-
confidence verdict is flagged for review rather than trusted.
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.change_detection.taxonomy import (
    RiskChangeCategory,
    RiskChangeDirection,
    coerce_category,
    coerce_direction,
    render_category_guidance,
    render_direction_guidance,
)
from filings_orchestrator.cost import emit_llm_call

DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"

CHANGE_ADDED = "added"
CHANGE_CHANGED = "changed"
CHANGE_DROPPED = "dropped"


class MaterialityVerdict(BaseModel):
    """The judge's structured output for one change. Matches the bound tool schema —
    field order and descriptions are what the model reads."""

    is_material: bool = Field(
        description=(
            "True if this change to the risk factors is material — a substantive new "
            "or worsened business, financial, or legal risk a reasonable investor or "
            "credit analyst would act on. False for boilerplate or cosmetic change."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in the is_material judgment, 0..1. Lower when the change is "
            "ambiguous or you are unsure whether it is substantive or cosmetic."
        ),
    )
    category: RiskChangeCategory = Field(
        description=(
            "The single theme this change belongs to, chosen from the governed set. "
            "Use 'other' only when no specific theme fits."
        )
    )
    direction: RiskChangeDirection = Field(
        description=(
            "Which way the change moved the risk: 'worse' (increased / newly disclosed "
            "/ escalated), 'eased' (decreased / resolved / removed), or 'neutral' (no "
            "real change in risk level — rewording or reorganization). Judge by the "
            "change in risk, not by whether the block was added, revised, or dropped."
        )
    )
    explanation: str = Field(
        description="At most 25 words: why the change is or is not material, citing what shifted."
    )

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: object) -> RiskChangeCategory:
        return coerce_category(value)

    @field_validator("direction", mode="before")
    @classmethod
    def _coerce_direction(cls, value: object) -> RiskChangeDirection:
        return coerce_direction(value)


_SYSTEM_PROMPT_BASE = (
    "You compare a company's latest 10-K Risk Factors to the prior year's and judge "
    "whether a specific change is MATERIAL: a substantive new or worsened business, "
    "financial, or legal risk a reasonable investor or credit analyst would act on — "
    "for example going-concern or liquidity doubt, new material litigation, "
    "restructuring or layoffs, loss of a major customer or revenue, debt or covenant "
    "problems, a guidance cut, regulatory action, or an impairment. Treat as NOT "
    "material: generic boilerplate, ESG/DEI/sustainability language, definitional or "
    "forward-looking-statement text, and cosmetic rewording that does not change the "
    "substance. Judge the CHANGE, not the passage — an unremarkable risk factor that "
    "was merely reworded is not material even if it reads as serious. Bias toward "
    "is_material=false unless the change clearly matters, and lower your confidence "
    "when the change is ambiguous.\n\n"
    "Also tag each change with its THEME (category) and its DIRECTION.\n\n"
    "Themes — pick the single best fit:\n"
    f"{render_category_guidance()}\n\n"
    "Direction — how the change moved the risk:\n"
    f"{render_direction_guidance()}\n\n"
    "Submit your judgment with the tool, exactly once."
)

# Kept as the hashed prompt so judge_version tracks any change to the base wording OR
# the governed vocabulary (both are folded into _SYSTEM_PROMPT_BASE above): a vocab
# change yields a new judge_version, so verdicts re-judge rather than reinterpret.
_SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE


def judge_version(model_name: str = DEFAULT_JUDGE_MODEL) -> str:
    """A reproducibility tag = model + a hash of the system prompt. Changing the model
    or the prompt yields a new version, so verdicts are append-only and re-judged
    rather than silently overwritten (mirrors the classifier)."""
    prompt_sha = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+materiality-{prompt_sha}"


def build_judge(model_name: str = DEFAULT_JUDGE_MODEL) -> Any:
    """A Claude model bound to the materiality tool, forced to call it once."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_materiality",
        "description": "Submit the materiality judgment for the change. Call exactly once.",
        "input_schema": MaterialityVerdict.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_materiality"})


def _build_user_prompt(change_type: str, current_text: str | None, prior_text: str | None) -> str:
    if change_type == CHANGE_CHANGED:
        return (
            "A risk factor was revised year over year. Judge whether the CHANGE is "
            "material.\n\nPRIOR YEAR:\n"
            f"{prior_text or ''}\n\nTHIS YEAR:\n{current_text or ''}"
        )
    if change_type == CHANGE_ADDED:
        return (
            "This risk factor is NEW this year (no prior-year counterpart). Judge "
            "whether its addition is material.\n\nNEWLY ADDED:\n"
            f"{current_text or ''}"
        )
    if change_type == CHANGE_DROPPED:
        return (
            "This risk factor was present last year and is GONE this year. Judge "
            "whether its removal is material.\n\nREMOVED (prior year):\n"
            f"{prior_text or ''}"
        )
    raise ValueError(f"unknown change_type: {change_type!r}")


def judge_change(
    model: Any,
    *,
    change_type: str,
    current_text: str | None,
    prior_text: str | None,
    model_name: str,
    accession_number: str | None = None,
) -> MaterialityVerdict:
    """Judge one change via the bound `model`. Records the call for cost accounting
    even if the response fails to parse (it still cost tokens)."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(change_type, current_text, prior_text)
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    emit_llm_call(
        model=model_name,
        stage="materiality",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract materiality verdict")
    return MaterialityVerdict.model_validate(tool_calls[0]["args"])
