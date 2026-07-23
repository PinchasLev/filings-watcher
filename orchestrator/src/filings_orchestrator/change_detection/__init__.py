"""Disclosure change-detection over periodic filings (ADR 0042).

The prose half of the risk-monitoring product: surface the material changes in a
company's periodic filings, period over period. This package will grow through the
ADR 0042 PR sequence (segment -> embed -> diff -> judge -> surface); the first
module is the deterministic section segmentation that turns a filing's Risk Factors
into whole risk-factor blocks.
"""

from filings_orchestrator.change_detection.embeddings import (
    DEFAULT_MODEL,
    Embedder,
    VoyageEmbedder,
)
from filings_orchestrator.change_detection.sectioning import (
    RiskFactorBlock,
    segment_risk_factors,
)

__all__ = [
    "DEFAULT_MODEL",
    "Embedder",
    "RiskFactorBlock",
    "VoyageEmbedder",
    "segment_risk_factors",
]
