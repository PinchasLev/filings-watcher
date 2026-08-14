"""Common-mode theme catalog extraction (calibration; extends ADR 0042/0043).

The change-detection judge scores each company against only its own prior year, so
common-mode macro boilerplate — the themes the whole cohort flags in common (tariffs,
generative-AI, rate/refinancing) — inflates every card and buries the idiosyncratic,
company-specific signal. Calibration discounts that boilerplate. Bottom-up mechanisms
(per-change prompts, raw vector similarity) failed because the boilerplate-vs-signal line
is semantic and needs the corpus-level vantage; the top-down reduce works.

This stage is that reduce: ONE Sonnet call over a digest of a filing season's material
changes (`COMPANY | theme | explanation`) that NAMES the recurring common-mode themes,
gives an archetype sentence for each, and estimates its prevalence. The result is a
versioned catalog a later stage classifies each change against (specific vs boilerplate).

The prevalence estimate is the model's own read over the digest it is shown; code owns only
the versioning and storage. The catalog is content-addressed (see catalog_version): a
change to the extracted themes yields a new immutable version, so a downstream
classification always knows exactly which catalog it used. Reuses the synthesis
structured-output discipline (forced single tool call, temperature 0, cached system prompt).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.cost import emit_llm_call

# The reduce is one call over the whole corpus digest — low volume, high judgment value,
# and the step the R&D showed only a stronger model gets right (Haiku could not hold the
# cross-company view). So it runs on Sonnet, like the synthesis reduce.
DEFAULT_CATALOG_MODEL = "claude-sonnet-4-6"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Normalize a theme name to a stable snake_case slug so the catalog's identity is
    insensitive to cosmetic model formatting ('Tariffs / Trade' -> 'tariffs_trade')."""
    return _SLUG_RE.sub("_", value.strip().lower()).strip("_")


