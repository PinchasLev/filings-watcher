"""Tests for risk-factor section segmentation (ADR 0042; robustness fix).

Hermetic and offline — inline synthetic 10-K HTML exercises the real structural cases:
the table-of-contents vs the real section, the several ways filers title the heading
("Item 1A. Risk Factors", "Item 1A—Risk Factors", plain "Risk Factors"), a non-bold
heading (the fallback path), bold-header splitting, an inline bold word that must not
over-segment, the Item 1B boundary, the Item-1 cross-reference over-capture guard, and
the degenerate-parse floor. Fixtures are sized past that floor so a real section clears
it while a table-of-contents entry does not.
"""

from __future__ import annotations

from filings_orchestrator.change_detection.sectioning import (
    RiskFactorBlock,
    _block_hash,
    _normalize_ws,
    segment_risk_factors,
)

# A long body so each risk factor — and the section as a whole — clears the
# degenerate-parse floor, as a real section does and a TOC entry does not.
_BODY = (
    "A significant portion of our results depends on this factor, and adverse "
    "developments could materially and adversely affect our business, financial "
    "condition, results of operations, cash flows, and prospects in ways that are "
    "difficult to predict, outside our control, and potentially prolonged. We may be "
    "unable to mitigate these effects through pricing, cost control, or other measures. "
)

_FACTORS = [
    (
        "Our revenue is concentrated among a small number of large customers, and the "
        "loss of any of them would harm our operating results.",
        "A significant portion of our revenue is derived from a few large customers. If "
        "we <b>lose</b> one of these customers, or if a customer reduces its purchases, "
        "our operating results would be materially and adversely affected. " + _BODY,
    ),
    (
        "We have a history of operating losses and may never achieve or sustain profitability.",
        "We have incurred operating losses in each fiscal year since our inception. " + _BODY,
    ),
    (
        "Substantial doubt may exist about our ability to continue as a going concern.",
        "Our recurring losses and negative cash flows raise substantial doubt about our "
        "ability to continue as a going concern within one year. " + _BODY,
    ),
]

_INTRO = (
    "The following risk factors could materially and adversely affect our business. You "
    "should carefully consider all of the risks described below before making an "
    "investment decision. " + _BODY
)

_TOC = (
    "<div><b>Item 1A.</b> Risk Factors .......... 12</div>"
    "<div><b>Item 1B.</b> Unresolved Staff Comments .......... 40</div>"
    "<div><b>Item 2.</b> Properties .......... 41</div>"
)
_TAIL = (
    '<div style="font-weight:bold">Item 1B. Unresolved Staff Comments</div><div>None.</div>'
    '<div style="font-weight:bold">Item 2. Properties</div>'
    "<div>Our corporate headquarters are in Delaware under a long-term lease.</div>"
)


def _factors_html(bold_factors: bool) -> str:
    weight = ' style="font-weight:700"' if bold_factors else ""
    parts = [f"<div>{_INTRO}</div>"]
    for heading, body in _FACTORS:
        parts.append(f"<div{weight}>{heading}</div><div>{body}</div>")
    return "".join(parts)


def _tenk(
    heading: str, *, bold_heading: bool = True, bold_factors: bool = True, before_section: str = ""
) -> str:
    """A synthetic 10-K: TOC, optional Item-1 prose, the Risk Factors heading (as given),
    three risk factors, and the Item 1B / Item 2 boundary."""
    hw = ' style="font-weight:bold"' if bold_heading else ""
    return (
        "<html><body>"
        + _TOC
        + before_section
        + f"<div{hw}>{heading}</div>"
        + _factors_html(bold_factors)
        + _TAIL
        + "</body></html>"
    )


def test_standard_item1a_heading_splits_intro_plus_one_per_factor() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    assert len(blocks) == 4  # intro + three bold-headed risk factors
    assert all(isinstance(b, RiskFactorBlock) for b in blocks)
    assert blocks[0].heading is None
    assert blocks[1].heading is not None and "revenue is concentrated" in blocks[1].heading.lower()
    assert blocks[3].heading is not None and "going concern" in blocks[3].heading.lower()


