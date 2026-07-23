"""Tests for risk-factor section segmentation (ADR 0042, PR 1).

Hermetic and offline — small synthetic 10-K HTML fixtures exercise the real
structural cases: table-of-contents vs the real section, bold-header splitting, an
inline bold word that must not over-segment, the Item 1B boundary, and the no-bold
fallback path.
"""

from __future__ import annotations

from pathlib import Path

from filings_orchestrator.change_detection.sectioning import (
    RiskFactorBlock,
    _block_hash,
    _normalize_ws,
    segment_risk_factors,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _risk_factors_html() -> str:
    return (FIXTURES / "sample_10k_risk_factors.html").read_text()


def _no_bold_html() -> str:
    return (FIXTURES / "sample_10k_no_bold_headers.html").read_text()


def test_splits_into_intro_plus_one_block_per_risk_factor() -> None:
    blocks = segment_risk_factors(_risk_factors_html())
    # section intro + three bold-headed risk factors
    assert len(blocks) == 4
    assert all(isinstance(b, RiskFactorBlock) for b in blocks)


def test_intro_has_no_heading_risk_factors_do() -> None:
    blocks = segment_risk_factors(_risk_factors_html())
    assert blocks[0].heading is None
    assert blocks[1].heading is not None and "revenue is concentrated" in blocks[1].heading.lower()
    assert blocks[2].heading is not None and "operating losses" in blocks[2].heading.lower()
    assert blocks[3].heading is not None and "going concern" in blocks[3].heading.lower()


def test_going_concern_is_isolated_in_one_block() -> None:
    blocks = segment_risk_factors(_risk_factors_html())
    with_gc = [b for b in blocks if "going concern" in b.text.lower()]
    assert len(with_gc) == 1


def test_picks_real_section_not_table_of_contents() -> None:
    # The intro prose exists only in the real section, never in the TOC entry.
    blocks = segment_risk_factors(_risk_factors_html())
    assert any("carefully consider all of the risks" in b.text.lower() for b in blocks)


def test_boundary_excludes_later_items() -> None:
    blocks = segment_risk_factors(_risk_factors_html())
    joined = " ".join(b.text.lower() for b in blocks)
    assert "unresolved staff comments" not in joined
    assert "corporate headquarters" not in joined


def test_inline_bold_word_does_not_start_a_new_block() -> None:
    # "<b>lose</b>" mid-sentence must stay inside its risk-factor block, not split it.
    blocks = segment_risk_factors(_risk_factors_html())
    assert "lose one of these customers" in blocks[1].text.lower()


def test_blocks_have_sequential_index_and_stable_hash() -> None:
    blocks = segment_risk_factors(_risk_factors_html())
    for i, b in enumerate(blocks):
        assert b.index == i
        assert len(b.block_hash) == 64
        int(b.block_hash, 16)  # valid hex — raises if not


def test_fallback_when_no_bold_headers() -> None:
    blocks = segment_risk_factors(_no_bold_html())
    assert len(blocks) >= 1
    assert all(b.heading is None for b in blocks)  # fallback blocks are headingless
    joined = " ".join(b.text.lower() for b in blocks)
    assert "supply chain" in joined
    assert "numerous risks" in joined
    assert "none." not in joined  # boundary still excludes Item 1B


def test_no_risk_factors_section_returns_empty() -> None:
    html = "<html><body><p>This filing has no risk factors section at all.</p></body></html>"
    assert segment_risk_factors(html) == []


def test_hash_is_whitespace_invariant_but_content_sensitive() -> None:
    assert _normalize_ws("a  b\n c\t d") == "a b c d"
    assert _block_hash(_normalize_ws("Foo  bar")) == _block_hash(_normalize_ws("Foo bar"))
    assert _block_hash(_normalize_ws("Foo bar")) != _block_hash(_normalize_ws("Foo baz"))
