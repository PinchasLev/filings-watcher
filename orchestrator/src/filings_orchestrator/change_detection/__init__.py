"""Disclosure change-detection over periodic filings (ADR 0042).

The prose half of the risk-monitoring product: surface the material changes in a
company's periodic filings, period over period. This package will grow through the
ADR 0042 PR sequence (segment -> embed -> diff -> judge -> surface); the first
module is the deterministic section segmentation that turns a filing's Risk Factors
into whole risk-factor blocks.
"""

from filings_orchestrator.change_detection.catalog import (
    DEFAULT_CATALOG_MODEL,
    CatalogTheme,
    DisclosureCatalog,
    build_catalog_extractor,
    canonical_themes,
    catalog_content_hash,
    catalog_extractor_version,
    catalog_version,
    extract_catalog,
)
from filings_orchestrator.change_detection.diffing import (
    BlockChange,
    DiffResult,
    diff_blocks,
)
from filings_orchestrator.change_detection.embeddings import (
    DEFAULT_MODEL,
    Embedder,
    VoyageEmbedder,
)
from filings_orchestrator.change_detection.materiality import (
    DEFAULT_JUDGE_MODEL,
    MaterialityVerdict,
    build_judge,
    judge_change,
    judge_version,
)
from filings_orchestrator.change_detection.realization import (
    DEFAULT_REALIZATION_MODEL,
    RealizationEvent,
    RealizationVerdict,
    build_realization_judge,
    build_user_content,
    evidence_is_grounded,
    judge_realization,
    prompt_fingerprint,
    quote_is_grounded,
    realization_evidence_is_grounded,
    realization_is_grounded,
    realization_version,
)
from filings_orchestrator.change_detection.sectioning import (
    RiskFactorBlock,
    segment_risk_factors,
)
from filings_orchestrator.change_detection.specificity import (
    DEFAULT_SPECIFICITY_MODEL,
    ChangeSpecificity,
    SpecificityBatch,
    SpecificityInput,
    build_specificity_classifier,
    classify_specificity,
    render_specificity_system_prompt,
    sanitize_batch,
    specificity_version,
)
from filings_orchestrator.change_detection.synthesis import (
    DEFAULT_SYNTHESIS_MODEL,
    DisclosureSynthesis,
    Finding,
    HeadlineDirection,
    HeadlineIntensity,
    StandingRiskSummary,
    build_standing_synthesizer,
    build_synthesizer,
    standing_synthesis_version,
    synthesis_version,
    synthesize,
    synthesize_standing,
)
from filings_orchestrator.change_detection.taxonomy import (
    RiskChangeCategory,
    RiskChangeDirection,
)

__all__ = [
    "DEFAULT_CATALOG_MODEL",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_MODEL",
    "DEFAULT_REALIZATION_MODEL",
    "DEFAULT_SPECIFICITY_MODEL",
    "DEFAULT_SYNTHESIS_MODEL",
    "BlockChange",
    "CatalogTheme",
    "ChangeSpecificity",
    "DiffResult",
    "DisclosureCatalog",
    "DisclosureSynthesis",
    "Embedder",
    "Finding",
    "HeadlineDirection",
    "HeadlineIntensity",
    "MaterialityVerdict",
    "RealizationEvent",
    "RealizationVerdict",
    "RiskChangeCategory",
    "RiskChangeDirection",
    "RiskFactorBlock",
    "SpecificityBatch",
    "SpecificityInput",
    "StandingRiskSummary",
    "VoyageEmbedder",
    "build_catalog_extractor",
    "build_judge",
    "build_realization_judge",
    "build_specificity_classifier",
    "build_standing_synthesizer",
    "build_synthesizer",
    "build_user_content",
    "canonical_themes",
    "catalog_content_hash",
    "catalog_extractor_version",
    "catalog_version",
    "classify_specificity",
    "diff_blocks",
    "evidence_is_grounded",
    "extract_catalog",
    "judge_change",
    "judge_realization",
    "judge_version",
    "prompt_fingerprint",
    "quote_is_grounded",
    "realization_evidence_is_grounded",
    "realization_is_grounded",
    "realization_version",
    "render_specificity_system_prompt",
    "sanitize_batch",
    "segment_risk_factors",
    "specificity_version",
    "standing_synthesis_version",
    "synthesis_version",
    "synthesize",
    "synthesize_standing",
]