def test_heading_without_item_prefix() -> None:
    # Nike / McDonald's title the real heading just "Risk Factors" (Item 1A only in TOC).
    blocks = segment_risk_factors(_tenk("Risk Factors"))
    assert len(blocks) == 4
    assert "going concern" in " ".join(b.text.lower() for b in blocks)


def test_heading_item1a_em_dash() -> None:
    # Costco writes "Item 1A—Risk Factors" (em-dash separator).
    blocks = segment_risk_factors(_tenk("Item 1A—Risk Factors"))
    assert len(blocks) == 4
    assert "going concern" in " ".join(b.text.lower() for b in blocks)


def test_non_bold_heading_uses_fallback() -> None:
    # Intel / JPMorgan don't bold the heading; the specific-text fallback still locates it.
    blocks = segment_risk_factors(
        _tenk("Item 1A. Risk Factors", bold_heading=False, bold_factors=False)
    )
    assert len(blocks) >= 1
    assert all(b.heading is None for b in blocks)  # no bold headers -> size-merge fallback
    joined = " ".join(b.text.lower() for b in blocks)
    assert "going concern" in joined
    assert "none." not in joined  # boundary still excludes Item 1B


def test_going_concern_isolated_in_one_block() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    assert sum("going concern" in b.text.lower() for b in blocks) == 1


def test_picks_real_section_not_table_of_contents() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    assert any("carefully consider all of the risks" in b.text.lower() for b in blocks)


def test_boundary_excludes_later_items() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    joined = " ".join(b.text.lower() for b in blocks)
    assert "unresolved staff comments" not in joined
    assert "corporate headquarters" not in joined


def test_inline_bold_word_does_not_start_a_new_block() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    assert "lose one of these customers" in blocks[1].text.lower()


def test_item1_crossreference_does_not_leak_business() -> None:
    # A (non-bold) "Item 1A. Risk Factors" cross-reference inside a long Item-1 Business
    # section must not start the section and pull Business content in. The real bold
    # heading, which follows, is the true start.
    crossref = (
        "<div>See Item 1A. Risk Factors below for a discussion of the risks. "
        "BUSINESSMARKER our operating segments and products are described here. " + _BODY + "</div>"
    ) * 4
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors", before_section=crossref))
    joined = " ".join(b.text.lower() for b in blocks)
    assert "businessmarker" not in joined
    assert "going concern" in joined


def test_degenerate_tiny_section_is_suppressed() -> None:
    # Only a TOC (Item 1A a line from Item 1B) and no real section -> empty, not a
    # misleading fragment.
    assert segment_risk_factors("<html><body>" + _TOC + "</body></html>") == []


def test_no_risk_factors_section_returns_empty() -> None:
    assert (
        segment_risk_factors("<html><body><p>No risk factors here at all.</p></body></html>") == []
    )


def test_blocks_have_sequential_index_and_stable_hash() -> None:
    blocks = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    for i, b in enumerate(blocks):
        assert b.index == i
        assert len(b.block_hash) == 64
        int(b.block_hash, 16)  # valid hex — raises if not


def test_hash_is_whitespace_invariant_but_content_sensitive() -> None:
    assert _normalize_ws("a  b\n c\t d") == "a b c d"
    assert _block_hash(_normalize_ws("Foo  bar")) == _block_hash(_normalize_ws("Foo bar"))
    assert _block_hash(_normalize_ws("Foo bar")) != _block_hash(_normalize_ws("Foo baz"))


# --- tier 3: modern inline-XBRL filings located via table-of-contents anchors ---
# Reproduces the Intel / Microsoft failure mode: a NON-bold Risk Factors heading laid out
# as a table row (item number and title in separate cells), with the title's word
# fragmented across tagged spans ("Ris" + "k" + " Factors") so the text tiers can't match
# it — but an auto-generated TOC of in-page anchor links whose targets bound the section.


