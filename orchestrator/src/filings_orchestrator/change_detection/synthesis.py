"""Per-filing synthesis of disclosure changes (ADR 0043, PR 2).

The judge (PR 5 of ADR 0042) produces one verdict per change. A filing with forty
material changes is still forty rows — evidence, not an answer. This stage turns
that evidence into a *read*:

- **Headline** — a categorical direction (deteriorating / mixed / stable / improving),
  judged by the reduce itself as a holistic, severity-aware read of all the changes at
  once. A single severe shift (a new going-concern) can outweigh many trivial
  rewordings, and several marginal worsenings can still net out to stable — a *count*
  cannot see that, because materiality was captured as a boolean and the magnitude was
  discarded. The reduce is the only actor that reads every finding with its meaning at
  once, so it is the only one positioned to weigh severity. Never a numeric score.
- **Synthesis** — a short thesis paragraph and a top-effects list, produced by the same
  **LLM reduce over the distilled verdicts** (each change's theme, direction, and
  one-line explanation — never the raw block prose), so the reduce stays a single
  bounded call regardless of how many changes there are. This composes with the same
  map-reduce discipline as classification: map = per-change judge, reduce = compose.

Numbers are code's job; judgment is the model's. Code contributes only the *counts*
(worse / eased / material), stored beside the label as an independent factual
cross-check; the reduce owns the direction and the prose. The headline is a judgment,
not an arithmetic roll-up, so the bounded-operator boundary holds (ADR 0043, amended).

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
    """The filing-level risk-shift verdict shown at the top of the read — a holistic,
    severity-aware judgment made by the reduce, not a count-based roll-up."""

    DETERIORATING = "deteriorating"
    IMPROVING = "improving"
    MIXED = "mixed"
    STABLE = "stable"


def coerce_headline(value: object) -> HeadlineDirection:
    """Map a raw model value to a headline direction, degrading an unrecognized value
    to MIXED — the least-misleading fallback (STABLE could falsely reassure)."""
    if isinstance(value, HeadlineDirection):
        return value
    if isinstance(value, str):
        try:
            return HeadlineDirection(value.strip().lower())
        except ValueError:
            return HeadlineDirection.MIXED
    return HeadlineDirection.MIXED


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
            "The overall direction of this filing's material risk changes, weighing "
            "them by how much they matter (NOT by counting): a single severe shift can "
            "outweigh many minor ones, and several marginal worsenings can still net to "
            "stable. One of: deteriorating, improving, mixed, stable."
        )
    )
    thesis: str = Field(
        description=(
            "2-4 sentences, consistent with the headline direction: what materially "
            "changed in this company's risk disclosures versus the prior year and why it "
            "matters to a risk-monitoring reader. Lead with the most significant shift. "
            "Concrete and specific; no preamble, no hedging, no restating the instructions."
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
    def _coerce_headline(cls, value: object) -> HeadlineDirection:
        return coerce_headline(value)


_SYSTEM_PROMPT = (
    "You are given the material year-over-year changes a company made to its 10-K Risk "
    "Factors, each tagged with a theme, a direction (worse / eased / neutral), and a "
    "one-line explanation of what shifted. Synthesize them for a risk-monitoring reader "
    "(credit, procurement, insurance, or counterparty-risk) who wants the picture at a "
    "glance.\n\n"
    "First judge the overall HEADLINE DIRECTION, weighing the changes by how much they "
    "matter — a single severe shift (e.g. a new going-concern or covenant breach) can "
    "outweigh many minor rewordings, and several marginal worsenings can still net out "
    "to stable. Choose one:\n"
    "- deteriorating: the material risks got meaningfully worse on balance.\n"
    "- improving: the material risks meaningfully eased on balance. Be skeptical here — "
    "a removed or softened risk factor often reflects reorganization or boilerplate "
    "cleanup rather than a genuinely resolved risk.\n"
    "- mixed: meaningful worsening AND meaningful easing are both present.\n"
    "- stable: only minor or cosmetic changes; nothing that moves the risk picture.\n\n"
    "Then write the thesis and the top effects. Compose only from the changes provided — "
    "do not introduce risks that are not among them, and do not restate every change. "
    "Write plainly and specifically. Submit your synthesis with the tool, exactly once."
)


def synthesis_version(model_name: str = DEFAULT_SYNTHESIS_MODEL) -> str:
    """A reproducibility tag = model + a hash of the reduce prompt. Changing the prompt
    (which now carries the headline-direction rubric) or the model yields a new version,
    so syntheses re-derive rather than being silently reinterpreted (mirrors
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
    """Reduce a filing's distilled material findings into a headline direction, thesis,
    and top-effects via the bound `model`. Records the call for cost accounting even if
    parsing fails."""
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