class CatalogTheme(BaseModel):
    """One common-mode theme: a snake_case slug, a one-sentence archetype stating the
    generic (boilerplate) form of the theme, and an estimated prevalence (how many
    companies in the digest flag it)."""

    theme_slug: str = Field(
        description=(
            "A short snake_case identifier for the common-mode theme, e.g. "
            "'tariffs_trade_policy', 'generative_ai_competition', 'rate_refinancing'."
        )
    )
    archetype: str = Field(
        description=(
            "One sentence stating the GENERIC, boilerplate form of this theme as many "
            "companies express it — the common-mode version, naming no specific company."
        )
    )
    prevalence: int = Field(
        default=0,
        description="Approximately how many companies in the digest flag this theme (an estimate).",
    )

    @field_validator("theme_slug", mode="before")
    @classmethod
    def _coerce_slug(cls, value: object) -> str:
        return _slugify(str(value))

    @field_validator("prevalence", mode="before")
    @classmethod
    def _coerce_prevalence(cls, value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            try:
                return max(0, int(float(value.strip())))
            except ValueError:
                return 0
        return 0


class DisclosureCatalog(BaseModel):
    """The reduce's structured output — the common-mode theme catalog. Matches the bound
    tool schema; field order and descriptions are what the model reads."""

    themes: list[CatalogTheme] = Field(
        description=(
            "The recurring common-mode themes across the cohort — the macro/boilerplate "
            "risks many DIFFERENT companies flag in common. Name only shared cross-company "
            "themes; do NOT include any company-specific or idiosyncratic event."
        )
    )

    @field_validator("themes", mode="before")
    @classmethod
    def _coerce_themes(cls, value: object) -> list[Any]:
        """A malformed themes field must not sink the whole reduce — coerce a non-list to
        an empty list (the CLI treats a zero-theme catalog as 'nothing to cut')."""
        return value if isinstance(value, list) else []


_SYSTEM_PROMPT = (
    "You are given a digest of the MATERIAL year-over-year changes many companies made to "
    "their 10-K Risk Factors this filing season. Your job is to name the COMMON-MODE themes: "
    "the macro, boilerplate risks that recur across many DIFFERENT companies because the "
    "whole market faces them in common — for example tariff and trade-policy exposure, "
    "generative-AI competition and disruption, interest-rate and refinancing pressure, "
    "cybersecurity escalation, or data-privacy regulation.\n\n"
    "These common-mode themes are the NOISE a calibration step discounts so that each "
    "company's idiosyncratic, company-specific changes stand out. So name ONLY themes shared "
    "across many companies. Do NOT list any company-specific or idiosyncratic event (a "
    "particular merger, a named customer loss, a specific impairment or lawsuit, a leadership "
    "change) — those are exactly the signal the catalog must NOT absorb.\n\n"
    "For each common-mode theme provide: a short snake_case slug; one archetype sentence "
    "stating the generic, boilerplate form of the theme as companies typically express it "
    "(naming no company); and an estimate of how many companies in the digest flag it. Prefer "
    "a dozen or two well-separated themes over a long, redundant list.\n\n"
    "You may be shown the EXISTING catalog from the prior run. Treat it as the stable baseline: "
    "reuse every still-relevant theme EXACTLY — its existing slug and archetype VERBATIM, without "
    "re-wording, re-slugging, or reordering. ADD a theme only when the digest shows a genuinely "
    "new common-mode theme not already covered by an existing one; DROP a theme only when the "
    "digest no longer supports it. When the underlying themes have not changed, the catalog must "
    "come back identical — cosmetically rewording an unchanged theme is exactly what to avoid.\n\n"
    "Submit the catalog with the tool, exactly once."
)


def catalog_extractor_version(model_name: str = DEFAULT_CATALOG_MODEL) -> str:
    """Reproducibility tag for the EXTRACTOR config = model + a hash of the reduce prompt
    (mirrors synthesis_version). The full catalog_version appends the content hash, so two
    runs of the same extractor over different corpora/outputs remain distinct cuts."""
    prompt_sha = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    return f"{model_name}+catalog-{prompt_sha}"


def canonical_themes(themes: list[CatalogTheme]) -> list[CatalogTheme]:
    """Dedupe by slug (first wins), dropping empty slugs, and sort by slug — the canonical
    form used for both the content hash and storage, so a catalog's identity is
    order-independent and free of duplicate-slug collisions."""
    seen: dict[str, CatalogTheme] = {}
    for theme in themes:
        if theme.theme_slug and theme.theme_slug not in seen:
            seen[theme.theme_slug] = theme
    return [seen[slug] for slug in sorted(seen)]


def catalog_content_hash(themes: list[CatalogTheme]) -> str:
    """sha256 over the canonical (slug, archetype) pairs — the catalog's content fingerprint,
    order-independent via canonical_themes. Prevalence is deliberately EXCLUDED: it is a soft
    per-run model estimate that wobbles between runs, so folding it into the identity would
    mint a new catalog_version (and force a full re-classification wave downstream) on a purely
    cosmetic count change. A catalog's identity is its theme SET and each theme's meaning."""
    canon = canonical_themes(themes)
    payload = json.dumps(
        [[t.theme_slug, t.archetype] for t in canon],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def catalog_version(themes: list[CatalogTheme], model_name: str = DEFAULT_CATALOG_MODEL) -> str:
    """The full, content-addressed version: extractor identity + a hash of the extracted
    themes. A content change yields a new version (a new immutable cut)."""
    return f"{catalog_extractor_version(model_name)}-{catalog_content_hash(themes)[:8]}"


def build_catalog_extractor(model_name: str = DEFAULT_CATALOG_MODEL) -> Any:
    """A Claude model bound to the catalog tool, forced to call it once."""
    model = ChatAnthropic(model_name=model_name, timeout=120, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_catalog",
        "description": "Submit the common-mode theme catalog. Call exactly once.",
        "input_schema": DisclosureCatalog.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_catalog"})


def _build_user_prompt(digest: str, current_themes: Sequence[tuple[str, str]] = ()) -> str:
    existing = ""
    if current_themes:
        listing = "\n".join(f"- {slug}: {archetype}" for slug, archetype in current_themes)
        existing = (
            "EXISTING CATALOG (from the prior run — preserve each still-relevant theme "
            "VERBATIM, same slug and archetype):\n"
            f"{listing}\n\n"
        )
    return (
        existing + "Below is a digest of this filing season's MATERIAL year-over-year risk-factor "
        "changes across many companies, one per line as `COMPANY | theme | explanation`.\n\n"
        f"{digest}\n\n"
        "Update the catalog per the instructions — keep unchanged themes verbatim, add only "
        "genuinely-new common-mode themes, drop only obsolete ones — and submit it."
    )


def extract_catalog(
    model: Any,
    *,
    digest: str,
    model_name: str,
    current_themes: Sequence[tuple[str, str]] = (),
) -> DisclosureCatalog:
    """Reduce the corpus digest into the common-mode theme catalog via the bound `model`,
    seeding it with the current catalog so unchanged themes are carried forward verbatim
    (stable identity, no downstream churn). Records the call for cost accounting even if
    parsing fails (it still cost tokens)."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    user = _build_user_prompt(digest, current_themes)
    response = model.invoke([SystemMessage(content=system_blocks), HumanMessage(content=user)])
    emit_llm_call(model=model_name, stage="catalog", response=response)
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract catalog")
    return DisclosureCatalog.model_validate(tool_calls[0]["args"])
