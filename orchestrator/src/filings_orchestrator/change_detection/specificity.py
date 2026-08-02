"""Specific-vs-boilerplate classifier for material risk-factor changes (calibration;
extends ADR 0042/0043, consumes the catalog of migration 023).

The judge decides whether a change is material; this stage decides, for each material
change, whether it is a COMPANY-SPECIFIC development (a real idiosyncratic event/fact about
this company) or an instance of a COMMON-MODE catalog theme (macro boilerplate the whole
cohort discloses). is_specific is the filter a later stage uses to surface only the
company-specific changes and discount the boilerplate.

The boilerplate-vs-signal line is semantic and needs the corpus vantage, which the catalog
supplies, so this is a per-company classification given the catalog as reference. The catalog
rides in the CACHED system prompt so it is paid once and reused across every filing in a run.
Each verdict carries the change's index so results map back to change_seq robustly.

Specificity is a FILTER, not a rank — the classifier makes no importance/severity judgment
(that is impact = B, a later phase). Reuses the judge's structured-output discipline (forced
single tool call, temperature 0, cached system prompt).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, NamedTuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.change_detection.catalog import _slugify
from filings_orchestrator.cost import emit_llm_call

DEFAULT_SPECIFICITY_MODEL = "claude-haiku-4-5-20251001"


class SpecificityInput(NamedTuple):
    """One material change fed to the classifier — its theme (category) and the judge's
    one-line explanation. Deliberately not the raw block text: the classifier judges from
    the distilled change, keeping the per-filing call bounded."""

    category: str
    explanation: str


class ChangeSpecificity(BaseModel):
    """The classifier's verdict for one change. Matches the bound tool schema — field
    order and descriptions are what the model reads."""

    change_index: int = Field(
        description="The 1-based index of the change this verdict is for, exactly as numbered."
    )
    is_specific: bool = Field(
        description=(
            "True if this change is a COMPANY-SPECIFIC development (idiosyncratic to this "
            "company); false if it is a COMMON-MODE catalog theme (generic macro boilerplate)."
        )
    )
    matched_theme: str | None = Field(
        default=None,
        description=(
            "When is_specific is false, the single best-matching catalog theme slug; when "
            "true, leave empty."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the specific-vs-boilerplate call, 0..1.",
    )
    explanation: str = Field(
        default="",
        description="At most 20 words: why it is specific, or which common-mode theme it echoes.",
    )

    @field_validator("change_index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> int:
        if isinstance(value, bool):
            return 0  # out of range -> dropped by sanitize_batch
        if isinstance(value, int):
            return value
        if isinstance(value, (float, str)):
            try:
                return int(float(value))
            except ValueError:
                return 0
        return 0

    @field_validator("matched_theme", mode="before")
    @classmethod
    def _coerce_theme(cls, value: object) -> str | None:
        if value is None:
            return None
        slug = _slugify(str(value))
        return slug or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> float:
        try:
            return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5


class SpecificityBatch(BaseModel):
    """The classifier's structured output — one verdict per change, in any order."""

    verdicts: list[ChangeSpecificity] = Field(
        description="One verdict per change, each carrying its 1-based change_index."
    )

    @field_validator("verdicts", mode="before")
    @classmethod
    def _coerce_verdicts(cls, value: object) -> list[Any]:
        return value if isinstance(value, list) else []


