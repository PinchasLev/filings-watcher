"""Per-filing synthesis of disclosure changes (ADR 0043, PR 2 + 2b).

The judge (PR 5 of ADR 0042) produces one verdict per change. A filing with forty
material changes is still forty rows — evidence, not an answer. This stage turns
that evidence into a *read*:

- **Headline** — two coarse, categorical judgments the reduce makes over all the
  changes at once: a **direction** (worsening / easing / mixed) and an **intensity**
  (major / moderate / minor). These are separate on purpose: *which way* the risk
  moved and *how much* the overall picture moved are independent facts, and a single
  label cannot carry both (a going-concern collapse and three modest new risks are
  both "worse", yet worlds apart in size). Neither is a numeric score. Code
  contributes only the *counts* (worse / eased / material), shown beside the headline
  as an independent factual cross-check.
- **Synthesis** — a short thesis paragraph and a top-effects list, from the same
  **LLM reduce over the distilled verdicts** (each change's theme, direction, and
  one-line explanation — never the raw block prose), so the reduce stays a single
  bounded call regardless of how many changes there are. This composes with the same
  map-reduce discipline as classification: map = per-change judge, reduce = compose.

Numbers are code's job; judgment is the model's. Code owns the counts; the reduce
owns the direction, intensity, and prose. The headline is a judgment, not an
arithmetic roll-up, so the bounded-operator boundary holds (ADR 0043, amended).

Reuses the judge's structured-output discipline (forced single tool call,
temperature 0, cached system prompt). The synthesis is stored and versioned so the
company page and any later feed read the same text (persistence + reconciler live in
their own modules).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, NamedTuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.change_detection.taxonomy import (
    RiskChangeCategory,
    RiskChangeDirection,
)
from filings_orchestrator.cost import emit_llm_call

DEFAULT_SYNTHESIS_MODEL = "claude-haiku-4-5-20251001"


class HeadlineDirection(StrEnum):
    """Which way the filing's material risks moved, on balance — a holistic judgment
    by the reduce. Magnitude lives in HeadlineIntensity, not here."""

    WORSENING = "worsening"
    EASING = "easing"
    MIXED = "mixed"


class HeadlineIntensity(StrEnum):
    """How much the overall risk picture moved — the magnitude axis, judged by the
    reduce weighing severity, not by counting changes."""

    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"


def coerce_direction_headline(value: object) -> HeadlineDirection:
    """Map a raw model value to a headline direction, degrading an unrecognized value
    to MIXED — the least-misleading fallback."""
    if isinstance(value, HeadlineDirection):
        return value
    if isinstance(value, str):
        try:
            return HeadlineDirection(value.strip().lower())
        except ValueError:
            return HeadlineDirection.MIXED
    return HeadlineDirection.MIXED


def coerce_intensity(value: object) -> HeadlineIntensity:
    """Map a raw model value to an intensity, degrading an unrecognized value to
    MODERATE — the neutral middle, so garbage neither over- nor under-claims."""
    if isinstance(value, HeadlineIntensity):
        return value
    if isinstance(value, str):
        try:
            return HeadlineIntensity(value.strip().lower())
        except ValueError:
            return HeadlineIntensity.MODERATE
    return HeadlineIntensity.MODERATE


class Finding(NamedTuple):
    """One distilled material change fed to the reduce — theme, direction, and the
    judge's one-line explanation. Deliberately not the raw block text: the reduce
    composes summaries, so it stays bounded no matter how many changes there are."""

    category: RiskChangeCategory
    direction: RiskChangeDirection
    explanation: str


class DisclosureSynthesis(BaseModel):
    """The reduce's structured output. Matches the bound tool schema — field order
    and descriptions are what the model reads."""

    headline_direction: HeadlineDirection = Field(
        description=(
            "Which way the material risks moved on balance: 'worsening', 'easing', or "
            "'mixed' (meaningful worsening AND meaningful easing both present). Direction "
            "only — say nothing about size here."
        )
    )
    headline_intensity: HeadlineIntensity = Field(
        description=(
            "How much the OVERALL risk picture moved, weighing changes by severity, not "
            "counting them: 'major' = a severe single change (e.g. new going-concern, "
            "delisting, or covenant breach) OR broad worsening across many themes; "
            "'moderate' = a real but contained shift; 'minor' = a few localized changes, "
            "nothing severe and no broad pattern (the picture barely moved)."
        )
    )
    thesis: str = Field(
        description=(
            "2-4 sentences, consistent with the headline: what materially changed in "
            "this company's risk disclosures versus the prior year and why it matters to "
            "a risk-monitoring reader. Lead with the most significant shift. Concrete and "
            "specific; no preamble, no hedging, no restating the instructions."
        )
    )
    top_effects: list[str] = Field(
        description=(
            "The 3-5 most important individual effects, each a short phrase (<= 15 "
            "words). Order by importance. Draw only from the changes provided; do not "
            "invent effects not present in them."
        )
    )

    @field_validator("headline_direction", mode="before")
    @classmethod
    def _coerce_direction(cls, value: object) -> HeadlineDirection:
        return coerce_direction_headline(value)

    @field_validator("headline_intensity", mode="before")
    @classmethod
    def _coerce_intensity(cls, value: object) -> HeadlineIntensity:
        return coerce_intensity(value)


_SYSTEM_PROMPT = (
    "You are given the material year-over-year changes a company made to its 10-K Risk "
    "Factors, each tagged with a theme, a direction (worse / eased / neutral), and a "
    "one-line explanation of what shifted. Synthesize them for a risk-monitoring reader "
    "(credit, procurement, insurance, or counterparty-risk) who wants the picture at a "
    "glance.\n\n"
    "Judge two separate things about the filing as a whole:\n\n"
    "DIRECTION — which way the material risks moved on balance:\n"
    "- worsening: they got worse on balance.\n"
    "- easing: they meaningfully eased on balance. Be skeptical here — a removed or "
    "softened risk factor often reflects reorganization or boilerplate cleanup rather "
    "than a genuinely resolved risk.\n"
    "- mixed: meaningful worsening AND meaningful easing are both present.\n\n"
    "INTENSITY — how much the OVERALL risk picture moved, weighing changes by how much "
    "they matter, NOT by counting them:\n"
    "- major: a severe single change (e.g. a new going-concern, delisting, or covenant "
    "breach) OR broad worsening across many themes.\n"
    "- moderate: a real but contained shift.\n"
    "- minor: a few localized changes, nothing severe and no broad pattern — the overall "
    "picture barely moved. Several individually-material but modest changes can still be "
    "minor in aggregate.\n\n"
    "Then write the thesis and the top effects. Compose only from the changes provided — "
    "do not introduce risks that are not among them, and do not restate every change. "
    "Write plainly and specifically. Submit your synthesis with the tool, exactly once."
)


def synthesis_version(model_name: str = DEFAULT_SYNTHESIS_MODEL) -> str:
    """A reproducibility tag = model + a hash of the reduce prompt. Changing the prompt
    (which carries the direction and intensity rubrics) or the model yields a new
    version, so syntheses re-derive rather than being silently reinterpreted (mirrors
    judge_version / classifier_version)."""
    prompt_sha = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+synthesis-{prompt_sha}"


def build_synthesizer(model_name: str = DEFAULT_SYNTHESIS_MODEL) -> Any:
    """A Claude model bound to the synthesis tool, forced to call it once."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_synthesis",
        "description": "Submit the disclosure-change synthesis. Call exactly once.",
        "input_schema": DisclosureSynthesis.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_synthesis"})


def _build_user_prompt(findings: list[Finding]) -> str:
    lines = [f"- [{f.category.value} | {f.direction.value}] {f.explanation}" for f in findings]
    return "The material risk-factor changes this year:\n" + "\n".join(lines)


def synthesize(
    model: Any,
    *,
    findings: list[Finding],
    model_name: str,
    accession_number: str | None = None,
) -> DisclosureSynthesis:
    """Reduce a filing's distilled material findings into a headline (direction +
    intensity), thesis, and top-effects via the bound `model`. Records the call for
    cost accounting even if parsing fails."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(findings)
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    emit_llm_call(
        model=model_name,
        stage="synthesis",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract synthesis")
    return DisclosureSynthesis.model_validate(tool_calls[0]["args"])
