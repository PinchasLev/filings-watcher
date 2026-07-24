"""Unit tests for the pure block-diff (ADR 0042, PR 4).

Deterministic vectors with controlled cosine similarity: identical vectors give
1.0, the "changed" vector sits at 0.866 to its prior counterpart (inside the
change band), and orthogonal vectors give 0.0. diff_blocks normalizes internally,
so raw vectors are fine.
"""

from __future__ import annotations

from filings_orchestrator.change_detection import DiffResult, diff_blocks

_PRIOR = [(0, [1.0, 0.0, 0.0]), (1, [0.0, 1.0, 0.0])]


def _counts(r: DiffResult) -> tuple[int, int, int, int]:
    return (r.carried, r.changed, r.added, r.dropped)


def test_identical_blocks_are_carried() -> None:
    r = diff_blocks([(0, [1.0, 0.0, 0.0]), (1, [0.0, 1.0, 0.0])], _PRIOR)
    assert _counts(r) == (2, 0, 0, 0)
    assert r.changes == []  # carried blocks are counted, not listed


def test_changed_added_dropped_together() -> None:
    current = [
        (0, [1.0, 0.0, 0.0]),  # identical to prior 0 -> carried
        (1, [0.5, 0.866, 0.0]),  # cosine 0.866 to prior 1 -> changed
        (2, [0.0, 0.0, 1.0]),  # orthogonal to both -> added
    ]
    r = diff_blocks(current, _PRIOR)
    assert _counts(r) == (1, 1, 1, 0)  # both prior blocks are matched -> nothing dropped
    by_type = {c.change_type: c for c in r.changes}
    assert set(by_type) == {"changed", "added"}
    assert by_type["changed"].current_block_index == 1
    assert by_type["changed"].prior_block_index == 1
    assert by_type["added"].current_block_index == 2
    assert by_type["added"].prior_block_index is None


def test_dropped_block() -> None:
    r = diff_blocks([(0, [1.0, 0.0, 0.0])], _PRIOR)  # prior 1 has no current match
    assert _counts(r) == (1, 0, 0, 1)
    dropped = [c for c in r.changes if c.change_type == "dropped"]
    assert len(dropped) == 1
    assert dropped[0].prior_block_index == 1
    assert dropped[0].current_block_index is None


def test_empty_prior_makes_everything_added() -> None:
    r = diff_blocks([(0, [1.0, 0.0, 0.0]), (1, [0.0, 1.0, 0.0])], [])
    assert _counts(r) == (0, 0, 2, 0)
    assert all(c.change_type == "added" for c in r.changes)


def test_empty_current_makes_everything_dropped() -> None:
    r = diff_blocks([], _PRIOR)
    assert _counts(r) == (0, 0, 0, 2)
    assert all(c.change_type == "dropped" for c in r.changes)


def test_both_empty() -> None:
    assert diff_blocks([], []) == DiffResult([], 0, 0, 0, 0)