_SYSTEM_PROMPT_BASE = (
    "You are given a CATALOG of common-mode boilerplate themes — the macro risks many "
    "companies disclose in common (tariffs, generative-AI, rate/refinancing, data-privacy) — "
    "and a list of one company's MATERIAL year-over-year risk-factor changes. For EACH change "
    "decide one thing: is it a COMPANY-SPECIFIC development — a real, idiosyncratic event or "
    "fact about THIS company (a named merger or acquisition, a specific customer loss, a "
    "particular impairment, lawsuit, restructuring, guidance cut, or covenant issue) — or is "
    "it merely this company's instance of a COMMON-MODE catalog theme (generic macro "
    "boilerplate the whole cohort writes)?\n\n"
    "Set is_specific=true for the idiosyncratic, company-specific changes; false for "
    "common-mode boilerplate. When false, name the single best-matching catalog theme slug in "
    "matched_theme; when true, leave matched_theme empty. A change can be serious-sounding yet "
    "still common-mode (for example a generic paragraph on tariff exposure): judge whether it "
    "is SPECIFIC TO THIS COMPANY, not whether it sounds important. Do NOT rate importance or "
    "severity — judge only specific vs common-mode.\n\n"
    "Return exactly one verdict per change, each carrying the change's index as numbered. "
    "Submit your verdicts with the tool, exactly once."
)


def specificity_version(model_name: str = DEFAULT_SPECIFICITY_MODEL) -> str:
    """A reproducibility tag = model + a hash of the BASE prompt (the instructions). The
    catalog identity rides separately on catalog_version, so a catalog change does not
    change specificity_version — the two versions key the stored verdict independently
    (mirrors judge_version / synthesis_version)."""
    prompt_sha = hashlib.sha256(_SYSTEM_PROMPT_BASE.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+specificity-{prompt_sha}"


def render_specificity_system_prompt(catalog: Sequence[tuple[str, str]]) -> str:
    """The full system prompt = base instructions + the rendered catalog (slug: archetype).
    Held constant across all filings in a run so it caches; the base half is what
    specificity_version hashes."""
    lines = [f"- {slug}: {archetype}" for slug, archetype in catalog]
    return (
        _SYSTEM_PROMPT_BASE
        + "\n\nCOMMON-MODE THEME CATALOG (slug: archetype):\n"
        + "\n".join(lines)
    )


def build_specificity_classifier(model_name: str = DEFAULT_SPECIFICITY_MODEL) -> Any:
    """A Claude model bound to the specificity tool, forced to call it once. The catalog is
    supplied per-call via the (cached) system prompt, not baked into the binding."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_specificity",
        "description": "Submit the specific-vs-boilerplate verdicts. Call exactly once.",
        "input_schema": SpecificityBatch.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_specificity"})


def _build_user_prompt(changes: Sequence[SpecificityInput]) -> str:
    lines = [f"{i}. [{c.category}] {c.explanation}" for i, c in enumerate(changes, start=1)]
    return "The company's material risk-factor changes this year:\n" + "\n".join(lines)


def classify_specificity(
    model: Any,
    *,
    system_prompt: str,
    changes: Sequence[SpecificityInput],
    model_name: str,
    accession_number: str | None = None,
) -> SpecificityBatch:
    """Classify a filing's material changes specific-vs-boilerplate via the bound `model`,
    with the catalog in the cached system prompt. Records the call for cost accounting even
    if parsing fails."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(changes)
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    emit_llm_call(
        model=model_name,
        stage="specificity",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract specificity verdicts")
    return SpecificityBatch.model_validate(tool_calls[0]["args"])


def sanitize_batch(
    batch: SpecificityBatch, *, n_changes: int, allowed_slugs: set[str]
) -> dict[int, ChangeSpecificity]:
    """Map the model's verdicts to change indices (1..n_changes), keeping the first per
    index, dropping out-of-range or duplicate indices, and nulling a matched_theme that is
    not a known catalog slug. Returns {change_index: verdict}. A missing index is simply
    absent — the caller leaves that change unclassified this run and the gap query re-opens
    the filing next tick."""
    out: dict[int, ChangeSpecificity] = {}
    for verdict in batch.verdicts:
        idx = verdict.change_index
        if idx < 1 or idx > n_changes or idx in out:
            continue
        theme = verdict.matched_theme if verdict.matched_theme in allowed_slugs else None
        out[idx] = verdict.model_copy(update={"matched_theme": theme})
    return out
