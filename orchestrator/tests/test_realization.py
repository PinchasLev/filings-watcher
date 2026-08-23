"""Tests for the risk materialization tracker (Phase 2b): the realization judge, the
re-check gap query, storage, and the reconciler pass.

The LLM is a fake model returning a scripted tool-call verdict, so no network or Anthropic
config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    RealizationEvent,
    RealizationVerdict,
    evidence_is_grounded,
    judge_realization,
    quote_is_grounded,
    realization_evidence_is_grounded,
    realization_is_grounded,
    realization_version,
)
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
) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO periodic_filings (accession_number,cik,company_name,form,filed_at,"
                "period_of_report,fiscal_year,parsed,block_count,ingested_at) "
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
                "similarity) VALUES (:a,:s,:m,:q,'changed',:q,NULL,'prior',0.8)"
            ),
            {"a": acc, "s": _SEC, "m": _MODEL, "q": seq},
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
    assert "Risk factor: A merger or acquisition could disrupt operations." in model.last_user
    assert "What changed this year: Added merger language." in model.last_user
    assert "1. [2026-05-01] ma_activity (item 1.01): Merger agreement announced." in model.last_user
    assert "2. [2026-06-01] earnings_release: Q2 results." in model.last_user


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
    assert "DISCLOSURE TEXT (quote only from here):" in model.last_user
    assert "Jane Doe resigned as CEO effective today." in model.last_user
    assert "2. [2026-06-01] earnings_release: Q2 results." in model.last_user


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
    assert counts == {"realized": 1, "not_realized": 0, "failed": 0, "candidates": 1}
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
    assert counts == {"realized": 0, "not_realized": 1, "failed": 0, "candidates": 1}
    with engine.begin() as c:
        row = c.execute(
            text("SELECT is_realized, realizing_accession FROM risk_realizations")
        ).one()
    assert row == (0, None)


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
    assert counts == {"realized": 0, "not_realized": 1, "failed": 0, "candidates": 1}
    with engine.begin() as c:
        row = c.execute(
            text("SELECT is_realized, realizing_accession, evidence, quote FROM risk_realizations")
        ).one()
    assert row == (0, None, "", "")


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