def _inline_xbrl_tenk(*, fragment_heading: bool = True, with_end_anchor: bool = True) -> str:
    title = "Ris<span>k</span> Factors" if fragment_heading else "Risk Factors"
    end_link = (
        '<tr><td><a href="#usc">Unresolved Staff Comments</a></td><td>30</td></tr>'
        if with_end_anchor
        else ""
    )
    toc = (
        "<table>"
        '<tr><td><a href="#biz">Business</a></td><td>1</td></tr>'
        '<tr><td><a href="#rf">Risk Factors</a></td><td>14</td></tr>' + end_link + "</table>"
    )
    # Non-bold (font-weight:400) table-row heading — the tier-1/2 blind spot.
    heading = (
        '<div id="rf"><table><tr>'
        '<td><span style="font-weight:400">Item 1A.</span></td>'
        f'<td><span style="font-weight:400">{title}</span></td>'
        "</tr></table></div>"
    )
    end = (
        '<div id="usc"><div style="font-weight:bold">Item 1B. Unresolved Staff Comments</div>'
        "<div>None.</div></div>"
    )
    return (
        "<html><body>" + toc + heading + _factors_html(bold_factors=True) + end + "</body></html>"
    )


def test_inline_xbrl_located_via_toc_anchor() -> None:
    blocks = segment_risk_factors(_inline_xbrl_tenk())
    assert len(blocks) >= 3  # intro + bold-headed risk factors
    joined = " ".join(b.text.lower() for b in blocks)
    assert "going concern" in joined
    assert "unresolved staff comments" not in joined  # the end anchor bounds the section
    assert "none." not in joined


def test_inline_xbrl_fragmented_heading_still_locates() -> None:
    # The fragmented "Ris k Factors" heading is exactly what defeats the text tiers; the
    # TOC anchor locates the section regardless of how the heading text is broken up.
    frag = segment_risk_factors(_inline_xbrl_tenk(fragment_heading=True))
    assert any("going concern" in b.text.lower() for b in frag)


def test_inline_xbrl_without_end_anchor_degrades_to_empty() -> None:
    # No next-section TOC link to bound the end -> safe degrade to no blocks, never an
    # over-capture that swallows the rest of the document.
    assert segment_risk_factors(_inline_xbrl_tenk(with_end_anchor=False)) == []


def test_toc_anchor_tier_not_used_when_bold_heading_present() -> None:
    # A normal bold-heading filing must still be located by tier 1 even if it also carries
    # TOC anchors — the new tier only runs when the text tiers find nothing.
    bold = segment_risk_factors(_tenk("Item 1A. Risk Factors"))
    assert len(bold) == 4  # unchanged from the tier-1 standard case


# --- over-capture guard: inline-XBRL end markers undetected -> span runs too long ---


def _oversize_tenk(body_reps: int) -> str:
    body = "An adverse development could materially and adversely affect our business. " * body_reps
    return (
        "<html><body>"
        '<div style="font-weight:bold">Item 1A. Risk Factors</div>'
        f"<div>{body}</div>"
        '<div style="font-weight:bold">Item 1B. Unresolved Staff Comments</div><div>None.</div>'
        "</body></html>"
    )


def test_oversized_section_suppressed_as_over_capture() -> None:
    # ~550k chars between the located start and end -> an over-capture (the Morgan Stanley
    # case, where undetected Item 1B/1C/2 markers let the span run to Item 9A). Suppressed.
    assert segment_risk_factors(_oversize_tenk(7500)) == []


def test_large_section_under_cap_is_kept() -> None:
    # A genuinely large (but plausible) section stays -> the guard never over-suppresses.
    blocks = segment_risk_factors(_oversize_tenk(1500))  # ~110k chars
    total = sum(len(b.text) for b in blocks)
    assert blocks and 1500 < total < 400_000
