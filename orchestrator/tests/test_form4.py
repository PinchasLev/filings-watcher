"""Unit tests for the Form 4 ownership-document parser."""

from __future__ import annotations

from pathlib import Path

from filings_orchestrator.edgar.form4 import (
    extract_ownership_xml,
    parse_form4,
    submission_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
_OWNERSHIP = (FIXTURES / "form4_ownership.xml").read_text()
_OPTIONONLY = (FIXTURES / "form4_optiononly.xml").read_text()
_ACCESSION = "0001234567-26-000001"


def test_parse_form4_extracts_issuer_owner_and_role() -> None:
    f = parse_form4(_OWNERSHIP, _ACCESSION)
    assert f is not None
    assert f.accession_number == _ACCESSION
    assert f.period_of_report == "2026-06-22"
    assert f.issuer_cik == "0000000123"
    assert f.issuer_name == "ACME CORP"
    assert f.issuer_ticker == "ACME"
    assert f.owner_cik == "0000000456"
    assert f.owner_name == "DOE JANE"
    assert f.is_director is True
    assert f.is_officer is True
    assert f.is_ten_percent_owner is False
    assert f.officer_title == "Chief Executive Officer"
    assert f.is_10b5_1 is True
    assert f.not_subject_to_section16 is False


def test_parse_form4_extracts_nonderivative_transactions() -> None:
    f = parse_form4(_OWNERSHIP, _ACCESSION)
    assert f is not None
    assert len(f.transactions) == 2

    buy = f.transactions[0]
    assert buy.txn_seq == 0
    assert buy.transaction_code == "P"  # open-market buy
    assert buy.acquired_disposed == "A"
    assert buy.shares == 1000.0
    assert buy.price_per_share == 10.0
    assert buy.shares_owned_following == 5000.0
    assert buy.direct_or_indirect == "D"
    assert buy.transaction_date == "2026-06-22"
    assert buy.security_title == "Common Stock"

    sell = f.transactions[1]
    assert sell.txn_seq == 1
    assert sell.transaction_code == "S"
    assert sell.acquired_disposed == "D"
    assert sell.shares == 200.0
    assert sell.price_per_share == 12.5


def test_parse_form4_ownership_has_no_derivatives() -> None:
    f = parse_form4(_OWNERSHIP, _ACCESSION)
    assert f is not None
    assert f.derivative_transactions == []


def test_parse_form4_extracts_derivative_transactions() -> None:
    f = parse_form4(_OPTIONONLY, _ACCESSION)
    assert f is not None
    # Option-only filing: nothing in the non-derivative table, one derivative line.
    assert f.transactions == []
    assert len(f.derivative_transactions) == 1
    d = f.derivative_transactions[0]
    assert d.txn_seq == 0
    assert d.security_title == "Employee Stock Option (Right to Buy)"
    assert d.conversion_exercise_price == 15.5
    assert d.transaction_code == "A"  # grant
    assert d.acquired_disposed == "A"
    assert d.shares == 5000.0
    assert d.price_per_share == 0.0
    assert d.exercise_date == "2027-06-26"
    assert d.expiration_date == "2036-06-26"
    assert d.underlying_security_title == "Common Stock"
    assert d.underlying_shares == 5000.0
    assert d.direct_or_indirect == "D"


def test_parse_form4_returns_none_without_ownership_block() -> None:
    assert parse_form4("<html>not a form 4</html>", _ACCESSION) is None


def test_parse_form4_returns_none_without_issuer_cik() -> None:
    # An ownership block missing the issuer CIK is unusable.
    broken = "<ownershipDocument><reportingOwner></reportingOwner></ownershipDocument>"
    assert parse_form4(broken, _ACCESSION) is None


def test_extract_ownership_xml_slices_block() -> None:
    wrapped = f"<SEC-HEADER>junk</SEC-HEADER>\n{_OWNERSHIP}\ntrailing"
    block = extract_ownership_xml(wrapped)
    assert block is not None
    assert block.startswith("<ownershipDocument>")
    assert block.endswith("</ownershipDocument>")
    assert extract_ownership_xml("no document here") is None


def test_submission_url_builds_archives_url() -> None:
    assert (
        submission_url("edgar/data/123/0001234567-26-000001.txt")
        == "https://www.sec.gov/Archives/edgar/data/123/0001234567-26-000001.txt"
    )
