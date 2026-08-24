"""Tests for the risk materialization tracker (Phase 2b): the realization judge, the
re-check gap query, storage, and the reconciler pass.

The LLM is a fake model returning a scripted tool-call verdict, so no network or Anthropic
config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    RealizationEvent,
    RealizationVerdict,
    build_user_content,
    evidence_is_grounded,
    judge_realization,
    prompt_fingerprint,
    quote_is_grounded,
    realization_evidence_is_grounded,
    realization_is_grounded,
    realization_version,
)
from filings_orchestrator.change_detection.realization import _build_user_prompt
from filings_orchestrator.cli.track_realizations import realization_pass
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    RiskToTrack,
    insert_risk_realization,
    load_subsequent_material_events,
    select_risks_needing_realization,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_SEC = "risk_factors"
_MODEL = "voyage-finance-2"
_JV = "test-judge-v1"
_RV = "test-realization-v1"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


# --- seed helpers ---


def _seed_risk(
    engine: Engine,
    *,
    acc: str,
    cik: str,
    filed_at: str,
    seq: int = 0,
    explanation: str = "A specific risk.",
    risk_text: str = "Deterioration of labor relations, availability or costs could harm us.",
    change_type: str = "added",
) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                # OR IGNORE so one 10-K can carry several risks, as a real one does.
                "INSERT OR IGNORE INTO periodic_filings (accession_number,cik,company_name,"
                "form,filed_at,period_of_report,fiscal_year,parsed,block_count,ingested_at) "
                "VALUES (:a,:k,'ACME','10-K',:f,'2025-12-31',2025,1,0,'t')"
            ),
            {"a": acc, "k": cik, "f": filed_at},
        )
        c.execute(
            text(
                "INSERT INTO filing_blocks (accession_number,section,block_index,heading,"
                "block_text,block_hash) VALUES (:a,:s,:q,'Labor',:t,:hash)"
            ),
            {"a": acc, "s": _SEC, "q": seq, "t": risk_text, "hash": f"{acc}-{seq}"},
        )
        c.execute(
            text(
                "INSERT INTO block_changes (accession_number,section,model_id,change_seq,"
                "change_type,current_block_index,prior_block_index,prior_accession_number,"
                "similarity) VALUES (:a,:s,:m,:q,:ct,:q,NULL,'prior',0.8)"
            ),
            {"a": acc, "s": _SEC, "m": _MODEL, "q": seq, "ct": change_type},
        )
        c.execute(
            text(
                "INSERT INTO block_change_verdicts (accession_number,section,model_id,change_seq,"
                "judge_version,is_material,confidence,category,direction,explanation,"
                "needs_review,judged_at) "
                "VALUES (:a,:s,:m,:q,:jv,1,0.9,'ma_activity','worse',:e,0,'t')"
            ),
            {"a": acc, "s": _SEC, "m": _MODEL, "q": seq, "jv": _JV, "e": explanation},
        )
        c.execute(
            text(
                "INSERT INTO change_specificity (accession_number,section,model_id,change_seq,"
                "judge_version,catalog_version,specificity_version,is_specific,matched_theme,"
                "confidence,classified_at) "
                "VALUES (:a,:s,:m,:q,:jv,'cv1','sv1',1,'',0.9,'t')"
            ),
            {"a": acc, "s": _SEC, "m": _MODEL, "q": seq, "jv": _JV},
        )


def _user_text(content: Any) -> str:
    """The user turn's text, whether sent as one string or as cache-broken content blocks."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b["text"] for b in content)


def _seed_8k(
    engine: Engine,
    *,
    acc: str,
    cik: str,
    filing_date: str,
    event_type: str = "ma_activity",
    summary: str = "Merger agreement announced.",
    item: str = "1.01",
    material: bool = True,
    form: str = "8-K",
) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT OR IGNORE INTO runs (run_id,stage,config_version,taxonomy_version,"
                "status,started_at) VALUES (1,'classify','cv','tv','succeeded','t')"
            )
        )
        c.execute(
            text(
                "INSERT INTO filings (accession_number,cik,ticker,company_name,form,filing_date,"
                "primary_document,primary_document_url,items_json,fetched_at) "
                "VALUES (:a,:k,'ACME','ACME Corp',:form,:d,'d','u','[]','t')"
            ),
            {"a": acc, "k": cik, "form": form, "d": filing_date},
        )
        c.execute(
            text(
                "INSERT INTO events (run_id,accession_number,anchor_item_number,event_type,"
                "event_domain,is_material,confidence,summary) "
                "VALUES (1,:a,:it,:et,'corporate',:mat,0.9,:s)"
            ),
            {"a": acc, "it": item, "et": event_type, "mat": 1 if material else 0, "s": summary},
        )


