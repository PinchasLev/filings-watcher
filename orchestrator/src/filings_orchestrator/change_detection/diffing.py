"""Pairwise diff of a filing's risk-factor blocks against the prior period (ADR 0042).

Given this year's block vectors and last year's, align them by cosine similarity and
classify each block:

- **carried** — a current block near-identical to a prior one (>= carry threshold):
  unchanged, not surfaced.
- **changed** — a current block that matches a prior one but below the carry
  threshold: an edited version of that risk factor.
- **added** — a current block with no good prior match: new content.
- **dropped** — a prior block with no good current match: removed content.

changed + added + dropped are the shortlist a later stage judges for materiality;
carried blocks are only counted. This is the recall stage — deterministic given the
embeddings, no LLM. Thresholds are heuristic: the embedding decides *what changed*,
the LLM decides *whether it matters*, so exact cutoffs need not be perfect here.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

# A current block whose best prior match is at or above this is treated as unchanged.
_CARRY_THRESHOLD = 0.95
# Between change and carry: a matched-but-edited block. Below it: no real counterpart
# (added on the current side, dropped on the prior side).
_CHANGE_THRESHOLD = 0.70

CHANGE_ADDED = "added"
CHANGE_CHANGED = "changed"
CHANGE_DROPPED = "dropped"


class BlockChange(NamedTuple):
    """One entry of the diff shortlist. current/prior index is None when the block
    exists on only one side (added has no prior, dropped has no current)."""

    change_type: str
    current_block_index: int | None
    prior_block_index: int | None
    similarity: float | None


class DiffResult(NamedTuple):
    """The shortlist (added/changed/dropped) plus counts of every category."""

    changes: list[BlockChange]
    added: int
    changed: int
    carried: int
    dropped: int


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0.0, 1.0, norms)


def diff_blocks(
    current: list[tuple[int, list[float]]],
    prior: list[tuple[int, list[float]]],
    *,
    carry_threshold: float = _CARRY_THRESHOLD,
    change_threshold: float = _CHANGE_THRESHOLD,
) -> DiffResult:
    """Align current vs prior block vectors and classify each. Inputs are
    (block_index, vector) pairs; vectors are L2-normalized here, so callers need not
    pre-normalize. A block present on only one side is added (current) or dropped
    (prior)."""
    changes: list[BlockChange] = []

    if not current and not prior:
        return DiffResult([], 0, 0, 0, 0)
    if not prior:
        changes = [BlockChange(CHANGE_ADDED, i, None, None) for i, _ in current]
        return DiffResult(changes, len(current), 0, 0, 0)
    if not current:
        changes = [BlockChange(CHANGE_DROPPED, None, j, None) for j, _ in prior]
        return DiffResult(changes, 0, 0, 0, len(prior))

    cur_idx = [i for i, _ in current]
    pri_idx = [j for j, _ in prior]
    cur_mat = _normalize(np.asarray([v for _, v in current], dtype=np.float64))
    pri_mat = _normalize(np.asarray([v for _, v in prior], dtype=np.float64))
    sim = cur_mat @ pri_mat.T  # cosine, since both sides are unit-normalized

    added = changed = carried = dropped = 0

    # Current side: each block is carried, an edit of a prior block, or brand new.
    for row, i in zip(sim, cur_idx, strict=True):
        best_j = int(np.argmax(row))
        best = float(row[best_j])
        if best >= carry_threshold:
            carried += 1
        elif best >= change_threshold:
            changes.append(BlockChange(CHANGE_CHANGED, i, pri_idx[best_j], round(best, 4)))
            changed += 1
        else:
            changes.append(BlockChange(CHANGE_ADDED, i, None, round(best, 4)))
            added += 1

    # Prior side: a block nothing current resembles was dropped.
    for col, j in zip(sim.T, pri_idx, strict=True):
        best = float(np.max(col))
        if best < change_threshold:
            changes.append(BlockChange(CHANGE_DROPPED, None, j, round(best, 4)))
            dropped += 1

    return DiffResult(changes, added, changed, carried, dropped)
