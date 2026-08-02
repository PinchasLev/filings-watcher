"""Tests for the specificity classifier (calibration, PR2): the per-change specific-vs-
boilerplate verdict, its versioning, sanitation, storage, and the reconciler pass.

The LLM is a fake model returning a scripted tool-call batch, so no network or Anthropic
config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    BlockChange,
    ChangeSpecificity,
    DiffResult,
    MaterialityVerdict,
    RiskChangeCategory,
    RiskChangeDirection,
    RiskFactorBlock,
    SpecificityBatch,
    SpecificityInput,
    classify_specificity,
    render_specificity_system_prompt,
    sanitize_batch,
    specificity_version,
)
from filings_orchestrator.cli.classify_specificity import specificity_pass
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    SynthesisTarget,
    insert_change_specificity,
    insert_change_verdict,
    insert_disclosure_catalog,
    insert_filing_diff,
    insert_periodic_filing,
    load_material_changes_for_specificity,
    select_changes_needing_verdict,
    select_filings_needing_specificity,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_MODEL = "test-embed-model"
_SECTION = "risk_factors"
_JUDGE_V = "test-judge-v1"
_CATALOG_V = "test-catalog-v1"
_SPEC_V = "test-spec-v1"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


# --- module: versioning, prompt, sanitation ---


def test_specificity_version_stable_and_names_model() -> None:
    v = specificity_version("claude-x")
    assert v == specificity_version("claude-x")
    assert v.startswith("claude-x+specificity-")


def test_render_system_prompt_includes_catalog() -> None:
    prompt = render_specificity_system_prompt([("tariffs_trade_policy", "Tariffs raise costs.")])
    assert "tariffs_trade_policy: Tariffs raise costs." in prompt
    assert "COMMON-MODE THEME CATALOG" in prompt


class _SpecResponse:
    def __init__(self, args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_specificity", "args": args, "id": "t"}]
        self.usage_metadata: dict[str, Any] = {}


class _FakeSpecModel:
    """Returns the same batch on every invoke; records the last system + user prompts."""

    def __init__(self, result: SpecificityBatch) -> None:
        self._result = result
        self.last_system: Any = None
        self.last_user: Any = None

    def invoke(self, messages: list[Any]) -> _SpecResponse:
        self.last_system = messages[0].content
        self.last_user = messages[-1].content
        return _SpecResponse(self._result.model_dump(mode="json"))


class _NoToolModel:
    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(tool_calls=[], usage_metadata={})


def _verdict(idx: int, specific: bool, theme: str | None = None) -> ChangeSpecificity:
    return ChangeSpecificity(
        change_index=idx, is_specific=specific, matched_theme=theme, confidence=0.9, explanation="x"
    )


def test_classify_sends_catalog_and_numbered_changes() -> None:
    model = _FakeSpecModel(SpecificityBatch(verdicts=[_verdict(1, True)]))
    sp = render_specificity_system_prompt([("tariffs_trade_policy", "arch")])
    changes = [
        SpecificityInput("macro_geopolitical", "tariff exposure"),
        SpecificityInput("ma", "merger"),
    ]
    classify_specificity(
        model, system_prompt=sp, changes=changes, model_name="m", accession_number="a"
    )
    # catalog rides in the cached system block; changes are numbered in the user prompt
    assert "tariffs_trade_policy" in model.last_system[0]["text"]
    assert "1. [macro_geopolitical] tariff exposure" in model.last_user
    assert "2. [ma] merger" in model.last_user


def test_classify_raises_without_tool_call() -> None:
    with pytest.raises(RuntimeError):
        classify_specificity(
            _NoToolModel(), system_prompt="s", changes=[SpecificityInput("c", "e")], model_name="m"
        )


def test_field_coercions() -> None:
    v = ChangeSpecificity(
        change_index="2",  # type: ignore[arg-type]
        is_specific=False,
        matched_theme="Tariffs / Trade Policy",  # type: ignore[arg-type]
        confidence="1.4",  # type: ignore[arg-type]
        explanation="e",
    )
    assert v.change_index == 2
    assert v.matched_theme == "tariffs_trade_policy"
    assert v.confidence == 1.0  # clamped


def test_malformed_batch_coerces_to_empty() -> None:
    assert SpecificityBatch(verdicts="nope").verdicts == []  # type: ignore[arg-type]


def test_sanitize_drops_out_of_range_dupes_and_unknown_theme() -> None:
    batch = SpecificityBatch(
        verdicts=[
            _verdict(1, True),
            _verdict(2, False, "tariffs_trade_policy"),
            _verdict(3, False, "not_a_catalog_slug"),
            _verdict(5, True),  # out of range (n=3)
            _verdict(1, False, "tariffs_trade_policy"),  # duplicate index -> first wins
        ]
    )
    out = sanitize_batch(batch, n_changes=3, allowed_slugs={"tariffs_trade_policy"})
    assert set(out) == {1, 2, 3}  # index 5 dropped
    assert out[1].is_specific is True  # first-wins on the duplicate
    assert out[2].matched_theme == "tariffs_trade_policy"
    assert out[3].matched_theme is None  # unknown slug nulled


# --- persistence + pass ---


def _seed_company(
    engine: Engine,
    *,
    verdicts: list[tuple[RiskChangeCategory, str, str, bool]],
) -> None:
    """Seed one company's filing pair with a diff and one verdict per entry
    (category, direction, explanation, is_material), judging its pending changes."""
    n = len(verdicts)
    for accession, period, fy in (("prior", "2024-12-31", 2024), ("current", "2025-12-31", 2025)):
        insert_periodic_filing(
            engine,
            accession_number=accession,
            cik="C",
            company_name="ACME",
            form="10-K",
            filed_at="2026-01-01",
            period_of_report=period,
            fiscal_year=fy,
            parsed=True,
            blocks=[
                RiskFactorBlock(
                    index=i, heading="H", text=f"{accession} {i}", block_hash=f"{accession}{i}"
                )
                for i in range(n)
            ],
            ingested_at="t",
        )
    insert_filing_diff(
        engine,
        accession_number="current",
        prior_accession_number="prior",
        section=_SECTION,
        model_id=_MODEL,
        result=DiffResult(
            changes=[BlockChange("changed", i, i, 0.8) for i in range(n)],
            added=0,
            changed=n,
            carried=0,
            dropped=0,
        ),
        computed_at="t",
    )
    pending = select_changes_needing_verdict(engine, _JUDGE_V, limit=100)
    for change, (category, direction, explanation, material) in zip(pending, verdicts, strict=True):
        insert_change_verdict(
            engine,
            change=change,
            judge_version=_JUDGE_V,
            verdict=MaterialityVerdict(
                is_material=material,
                confidence=0.9,
                category=category,
                direction=RiskChangeDirection(direction),
                explanation=explanation,
            ),
            needs_review=False,
            judged_at="t",
        )


def _seed_catalog(engine: Engine) -> None:
    insert_disclosure_catalog(
        engine,
        catalog_version=_CATALOG_V,
        model_id="m",
        corpus_label="all",
        content_hash="h",
        themes=[("tariffs_trade_policy", "Tariffs raise costs.", 40)],
        cut_at="t",
    )


def test_gap_finds_unclassified_and_ignores_classified(engine: Engine) -> None:
    _seed_company(
        engine,
        verdicts=[
            (RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "merger", True),
            (RiskChangeCategory.LIQUIDITY_GOING_CONCERN, "neutral", "boilerplate", False),
        ],
    )
    target = SynthesisTarget("current", _SECTION, _MODEL)
    assert select_filings_needing_specificity(engine, _JUDGE_V, _CATALOG_V, _SPEC_V, 10) == [target]
    # classify the one material change -> the filing drops out of the gap
    insert_change_specificity(
        engine,
        target=target,
        judge_version=_JUDGE_V,
        catalog_version=_CATALOG_V,
        specificity_version=_SPEC_V,
        verdicts=[(0, True, None, 0.9, "specific merger")],
        classified_at="t",
    )
    assert select_filings_needing_specificity(engine, _JUDGE_V, _CATALOG_V, _SPEC_V, 10) == []
    # a different catalog re-opens the gap
    assert select_filings_needing_specificity(engine, _JUDGE_V, "other-catalog", _SPEC_V, 10) == [
        target
    ]


def test_load_material_changes_only_material(engine: Engine) -> None:
    _seed_company(
        engine,
        verdicts=[
            (RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "layoffs", True),
            (RiskChangeCategory.LIQUIDITY_GOING_CONCERN, "neutral", "reworded", False),
        ],
    )
    changes = load_material_changes_for_specificity(
        engine,
        accession_number="current",
        section=_SECTION,
        model_id=_MODEL,
        judge_version=_JUDGE_V,
    )
    assert len(changes) == 1
    assert changes[0].explanation == "layoffs"


def test_insert_round_trip_and_idempotent(engine: Engine) -> None:
    _seed_company(
        engine, verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "merger", True)]
    )
    target = SynthesisTarget("current", _SECTION, _MODEL)
    for specific in (True, False):  # second write overwrites under the same versions
        insert_change_specificity(
            engine,
            target=target,
            judge_version=_JUDGE_V,
            catalog_version=_CATALOG_V,
            specificity_version=_SPEC_V,
            verdicts=[(0, specific, None if specific else "tariffs_trade_policy", 0.8, "e")],
            classified_at="t",
        )
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT is_specific, matched_theme FROM change_specificity")
        ).fetchall()
    assert len(rows) == 1  # overwrote, no duplicate
    assert rows[0][0] == 0 and rows[0][1] == "tariffs_trade_policy"


def test_specificity_pass_classifies_and_stores(engine: Engine) -> None:
    _seed_catalog(engine)
    _seed_company(
        engine,
        verdicts=[
            (RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "Norfolk Southern merger", True),
            (RiskChangeCategory.MACRO_GEOPOLITICAL, "worse", "generic tariff exposure", True),
        ],
    )
    model = _FakeSpecModel(
        SpecificityBatch(verdicts=[_verdict(1, True), _verdict(2, False, "tariffs_trade_policy")])
    )
    counts = specificity_pass(
        engine,
        model,
        system_prompt=render_specificity_system_prompt([("tariffs_trade_policy", "arch")]),
        allowed_slugs={"tariffs_trade_policy"},
        model_name="m",
        judge_ver=_JUDGE_V,
        catalog_ver=_CATALOG_V,
        spec_ver=_SPEC_V,
        limit=10,
    )
    assert counts == {"classified": 1, "failed": 0, "candidates": 1}
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT change_seq, is_specific, matched_theme "
                "FROM change_specificity ORDER BY change_seq"
            )
        ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(0, 1, None), (1, 0, "tariffs_trade_policy")]

    # re-run: everything classified for these versions -> nothing to do
    again = specificity_pass(
        engine,
        _FakeSpecModel(SpecificityBatch(verdicts=[])),
        system_prompt="s",
        allowed_slugs=set(),
        model_name="m",
        judge_ver=_JUDGE_V,
        catalog_ver=_CATALOG_V,
        spec_ver=_SPEC_V,
        limit=10,
    )
    assert again["candidates"] == 0


def test_specificity_pass_empty_batch_leaves_filing_for_retry(engine: Engine) -> None:
    _seed_catalog(engine)
    _seed_company(
        engine, verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "merger", True)]
    )
    model = _FakeSpecModel(SpecificityBatch(verdicts=[]))  # model returned nothing mappable
    counts = specificity_pass(
        engine,
        model,
        system_prompt="s",
        allowed_slugs=set(),
        model_name="m",
        judge_ver=_JUDGE_V,
        catalog_ver=_CATALOG_V,
        spec_ver=_SPEC_V,
        limit=10,
    )
    assert counts == {"classified": 0, "failed": 1, "candidates": 1}
    # still in the gap, to retry next run
    assert len(select_filings_needing_specificity(engine, _JUDGE_V, _CATALOG_V, _SPEC_V, 10)) == 1