# --- module: judge + versioning ---


class _RealResponse:
    def __init__(self, args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_realization", "args": args, "id": "t"}]
        self.usage_metadata: dict[str, Any] = {}


class _FakeRealModel:
    def __init__(self, result: RealizationVerdict) -> None:
        self._r = result
        self.last_system: Any = None
        self.last_user: Any = None

    def invoke(self, messages: list[Any]) -> _RealResponse:
        self.last_system = messages[0].content
        self.last_user = messages[-1].content
        return _RealResponse(self._r.model_dump(mode="json"))


class _NoToolModel:
    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(tool_calls=[], usage_metadata={})


def test_realization_version_stable_and_names_model() -> None:
    v = realization_version("claude-x")
    assert v == realization_version("claude-x")
    assert v.startswith("claude-x+realization-")


def test_judge_sees_risk_and_numbered_events() -> None:
    model = _FakeRealModel(
        RealizationVerdict(is_realized=True, event_index=1, evidence="e", confidence=0.9)
    )
    events = [
        RealizationEvent("2026-05-01", "ma_activity", "1.01", "Merger agreement announced."),
        RealizationEvent("2026-06-01", "earnings_release", "", "Q2 results."),
    ]
    judge_realization(
        model,
        risk_text="A merger or acquisition could disrupt operations.",
        risk="Added merger language.",
        events=events,
        model_name="m",
        accession_number="a",
    )
    assert "Risk factor: A merger or acquisition could disrupt operations." in _user_text(
        model.last_user
    )
    assert "What changed this year: Added merger language." in _user_text(model.last_user)
    assert "1. [2026-05-01] ma_activity (item 1.01): Merger agreement announced." in _user_text(
        model.last_user
    )
    assert "2. [2026-06-01] earnings_release: Q2 results." in _user_text(model.last_user)


def test_judge_raises_without_tool_call() -> None:
    with pytest.raises(RuntimeError):
        judge_realization(_NoToolModel(), risk_text="t", risk="r", events=[], model_name="m")


def test_prompt_shows_disclosure_text_only_when_supplied() -> None:
    model = _FakeRealModel(RealizationVerdict(is_realized=False, event_index=None, confidence=0.1))
    events = [
        RealizationEvent(
            "2026-05-01",
            "exec_departure",
            "5.02",
            "CEO left.",
            "Jane Doe resigned as CEO effective today.",
        ),
        RealizationEvent("2026-06-01", "earnings_release", "", "Q2 results."),
    ]
    judge_realization(
        model, risk_text="Key-person risk.", risk="Added.", events=events, model_name="m"
    )
    # Event 1 carries source text -> its disclosure block is shown to quote from; event 2 does not.
    assert "DISCLOSURE TEXT (quote only from here):" in _user_text(model.last_user)
    assert "Jane Doe resigned as CEO effective today." in _user_text(model.last_user)
    assert "2. [2026-06-01] earnings_release: Q2 results." in _user_text(model.last_user)


def test_quote_is_grounded_normalizes_and_rejects_absent() -> None:
    source = (
        "Rafa Oliveira, head of KDP's Coffee Operating Unit, has announced\n"
        "13 Table of Contents\nhis intention to depart."
    )
    # Verbatim span survives whitespace + injected page-break normalization + case.
    assert quote_is_grounded(
        "head of KDP's Coffee Operating Unit, has announced his intention to depart", source
    )
    # A fabricated qualifier not in the source is rejected — the "designated future CEO" class.
    assert not quote_is_grounded("designated future CEO of Global Coffee Co.", source)
    # A too-short span is not grounding.
    assert not quote_is_grounded("depart", source)
    assert not quote_is_grounded("", source)


def test_realization_is_grounded_gate() -> None:
    events = [
        RealizationEvent(
            "2026-06-23",
            "exec_departure",
            "8.01",
            "Oliveira departs.",
            "Rafa Oliveira has announced his intention to depart at the end of July 2026.",
        )
    ]
    grounded = RealizationVerdict(
        is_realized=True,
        event_index=1,
        quote="announced his intention to depart at the end of July 2026",
        confidence=0.9,
    )
    fabricated = RealizationVerdict(
        is_realized=True,
        event_index=1,
        quote="the designated future CEO of Global Coffee Co.",
        confidence=0.9,
    )
    assert realization_is_grounded(grounded, events)
    assert not realization_is_grounded(fabricated, events)
    # A not-realized verdict has nothing to ground; it passes through.
    assert realization_is_grounded(
        RealizationVerdict(is_realized=False, event_index=None, confidence=0.1), events
    )
    # A realized verdict pointing past the candidate list is not grounded.
    assert not realization_is_grounded(
        RealizationVerdict(is_realized=True, event_index=9, quote="x" * 20, confidence=0.9), events
    )


def test_verdict_coercions() -> None:
    v = RealizationVerdict(is_realized=True, event_index="2", evidence="e", confidence="1.5")  # type: ignore[arg-type]
    assert v.event_index == 2 and v.confidence == 1.0
    assert (
        RealizationVerdict(is_realized=False, event_index=None, confidence=0.4).event_index is None
    )


# --- repository: gap, load, insert ---


def test_gap_finds_specific_risk_with_subsequent_8k(engine: Engine) -> None:
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    _seed_8k(engine, acc="eightk", cik="C", filing_date="2026-05-01")
    risks = select_risks_needing_realization(
        engine, judge_version=_JV, realization_version=_RV, limit=10
    )
    assert len(risks) == 1 and risks[0].accession_number == "tenk" and risks[0].cik == "C"


def test_gap_skips_risk_with_no_subsequent_8k(engine: Engine) -> None:
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    # an 8-K filed BEFORE the 10-K does not count
    _seed_8k(engine, acc="old8k", cik="C", filing_date="2026-01-01")
    assert (
        select_risks_needing_realization(
            engine, judge_version=_JV, realization_version=_RV, limit=10
        )
        == []
    )


def test_gap_reopens_not_realized_when_newer_8k_arrives(engine: Engine) -> None:
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01")
    risk = RiskToTrack(
        "tenk", _SEC, _MODEL, 0, "C", "ACME", "2026-02-01", "A specific risk.", "Labor risk."
    )
    # record a not-realized verdict checked through 2026-05-01
    insert_risk_realization(
        engine,
        risk=risk,
        judge_version=_JV,
        realization_version=_RV,
        is_realized=False,
        realizing_accession=None,
        realizing_event_type=None,
        realizing_item=None,
        evidence="",
        quote="",
        confidence=0.7,
        checked_through="2026-05-01",
        judged_at="t",
    )
    assert (
        select_risks_needing_realization(
            engine, judge_version=_JV, realization_version=_RV, limit=10
        )
        == []
    )
    # a newer 8-K re-opens the risk for a re-check
    _seed_8k(engine, acc="e2", cik="C", filing_date="2026-07-01", summary="Merger completed.")
    reopened = select_risks_needing_realization(
        engine, judge_version=_JV, realization_version=_RV, limit=10
    )
    assert len(reopened) == 1 and reopened[0].accession_number == "tenk"


def test_recheck_not_realized_reopens_without_a_newer_filing(engine: Engine) -> None:
    # The escape hatch for a gate change: the events have not moved, but the code that vets the
    # judge's answer has, so the stored not-realized verdict has to be re-derived anyway.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01")
    insert_risk_realization(
        engine,
        risk=RiskToTrack(
            "tenk", _SEC, _MODEL, 0, "C", "ACME", "2026-02-01", "A specific risk.", "Labor risk."
        ),
        judge_version=_JV,
        realization_version=_RV,
        is_realized=False,
        realizing_accession=None,
        realizing_event_type=None,
        realizing_item=None,
        evidence="",
        quote="",
        confidence=0.7,
        checked_through="2026-05-01",
        judged_at="t",
    )
    assert (
        select_risks_needing_realization(
            engine, judge_version=_JV, realization_version=_RV, limit=10
        )
        == []
    )
    reopened = select_risks_needing_realization(
        engine,
        judge_version=_JV,
        realization_version=_RV,
        limit=10,
        recheck_not_realized=True,
    )
    assert len(reopened) == 1 and reopened[0].accession_number == "tenk"


def test_load_subsequent_events_filters(engine: Engine) -> None:
    _seed_8k(engine, acc="after", cik="C", filing_date="2026-05-01", summary="After.")
    _seed_8k(engine, acc="before", cik="C", filing_date="2026-01-01", summary="Before.")
    _seed_8k(
        engine,
        acc="immaterial",
        cik="C",
        filing_date="2026-06-01",
        summary="Immaterial.",
        material=False,
    )
    evs = load_subsequent_material_events(engine, cik="C", after="2026-02-01")
    assert [e.accession_number for e in evs] == ["after"]
    assert evs[0].summary == "After."


def test_insert_round_trip_and_idempotent(engine: Engine) -> None:
    risk = RiskToTrack(
        "tenk", _SEC, _MODEL, 0, "C", "ACME", "2026-02-01", "A specific risk.", "Labor risk."
    )
    for realized in (False, True):
        insert_risk_realization(
            engine,
            risk=risk,
            judge_version=_JV,
            realization_version=_RV,
            is_realized=realized,
            realizing_accession="e1" if realized else None,
            realizing_event_type="ma_activity" if realized else None,
            realizing_item="1.01" if realized else None,
            evidence="merger" if realized else "",
            quote="a merger agreement was signed" if realized else "",
            confidence=0.9,
            checked_through="2026-07-01",
            judged_at="t",
        )
    with engine.begin() as c:
        rows = c.execute(
            text("SELECT is_realized, realizing_accession FROM risk_realizations")
        ).fetchall()
    assert len(rows) == 1 and rows[0][0] == 1 and rows[0][1] == "e1"


# --- pass ---


def test_pass_realizes_a_seeded_risk(engine: Engine) -> None:
    _seed_risk(
        engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Merger intermediary risk."
    )
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary="ICE merger agreement.")
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="ICE merger agreement",
            evidence="ICE merger realizes it.",
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts == {
        "realized": 1,
        "not_realized": 0,
        "failed": 0,
        "candidates": 1,
        "quote_rejected": 0,
        "evidence_rejected": 0,
    }
    with engine.begin() as c:
        row = c.execute(
            text(
                "SELECT is_realized, realizing_accession, realizing_event_type, evidence, "
                "checked_through FROM risk_realizations"
            )
        ).one()
    assert row == (1, "e1", "ma_activity", "ICE merger realizes it.", "2026-05-01")

    # re-run: everything checked, nothing new -> no candidates
    again = realization_pass(
        engine,
        _FakeRealModel(RealizationVerdict(is_realized=False, confidence=0.5)),
        model_name="m",
        judge_ver=_JV,
        realization_ver=_RV,
        limit=10,
    )
    assert again["candidates"] == 0


