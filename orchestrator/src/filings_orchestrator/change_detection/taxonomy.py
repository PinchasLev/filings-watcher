"""Governed vocabulary for disclosure-change verdicts (ADR 0043).

Two small controlled vocabularies the materiality judge tags each change with:

- `RiskChangeCategory` — the *theme* a change belongs to. This is what the read
  surface groups by, so it must be a bounded set: free-text categories fragment a
  filing's forty changes into forty singleton groups and the grouping buys nothing.
  The governance here mirrors the event taxonomy's pattern (ADR 0032): a fixed enum
  with a single `OTHER` catch-all, so an unrecognized theme degrades to a known home
  rather than inventing a new bucket.
- `RiskChangeDirection` — whether the change made the risk *worse*, *eased* it, or
  left the risk level unchanged. Direction is the *meaning* of a change (a reader
  cares that leverage risk worsened, not that a block was "revised"), so it leads
  the display; the mechanical added/changed/dropped is secondary detail.

Governance stops at these labels. They exist to *organize* the surface; the rich,
specific per-change explanation the judge writes is deliberately left ungoverned —
that detail is where the insight lives (ADR 0043).

The vocabulary is rendered into the judge's system prompt, so any change to it
flows into the prompt hash and therefore the `judge_version` — re-judging writes new
rows rather than silently reinterpreting old ones (same append-only discipline as
the classifier taxonomy).
"""

from __future__ import annotations

from enum import StrEnum


class RiskChangeDirection(StrEnum):
    """Which way a change moved the risk. String enum so it flows through JSON
    Schema and tool-use arguments without custom serialization."""

    WORSE = "worse"
    EASED = "eased"
    NEUTRAL = "neutral"


class RiskChangeCategory(StrEnum):
    """The theme a risk-factor change belongs to — a bounded set the read surface
    groups by. `OTHER` is the catch-all for a change that fits no specific theme."""

    LIQUIDITY_GOING_CONCERN = "liquidity_going_concern"
    DEBT_CAPITAL_STRUCTURE = "debt_capital_structure"
    IMPAIRMENT_ASSET_VALUE = "impairment_asset_value"
    RESTRUCTURING_WORKFORCE = "restructuring_workforce"
    LITIGATION_LEGAL = "litigation_legal"
    REGULATORY_COMPLIANCE = "regulatory_compliance"
    MA_STRATEGIC = "ma_strategic"
    OPERATIONS_SUPPLY_CHAIN = "operations_supply_chain"
    MARKET_COMPETITION = "market_competition"
    TECHNOLOGY_CYBERSECURITY = "technology_cybersecurity"
    GOVERNANCE_CONTROLS = "governance_controls"
    MACRO_GEOPOLITICAL = "macro_geopolitical"
    ENVIRONMENTAL_CLIMATE = "environmental_climate"
    OTHER = "other"


DIRECTION_DESCRIPTIONS: dict[RiskChangeDirection, str] = {
    RiskChangeDirection.WORSE: (
        "The risk increased — newly disclosed, escalated in severity or likelihood, "
        "or a previously mild risk now described as serious."
    ),
    RiskChangeDirection.EASED: "The risk decreased, was resolved, or was removed.",
    RiskChangeDirection.NEUTRAL: (
        "No real change in risk level — rewording, reorganization, or a like-for-like "
        "update that does not shift the underlying risk."
    ),
}

CATEGORY_DESCRIPTIONS: dict[RiskChangeCategory, str] = {
    RiskChangeCategory.LIQUIDITY_GOING_CONCERN: (
        "Liquidity, cash runway, ability to continue as a going concern, funding needs."
    ),
    RiskChangeCategory.DEBT_CAPITAL_STRUCTURE: (
        "Debt, leverage, covenants, refinancing, credit ratings, dilution, capital raises."
    ),
    RiskChangeCategory.IMPAIRMENT_ASSET_VALUE: (
        "Asset impairments, write-downs, goodwill, valuation of assets or investments."
    ),
    RiskChangeCategory.RESTRUCTURING_WORKFORCE: (
        "Restructuring, layoffs, facility closures, reorganizations, labor relations, strikes."
    ),
    RiskChangeCategory.LITIGATION_LEGAL: (
        "Litigation, legal claims, investigations, settlements, contractual disputes."
    ),
    RiskChangeCategory.REGULATORY_COMPLIANCE: (
        "Laws, regulation, government action, licensing, sanctions, tax, enforcement."
    ),
    RiskChangeCategory.MA_STRATEGIC: (
        "Mergers, acquisitions, divestitures, strategic alternatives, integration risk."
    ),
    RiskChangeCategory.OPERATIONS_SUPPLY_CHAIN: (
        "Operations, manufacturing, production, supply chain, suppliers, product quality or safety."
    ),
    RiskChangeCategory.MARKET_COMPETITION: (
        "Competition, demand, pricing, customer concentration, market or industry conditions."
    ),
    RiskChangeCategory.TECHNOLOGY_CYBERSECURITY: (
        "Cybersecurity, data breaches, IT systems, intellectual property, technology change."
    ),
    RiskChangeCategory.GOVERNANCE_CONTROLS: (
        "Board or management, key personnel, internal controls, material weakness, governance."
    ),
    RiskChangeCategory.MACRO_GEOPOLITICAL: (
        "Macroeconomy, interest rates, inflation, currency, geopolitics, war, trade, pandemics."
    ),
    RiskChangeCategory.ENVIRONMENTAL_CLIMATE: (
        "Environmental liabilities, climate change, natural disasters, or ESG as a genuine risk."
    ),
    RiskChangeCategory.OTHER: "Use only when the change fits none of the specific themes above.",
}


def coerce_category(value: object) -> RiskChangeCategory:
    """Map a raw model value to a governed category, degrading to OTHER rather than
    failing. A stray or out-of-vocabulary label must not sink an otherwise-good
    verdict — the catch-all is the graceful home (ADR 0032 governance pattern)."""
    if isinstance(value, RiskChangeCategory):
        return value
    if isinstance(value, str):
        try:
            return RiskChangeCategory(value.strip().lower())
        except ValueError:
            return RiskChangeCategory.OTHER
    return RiskChangeCategory.OTHER


def coerce_direction(value: object) -> RiskChangeDirection:
    """Map a raw model value to a direction, degrading to NEUTRAL when unrecognized."""
    if isinstance(value, RiskChangeDirection):
        return value
    if isinstance(value, str):
        try:
            return RiskChangeDirection(value.strip().lower())
        except ValueError:
            return RiskChangeDirection.NEUTRAL
    return RiskChangeDirection.NEUTRAL


def render_category_guidance() -> str:
    """A compact `value — meaning` list for the judge's system prompt. Rendering the
    vocabulary into the prompt is what binds it to the judge_version hash."""
    return "\n".join(f"- {c.value}: {CATEGORY_DESCRIPTIONS[c]}" for c in RiskChangeCategory)


def render_direction_guidance() -> str:
    return "\n".join(f"- {d.value}: {DIRECTION_DESCRIPTIONS[d]}" for d in RiskChangeDirection)
