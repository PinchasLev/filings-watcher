"""Tests for the EDGAR `getcurrent` Atom feed parser (ADR 0029)."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.atom_feed import (
    atom_feed_url,
    fetch_atom_feed,
    filter_form,
    parse_atom_feed,
)

FIXTURES = Path(__file__).parent / "fixtures"

_SAMPLE_ATOM = (FIXTURES / "atom_feed_8k.xml").read_text()


def test_atom_feed_url_composes_with_defaults() -> None:
    url = atom_feed_url()
    assert url == (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K&output=atom&count=100"
    )


def test_atom_feed_url_honors_overrides() -> None:
    url = atom_feed_url(form="10-K", count=40)
    assert url.endswith("type=10-K&output=atom&count=40")


def test_parse_atom_feed_returns_only_well_formed_entries() -> None:
    """Fixture has 6 entries: 4 well-formed, 1 missing <id>, 1 with an
    unparseable title. Parser must return the 4 well-formed entries and
    silently skip the rest."""
    entries = parse_atom_feed(_SAMPLE_ATOM)
    accessions = [e.accession_number for e in entries]
    assert accessions == [
        "0001822492-26-000089",
        "0001193125-26-258508",
        "0000123456-26-000001",
        "0000999888-26-000123",
    ]


def test_parse_atom_feed_extracts_cik_and_company_from_title() -> None:
    entries = parse_atom_feed(_SAMPLE_ATOM)
    hillman = entries[0]
    assert hillman.cik == "0001822492"
    assert hillman.company_name == "Hillman Solutions Corp."


def test_parse_atom_feed_preserves_filing_timestamp_verbatim() -> None:
    """`updated_at` carries the full ISO 8601 string with timezone offset.
    Atom is the only path that exposes sub-day filing granularity; the
    daily-index path is date-only. Don't lose it during parse."""
    entries = parse_atom_feed(_SAMPLE_ATOM)
    assert entries[0].updated_at == "2026-06-05T09:05:09-04:00"


def test_parse_atom_feed_handles_company_name_with_hyphen() -> None:
    """Company names sometimes contain embedded hyphens (the same separator
    used between form and company in the title). The title regex must anchor
    on the trailing CIK+role parens and not split on the first hyphen."""
    entries = parse_atom_feed(_SAMPLE_ATOM)
    acme = next(e for e in entries if e.cik == "0000123456")
    assert acme.company_name == "Acme Corp, A Co. with Punctuation - Inc."


def test_parse_atom_feed_takes_form_from_category_term() -> None:
    """The category element's `term` attribute is the authoritative form
    tagging. The 8-K/A entry must parse as `8-K/A`, not `8-K` (the title's
    leading token is identical to the category term here, but for amendments
    they diverge in some legacy feeds; trust the category)."""
    entries = parse_atom_feed(_SAMPLE_ATOM)
    amend = next(e for e in entries if e.cik == "0000999888")
    assert amend.form == "8-K/A"


def test_filter_form_is_exact_match() -> None:
    """`8-K/A` must not be picked up by a plain `8-K` filter — mirrors
    daily_index.filter_form. Amendments are handled separately if at all."""
    entries = parse_atom_feed(_SAMPLE_ATOM)
    only_8k = filter_form(entries, "8-K")
    forms = {e.form for e in only_8k}
    assert forms == {"8-K"}
    accessions = {e.accession_number for e in only_8k}
    assert "0000999888-26-000123" not in accessions


def test_fetch_atom_feed_uses_correct_url() -> None:
    target_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K&output=atom&count=100"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(target_url).mock(return_value=httpx.Response(200, text=_SAMPLE_ATOM))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            text = fetch_atom_feed(client)
    assert "<feed" in text
    assert "8-K" in text