def test_pass_downgrades_ungrounded_realization(engine: Engine) -> None:
    # A "realized" verdict whose quote is fabricated (not in the event's summary/source) must be
    # downgraded to not-realized with no link stored — the gate against embellishment.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Retention risk.")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary="An executive departed.")
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="the designated future CEO of a newly spun-off company",
            evidence="Fabricated detail not in the source.",
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    # The downgrade is counted, not just folded into not_realized — a gate that starts
    # rejecting true positives has to be visible in the run summary.
    assert counts == {
        "realized": 0,
        "not_realized": 1,
        "failed": 0,
        "candidates": 1,
        "quote_rejected": 1,
        "evidence_rejected": 0,
    }
    # The claim itself is retained for diagnosis (see test_pass_keeps_the_claim_a_gate_refused);
    # what must hold here is that it does not read as realized, so nothing surfaces.
    with engine.begin() as c:
        realized_flag = c.execute(text("SELECT is_realized FROM risk_realizations")).scalar_one()
    assert realized_flag == 0


def test_pass_not_realized_stores_no_link(engine: Engine) -> None:
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary="Unrelated event.")
    # model claims realized but with an out-of-range index -> treated as not realized (strict)
    model = _FakeRealModel(
        RealizationVerdict(is_realized=True, event_index=9, evidence="x", confidence=0.6)
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts["realized"] == 0 and counts["not_realized"] == 1
    with engine.begin() as c:
        row = c.execute(
            text("SELECT is_realized, realizing_accession FROM risk_realizations")
        ).one()
    assert row == (0, None)


# --- evidence grounding ---

# The 8-K text behind the KDP materialization that exposed this gap. It names the role the
# executive actually held and, separately, the role the Board was still searching to fill.
_KDP_DISCLOSURE = (
    "Also on June 23, 2026, the Company announced that Rafa Oliveira, the head of its Coffee "
    "Operating Unit, has informed the Company of his intention to depart at the end of July 2026 "
    "for an external Chief Executive Officer opportunity. Tim Cofer, the Chief Executive Officer "
    "of KDP, will continue to oversee the coffee business, while the Company's Board of Directors "
    "conducts a search for the future CEO of Global Coffee Co., the standalone entity expected to "
    "result from the previously-announced separation of the Company's coffee and beverage "
    "businesses."
)
_KDP_RISK = (
    "RISKS RELATED TO THE SEPARATION. The Separation may not be completed on the terms or "
    "timeline currently contemplated, and may cause disruptions with employees."
)
_KDP_QUOTE = (
    "Rafa Oliveira, the head of its Coffee Operating Unit, has informed the Company of his "
    "intention to depart at the end of July 2026"
)


def test_quote_gate_folds_edgar_typography() -> None:
    # Real refusals from production. EDGAR sets possessives and defined terms with curly
    # punctuation; the model transcribes them as ASCII. The wording is identical, so the
    # citation is genuine and must pass.
    cases = [
        (
            "NVIDIA\u2019s aggregate payment obligation is cumulatively capped at $105 billion.",
            "NVIDIA's aggregate payment obligation is cumulatively capped at $105 billion.",
        ),
        (
            "issued $800,000,000 5.000% Senior Notes due 2028 (the \u201c2028 Notes\u201d)",
            'issued $800,000,000 5.000% Senior Notes due 2028 (the "2028 Notes")',
        ),
        (
            "the Company\u2019s common stock will be subject to NYSE\u2019s suspension",
            "the Company's common stock will be subject to NYSE's suspension",
        ),
        (
            "the Separation \u2014 announced in March \u2014 remains on track",
            "the Separation - announced in March - remains on track",
        ),
    ]
    for source, quoted in cases:
        assert quote_is_grounded(quoted, source), f"should be grounded: {quoted!r}"


def test_quote_gate_still_refuses_an_invented_figure() -> None:
    # The eighth refusal from that same run, and the reason the gate exists. The filing does
    # discuss dispositions and liquidity, so this is semantically plausible — the figures are
    # invented. Folding punctuation must not make it pass.
    source = (
        "The Company completed several dispositions during the quarter and continues to "
        "evaluate its liquidity position in light of upcoming maturities."
    )
    fabricated = (
        "These dispositions are expected to deliver over $80 million in near-term liquidity "
        "as part of a broader $275 million plan"
    )
    assert not quote_is_grounded(fabricated, source)


def test_evidence_rejects_a_title_the_filing_never_gives() -> None:
    # The regression case. Every word of the invented title appears somewhere in the 8-K —
    # "future", "standalone", "coffee", "CEO" — but never as this phrase describing this person.
    # Contiguity is what separates a faithful description from a fabricated composite.
    evidence = (
        "The risk factor warns that the Separation may cause disruptions with employees; the "
        "departure of the head of the Coffee Operating Unit — the future standalone coffee "
        "company's prospective CEO — directly realizes that risk."
    )
    grounded, unsupported = evidence_is_grounded(
        evidence, sources=[_KDP_QUOTE, _KDP_RISK, _KDP_DISCLOSURE]
    )
    assert not grounded
    assert "prospective ceo" in unsupported


def test_evidence_accepts_a_faithful_paraphrase() -> None:
    # The sentence says "head of THE Coffee Operating Unit"; the filing says "head of ITS Coffee
    # Operating Unit". Articles and possessives are not claims, so this must not be rejected.
    evidence = (
        "The risk factor warns that integration could result in losses of personnel; the "
        "departure of the head of the Coffee Operating Unit directly realizes the personnel risk."
    )
    grounded, unsupported = evidence_is_grounded(
        evidence, sources=[_KDP_QUOTE, _KDP_RISK, _KDP_DISCLOSURE]
    )
    assert grounded, unsupported


def test_evidence_accepts_a_category_reference_to_the_risks_subject() -> None:
    # From the gold set (Align). The filing says "key personnel, particularly executive
    # management"; the sentence says "a key executive". Every word is faithful, but the phrase
    # is nowhere contiguous, and the gate used to throw the whole materialization away for it.
    # "a key executive" names the KIND of person the risk is about, not an office, so there is
    # no title for the filing to corroborate.
    risk = (
        "The loss of the services and knowledge of any key personnel, particularly executive "
        "management, research and development, or sales personnel, could harm our business."
    )
    quote = (
        "Julie Coletti, Executive Vice President and Chief Legal and Regulatory Officer, "
        "resigned effective August 1, 2026, to join Illumina as Chief Legal Officer."
    )
    evidence = (
        "The risk factor warns that the loss of key executive personnel could harm the "
        "company's business and prospects; this event discloses that Julie Coletti, Executive "
        "Vice President and Chief Legal and Regulatory Officer, resigned to join a competitor, "
        "which is precisely the adverse consequence of losing a key executive that the risk "
        "factor warns about."
    )
    grounded, unsupported = evidence_is_grounded(evidence, sources=[quote, risk, quote])
    assert grounded, unsupported


def test_evidence_still_rejects_an_inflated_title() -> None:
    # The exemption is only for a significance adjective on a BARE role word. A composite title
    # is still checked in full, so promoting a Vice President to Senior Vice President fails.
    quote = "Dana Reed, Vice President of Logistics, resigned effective June 1, 2026."
    evidence = "Dana Reed, the Senior Vice President of Logistics, departed, realizing the risk."
    grounded, unsupported = evidence_is_grounded(evidence, sources=[quote, "", quote])
    assert not grounded
    assert "senior vice president logistics" in unsupported


def test_evidence_does_not_read_a_form_name_as_a_figure() -> None:
    # Both refusals of this kind in one production run. Every evidence sentence naturally opens
    # "The 8-K discloses...", the tokenizer splits the compound, and a bare "8" was hunted for
    # as an invented figure.
    source = (
        "Chegg received a notice from the NYSE for failing to maintain the minimum average "
        "closing share price of $1.00 over a consecutive 30 trading-day period."
    )
    evidence = (
        "The 8-K discloses that Chegg received a new NYSE non-compliance notice for failing to "
        "maintain the minimum average closing share price of $1.00, directly realizing the risk."
    )
    grounded, unsupported = evidence_is_grounded(evidence, sources=[source, "", source])
    assert grounded, unsupported
    # A 10-Q, 6-K or 20-F reference is the same kind of name and equally not a claim.
    for form in ("10-Q", "6-K", "20-F", "10-K"):
        ok, un = evidence_is_grounded(f"The {form} discloses the departure.", sources=[source])
        assert ok, (form, un)


def test_evidence_tolerates_the_quoters_own_terminal_punctuation() -> None:
    # The filing ends the clause with a period; quoted mid-sentence it takes a comma, which is
    # the convention rather than a misquote. Refused verbatim, this cost a sound verdict.
    source = (
        "Such transactions remain subject to closing conditions, and may be delayed, "
        "restructured or not completed. Even if completed, such transactions could disrupt "
        "operations."
    )
    evidence = (
        'The risk factor warns that announced transactions may be "delayed, restructured or '
        'not completed," and the Gateway Amendment explicitly restructures one.'
    )
    grounded, unsupported = evidence_is_grounded(evidence, sources=["", source, source])
    assert grounded, unsupported


def test_evidence_still_rejects_a_span_the_source_never_contains() -> None:
    # Trimming the edges must not rescue a span whose interior is invented.
    source = "The Company completed the previously announced separation of its coffee business."
    evidence = 'The filing states the separation was "abandoned after a failed vote," realizing it.'
    grounded, unsupported = evidence_is_grounded(evidence, sources=["", source, source])
    assert not grounded
    assert "abandoned after a failed vote" in unsupported


def test_evidence_rejects_invented_figures_and_quoted_spans() -> None:
    with_figure = (
        "The departure of the head of the Coffee Operating Unit, costing $450 million, realizes "
        "the retention risk."
    )
    grounded, unsupported = evidence_is_grounded(
        with_figure, sources=[_KDP_QUOTE, _KDP_RISK, _KDP_DISCLOSURE]
    )
    assert not grounded and "450" in unsupported

    with_quote = (
        'The filing discloses "the wholesale collapse of the coffee unit", realizing the risk.'
    )
    grounded, unsupported = evidence_is_grounded(
        with_quote, sources=[_KDP_QUOTE, _KDP_RISK, _KDP_DISCLOSURE]
    )
    assert not grounded and "the wholesale collapse of the coffee unit" in unsupported


def test_evidence_gate_is_vacuous_without_material() -> None:
    assert evidence_is_grounded("", sources=[_KDP_QUOTE]) == (True, [])
    assert evidence_is_grounded("Anything at all.", sources=[]) == (True, [])
    assert evidence_is_grounded("Anything at all.", sources=["", "   "]) == (True, [])


def test_realization_evidence_gate_follows_the_cited_event() -> None:
    events = [
        RealizationEvent(
            "2026-06-23", "exec_departure", "8.01", "Oliveira departs.", _KDP_DISCLOSURE
        )
    ]
    faithful = RealizationVerdict(
        is_realized=True,
        event_index=1,
        quote=_KDP_QUOTE,
        evidence="The departure of the head of the Coffee Operating Unit realizes the risk.",
        confidence=0.9,
    )
    embellished = RealizationVerdict(
        is_realized=True,
        event_index=1,
        quote=_KDP_QUOTE,
        evidence=(
            "The departure of the head of the Coffee Operating Unit — the future standalone "
            "coffee company's prospective CEO — realizes the risk."
        ),
        confidence=0.9,
    )
    assert realization_evidence_is_grounded(faithful, events, risk_text=_KDP_RISK)[0]
    assert not realization_evidence_is_grounded(embellished, events, risk_text=_KDP_RISK)[0]
    # A not-realized verdict renders no sentence, so there is nothing to ground.
    assert realization_evidence_is_grounded(
        RealizationVerdict(is_realized=False, event_index=None, confidence=0.1),
        events,
        risk_text=_KDP_RISK,
    )[0]


def test_pass_downgrades_ungrounded_evidence(engine: Engine) -> None:
    # End to end: the quote verifies, so the first gate passes — but the sentence the page would
    # render invents a title, so no materialization is surfaced and no link is stored.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Retention risk.")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary=_KDP_DISCLOSURE)
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="has informed the Company of his intention to depart",
            evidence=(
                "The departure of the head of the Coffee Operating Unit — the future standalone "
                "coffee company's prospective CEO — realizes the retention risk."
            ),
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts == {
        "realized": 0,
        "not_realized": 1,
        "failed": 0,
        "candidates": 1,
        "quote_rejected": 0,
        "evidence_rejected": 1,
    }
    # Same contract as the quote gate: the sentence is kept for diagnosis, but the verdict does
    # not read as realized, so the page shows nothing.
    with engine.begin() as c:
        realized_flag = c.execute(text("SELECT is_realized FROM risk_realizations")).scalar_one()
    assert realized_flag == 0


def test_pass_keeps_the_claim_a_gate_refused(engine: Engine) -> None:
    # The refused claim is the only evidence of what the gate turned down. Storing it is what
    # makes an over-strict gate diagnosable instead of merely countable — is_realized stays 0,
    # so nothing reaches the page.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Retention risk.")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary="An executive departed.")
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="a quote that is nowhere in the disclosure",
            evidence="The departure realizes the retention risk.",
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts["quote_rejected"] == 1 and counts["realized"] == 0
    with engine.begin() as c:
        row = c.execute(
            text(
                "SELECT is_realized, rejected_by, rejected_detail, quote, evidence, "
                "realizing_accession FROM risk_realizations"
            )
        ).one()
    is_realized, rejected_by, detail, quote, evidence, realizing = row
    assert is_realized == 0, "a refused claim must never read as realized"
    assert rejected_by == "quote"
    assert quote == "a quote that is nowhere in the disclosure"
    assert evidence == "The departure realizes the retention risk."
    # The filing the quote was checked against, so the check can be reproduced later.
    assert realizing == "e1"
    assert detail == ""


def test_pass_records_what_the_evidence_gate_objected_to(engine: Engine) -> None:
    # The evidence gate does have a reportable objection — the phrases it could not trace — and
    # that is what makes its rejections triageable without re-running the judge.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Retention risk.")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary=_KDP_DISCLOSURE)
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="has informed the Company of his intention to depart",
            evidence=(
                "The departure of the head of the Coffee Operating Unit — the future standalone "
                "coffee company's prospective CEO — realizes the risk."
            ),
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts["evidence_rejected"] == 1 and counts["realized"] == 0
    with engine.begin() as c:
        row = c.execute(
            text("SELECT is_realized, rejected_by, rejected_detail FROM risk_realizations")
        ).one()
    assert row[0] == 0
    assert row[1] == "evidence"
    assert "prospective ceo" in row[2]


def test_pass_marks_no_rejection_when_the_judge_simply_found_nothing(engine: Engine) -> None:
    # The distinction the columns exist to draw: a plain not-realized verdict carries no claim,
    # so there is nothing refused and nothing to store.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01")
    model = _FakeRealModel(RealizationVerdict(is_realized=False, event_index=None, confidence=0.1))
    realization_pass(engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10)
    with engine.begin() as c:
        row = c.execute(
            text(
                "SELECT rejected_by, rejected_detail, quote, evidence, realizing_accession "
                "FROM risk_realizations"
            )
        ).one()
    assert row == (None, "", "", "", None)


def test_pass_stores_the_verified_quote(engine: Engine) -> None:
    # The citation the gate checked is kept, so the rendered claim can be audited against it.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", explanation="Retention risk.")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary=_KDP_DISCLOSURE)
    model = _FakeRealModel(
        RealizationVerdict(
            is_realized=True,
            event_index=1,
            quote="has informed the Company of his intention to depart",
            evidence="The departure of the head of the Coffee Operating Unit realizes the risk.",
            confidence=0.9,
        )
    )
    counts = realization_pass(
        engine, model, model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
    )
    assert counts["realized"] == 1
    with engine.begin() as c:
        quote = c.execute(text("SELECT quote FROM risk_realizations")).scalar_one()
    assert quote == "has informed the Company of his intention to depart"


# --- prompt layout + caching ---


def test_events_lead_the_turn_so_the_shared_half_can_cache() -> None:
    # Caching is a prefix match. The events block is identical for every risk flagged on one
    # 10-K, so it must come FIRST; the per-risk text must trail it. If this inverts, the
    # volatile half becomes the prefix and nothing after it is reusable.
    events = [
        RealizationEvent(
            "2026-06-23", "exec_departure", "8.01", "Oliveira departs.", _KDP_DISCLOSURE
        )
    ]
    prompt = _build_user_prompt(_KDP_RISK, "Separation risk added.", events)
    assert prompt.index("SUBSEQUENT 8-K EVENTS:") < prompt.index("FLAGGED RISK")


def test_user_content_breaks_cache_after_the_shared_block() -> None:
    events = [
        RealizationEvent(
            "2026-06-23", "exec_departure", "8.01", "Oliveira departs.", _KDP_DISCLOSURE
        )
    ]
    blocks = build_user_content(_KDP_RISK, "Separation risk added.", events)
    assert len(blocks) == 2
    shared, volatile = blocks
    assert isinstance(shared, dict) and isinstance(volatile, dict)
    assert shared["text"].startswith("SUBSEQUENT 8-K EVENTS:")
    assert shared["cache_control"] == {"type": "ephemeral"}
    assert volatile["text"].startswith("FLAGGED RISK")
    assert "cache_control" not in volatile
    # The split must not change what the model reads — same bytes, one boundary.
    assert shared["text"] + "\n\n" + volatile["text"] == _build_user_prompt(
        _KDP_RISK, "Separation risk added.", events
    )


def test_solo_risk_sends_no_breakpoint_to_pay_for() -> None:
    # A breakpoint costs a 1.25x write premium on the events block. When this 10-K contributes
    # one risk to the run there is no second call to read the entry, so the turn goes as one
    # block — same bytes, no premium.
    events = [
        RealizationEvent(
            "2026-06-23", "exec_departure", "8.01", "Oliveira departs.", _KDP_DISCLOSURE
        )
    ]
    blocks = build_user_content(
        _KDP_RISK, "Separation risk added.", events, cache_shared_prefix=False
    )
    assert len(blocks) == 1
    only = blocks[0]
    assert isinstance(only, dict) and "cache_control" not in only
    assert only["text"] == _build_user_prompt(_KDP_RISK, "Separation risk added.", events)


def test_pass_breaks_cache_only_for_a_tenk_with_more_than_one_risk(engine: Engine) -> None:
    # Two risks on one 10-K, one on another. The pair is worth a breakpoint; the loner is not.
    _seed_risk(engine, acc="pair", cik="C", filed_at="2026-02-01", seq=0)
    _seed_risk(engine, acc="pair", cik="C", filed_at="2026-02-01", seq=1)
    _seed_risk(engine, acc="solo", cik="D", filed_at="2026-02-01", seq=0)
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01")
    _seed_8k(engine, acc="e2", cik="D", filing_date="2026-05-01")

    seen: dict[str, list[bool]] = {}

    def _capture(**kwargs: object) -> RealizationVerdict:
        acc = str(kwargs["accession_number"])
        seen.setdefault(acc, []).append(bool(kwargs["cache_shared_prefix"]))
        return RealizationVerdict(is_realized=False, event_index=None, confidence=0.1)

    with patch(
        "filings_orchestrator.cli.track_realizations.judge_realization",
        side_effect=lambda _model, **kw: _capture(**kw),
    ):
        realization_pass(
            engine, object(), model_name="m", judge_ver=_JV, realization_ver=_RV, limit=10
        )

    assert seen["pair"] == [True, True]
    assert seen["solo"] == [False]


def test_shared_block_is_identical_across_risks_on_one_tenk() -> None:
    # The cache only pays off if this holds byte-for-byte; events are loaded by cik + the
    # 10-K's filing date, so they do not vary per risk.
    events = [
        RealizationEvent(
            "2026-06-23", "exec_departure", "8.01", "Oliveira departs.", _KDP_DISCLOSURE
        )
    ]
    first = build_user_content("Risk one text.", "Change one.", events)[0]
    second = build_user_content("Risk two text.", "Change two.", events)[0]
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["text"] == second["text"]


def test_version_tracks_the_layout_not_just_the_prompt() -> None:
    # A reordered turn changes the judgment as surely as a reworded prompt; both must version.
    import filings_orchestrator.change_detection.realization as r

    before = prompt_fingerprint()
    original = r._EVENTS_HEADER
    try:
        r._EVENTS_HEADER = "EVENTS:\n"
        assert prompt_fingerprint() != before
    finally:
        r._EVENTS_HEADER = original
    assert prompt_fingerprint() == before


def test_gap_skips_an_edited_standing_factor(engine: Engine) -> None:
    # Only genuinely-new risk factors are tracked. An edited standing factor can be matched to a
    # salient 8-K on language that was already on the books, so the service does not surface it
    # and the tracker does not pay to judge it.
    _seed_risk(engine, acc="tenk", cik="C", filed_at="2026-02-01", change_type="changed")
    _seed_8k(engine, acc="e1", cik="C", filing_date="2026-05-01", summary="An executive departed.")
    assert (
        select_risks_needing_realization(
            engine, judge_version=_JV, realization_version=_RV, limit=10
        )
        == []
    )
