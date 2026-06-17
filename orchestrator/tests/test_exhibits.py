"""Tests for EX-99 exhibit rendering, the volume budget, and the red-flag scan.

Covers the deterministic half of exhibit handling (no LLM): the budgeted prompt
block, the truncation metrics that make any cut visible, the 99.1-first ordering
that keeps truncation on the supplemental tail, and the adverse-term scan over
the dropped tail that stops a filer burying bad news past the budget.
"""

from __future__ import annotations

from datetime import date

from filings_orchestrator.classify.exhibits import (
    RED_FLAG_TERMS,
    render_exhibits,
    scan_red_flags,
)
from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Exhibit, Filing


def _doc(*exhibits: Exhibit) -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form="8-K",
        accession_number="0000000005-26-000001",
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="https://example.test/f.htm",
    )
    return FilingDocument(filing=filing, text="body", exhibits=list(exhibits), raw_size_bytes=4)


def _ex(subnum: int, text: str) -> Exhibit:
    return Exhibit(
        exhibit_type=f"EX-99.{subnum}",
        document=f"ex{subnum}.htm",
        url=f"https://example.test/ex{subnum}.htm",
        text=text,
    )


def test_no_exhibits_yields_empty_block() -> None:
    r = render_exhibits(_doc())
    assert r.block == ""
    assert r.exhibit_count == 0
    assert r.truncated is False


def test_exhibit_within_budget_is_rendered_whole_untruncated() -> None:
    r = render_exhibits(_doc(_ex(1, "Press release announcing results.")), budget=1000)
    assert r.exhibit_count == 1
    assert "Press release announcing results." in r.block
    assert r.truncated is False
    assert r.dropped_chars == 0
    assert r.used_chars == r.total_chars


def test_budget_truncates_and_reports_dropped_chars() -> None:
    r = render_exhibits(_doc(_ex(1, "X" * 100)), budget=40)
    assert r.truncated is True
    assert r.used_chars == 40
    assert r.dropped_chars == 60
    assert r.total_chars == 100
    assert len(r.dropped_text) == 60


def test_budget_spent_on_991_first_drops_the_tail_exhibit() -> None:
    # 99.1 fills the budget; 99.2 is entirely dropped (the tail, not the primary).
    r = render_exhibits(_doc(_ex(1, "A" * 50), _ex(2, "B" * 50)), budget=50)
    assert "AAAA" in r.block
    assert "BBBB" not in r.block  # tail exhibit dropped from the prompt
    assert r.dropped_text == "B" * 50
    assert r.dropped_chars == 50


def test_scan_red_flags_finds_curated_terms() -> None:
    flags = scan_red_flags("The auditor expressed substantial doubt about going concern.")
    assert "going concern" in flags


def test_scan_red_flags_empty_on_clean_text() -> None:
    assert scan_red_flags("Quarterly revenue rose and the dividend was maintained.") == []


def test_red_flag_in_dropped_tail_is_detectable() -> None:
    # The bury scenario: benign lede within budget, "material weakness" past it.
    benign = "We are pleased to report record revenue. " * 5
    buried = benign + " Note: the company identified a material weakness in controls."
    r = render_exhibits(_doc(_ex(1, buried)), budget=len(benign))
    assert r.truncated is True
    assert "material weakness" in scan_red_flags(r.dropped_text)


def test_red_flag_terms_are_lowercase() -> None:
    # scan_red_flags lowercases input and substring-matches, so terms must be lower.
    assert all(t == t.lower() for t in RED_FLAG_TERMS)
