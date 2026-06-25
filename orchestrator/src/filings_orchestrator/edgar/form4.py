"""EDGAR Form 4 (Section 16 insider transaction) parser.

A Form 4 is filed as a structured SEC `ownershipDocument` XML embedded in the
filing's full-submission `.txt`. Extraction is therefore deterministic parsing —
no LLM (the bounded-operator split: code parses the facts; the LLM only ever
contextualizes against the already-classified event layer, in a later join).

The XML wraps "footnotable" leaf values in a `<value>` element
(`<transactionShares><value>1000</value></transactionShares>`) while plain leaves
hold text directly (`<transactionCode>P</transactionCode>`); `_text` handles both.
Confirmed against real filings 2026-06-25. Non-derivative transactions only; the
derivative (option) table is deferred.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from pydantic import BaseModel

from filings_orchestrator.edgar.client import EdgarClient

_ARCHIVES_BASE = "https://www.sec.gov/Archives/"

_OWNERSHIP_OPEN = "<ownershipDocument>"
_OWNERSHIP_CLOSE = "</ownershipDocument>"


class InsiderTransaction(BaseModel):
    """One non-derivative transaction line from a Form 4."""

    txn_seq: int
    security_title: str | None
    transaction_date: str | None
    transaction_code: str | None  # P=open-market buy, S=open-market sell, A=grant, etc.
    acquired_disposed: str | None  # "A" or "D"
    shares: float | None
    price_per_share: float | None
    shares_owned_following: float | None
    direct_or_indirect: str | None  # "D" or "I"


class Form4Filing(BaseModel):
    """A parsed Form 4: issuer, reporting owner + role, and its transactions."""

    accession_number: str
    period_of_report: str | None
    issuer_cik: str
    issuer_name: str
    issuer_ticker: str | None
    owner_cik: str
    owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str | None
    is_10b5_1: bool
    not_subject_to_section16: bool
    transactions: list[InsiderTransaction]


def submission_url(submission_path: str) -> str:
    """Full URL for a daily-index `submission_path` (e.g. edgar/data/.../X.txt)."""
    return _ARCHIVES_BASE + submission_path.lstrip("/")


def fetch_form4_submission(client: EdgarClient, submission_path: str) -> str:
    """Fetch a Form 4's full-submission .txt (contains the ownershipDocument XML)."""
    return client.get_text(submission_url(submission_path))


def extract_ownership_xml(submission_text: str) -> str | None:
    """Slice the `<ownershipDocument>...</ownershipDocument>` block out of the
    full-submission text. Returns None if not present (e.g. a non-XML legacy Form 4)."""
    start = submission_text.find(_OWNERSHIP_OPEN)
    end = submission_text.find(_OWNERSHIP_CLOSE)
    if start == -1 or end == -1 or end < start:
        return None
    return submission_text[start : end + len(_OWNERSHIP_CLOSE)]


def _text(parent: ET.Element | None, path: str) -> str | None:
    """Text of `path` under `parent`, unwrapping a `<value>` child if present."""
    if parent is None:
        return None
    el = parent.find(path)
    if el is None:
        return None
    nested = el.findtext("value")
    raw = nested if nested is not None else el.text
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _flag(parent: ET.Element | None, path: str) -> bool:
    return (_text(parent, path) or "").lower() in {"1", "true"}


def _num(parent: ET.Element | None, path: str) -> float | None:
    raw = _text(parent, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _padded_cik(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return f"{int(raw):010d}"
    except ValueError:
        return None


def parse_form4(submission_text: str, accession_number: str) -> Form4Filing | None:
    """Parse a Form 4 full-submission into a Form4Filing.

    Returns None for anything we can't read as a Section 16 ownership document
    (missing XML block, unparseable XML, or no issuer/owner CIK) — the caller
    skips and continues, since one bad filing must not abort the ingest tick.
    """
    xml = extract_ownership_xml(submission_text)
    if xml is None:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    owner_id = owner.find("reportingOwnerId") if owner is not None else None
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None

    issuer_cik = _padded_cik(_text(issuer, "issuerCik"))
    owner_cik = _padded_cik(_text(owner_id, "rptOwnerCik"))
    if issuer_cik is None or owner_cik is None:
        return None

    transactions: list[InsiderTransaction] = []
    table = root.find("nonDerivativeTable")
    if table is not None:
        for seq, txn in enumerate(table.findall("nonDerivativeTransaction")):
            amounts = txn.find("transactionAmounts")
            post = txn.find("postTransactionAmounts")
            nature = txn.find("ownershipNature")
            transactions.append(
                InsiderTransaction(
                    txn_seq=seq,
                    security_title=_text(txn, "securityTitle"),
                    transaction_date=_text(txn, "transactionDate"),
                    transaction_code=_text(txn, "transactionCoding/transactionCode"),
                    acquired_disposed=_text(amounts, "transactionAcquiredDisposedCode"),
                    shares=_num(amounts, "transactionShares"),
                    price_per_share=_num(amounts, "transactionPricePerShare"),
                    shares_owned_following=_num(post, "sharesOwnedFollowingTransaction"),
                    direct_or_indirect=_text(nature, "directOrIndirectOwnership"),
                )
            )

    return Form4Filing(
        accession_number=accession_number,
        period_of_report=_text(root, "periodOfReport"),
        issuer_cik=issuer_cik,
        issuer_name=_text(issuer, "issuerName") or "",
        issuer_ticker=_text(issuer, "issuerTradingSymbol"),
        owner_cik=owner_cik,
        owner_name=_text(owner_id, "rptOwnerName") or "",
        is_director=_flag(relationship, "isDirector"),
        is_officer=_flag(relationship, "isOfficer"),
        is_ten_percent_owner=_flag(relationship, "isTenPercentOwner"),
        is_other=_flag(relationship, "isOther"),
        officer_title=_text(relationship, "officerTitle"),
        is_10b5_1=_flag(root, "aff10b5One"),
        not_subject_to_section16=_flag(root, "notSubjectToSection16"),
        transactions=transactions,
    )
