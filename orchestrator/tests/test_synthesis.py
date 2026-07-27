"""Tests for the disclosure-change synthesis (ADR 0043, PR 2): the reduce (headline
direction + thesis + top-effects), storage, and the reconciler pass.

The LLM is a fake model returning scripted tool-call syntheses, so no network or
Anthropic config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    BlockChange,
    DiffResult,
    DisclosureSynthesis,
    Finding,
    HeadlineDirection,
    HeadlineIntensity,
    MaterialityVerdict,
    RiskChangeCategory,
    RiskChangeDirection,
    RiskFactorBlock,
    synthesis_version,
    synthesize,
)
from filings_orchestrator.cli.synthesize_changes import synthesis_pass
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    SynthesisTarget,
    insert_change_synthesis,
    insert_change_verdict,
    insert_filing_diff,
    insert_periodic_filing,
    load_material_verdicts,
    select_changes_needing_verdict,
    select_filings_needing_synthesis,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_MODEL = "test-embed-model"
_SECTION = "risk_factors"
_JUDGE_V = "test-judge-v1"
_SYNTH_V = "test-synth-v1"


class _ToolResponse:
    def __init__(self, args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_synthesis", "args": args, "id": "t"}]
        self.usage_metadata: dict[str, Any] = {}


class _FakeModel:
    """Returns the given syntheses in order, one per invoke; records the last prompt."""

    def __init__(self, results: list[DisclosureSynthesis]) -> None:
        self._it = iter(results)
        self.last_user: str | None = None

    def invoke(self, messages: list[Any]) -> _ToolResponse:
        self.last_user = messages[-1].content
        return _ToolResponse(next(self._it).model_dump(mode="json"))


class _NoToolModel:
    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(tool_calls=[], usage_metadata={})


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


# --- reduce ---


def _synth(
    direction: HeadlineDirection = HeadlineDirection.WORSENING,
    intensity: HeadlineIntensity = HeadlineIntensity.MAJOR,
    thesis: str = "It got worse.",
    effects: list[str] | None = None,
) -> DisclosureSynthesis:
    return DisclosureSynthesis(
        headline_direction=direction,
        headline_intensity=intensity,
        thesis=thesis,
        top_effects=effects or ["impairment", "layoffs"],
    )


def test_synthesize_parses_and_renders_findings() -> None:
    model = _FakeModel([_synth()])
    findings = [
        Finding(
            RiskChangeCategory.LIQUIDITY_GOING_CONCERN, RiskChangeDirection.WORSE, "cash short"
        ),
        Finding(RiskChangeCategory.RESTRUCTURING_WORKFORCE, RiskChangeDirection.WORSE, "layoffs"),
    ]
    result = synthesize(model, findings=findings, model_name="m", accession_number="a")
    assert result.headline_direction is HeadlineDirection.WORSENING
    assert result.headline_intensity is HeadlineIntensity.MAJOR
    assert result.thesis == "It got worse."
    assert result.top_effects == ["impairment", "layoffs"]
    # the distilled findings (theme | direction | explanation) are what the reduce sees
    assert model.last_user is not None
    assert "liquidity_going_concern | worse" in model.last_user and "cash short" in model.last_user


def test_headline_axes_coerce_unknown_values() -> None:
    v = DisclosureSynthesis(
        headline_direction="worsening-ish",
        headline_intensity="catastrophic",
        thesis="t",
        top_effects=["a"],
    )
    assert v.headline_direction is HeadlineDirection.MIXED  # unknown direction -> mixed
    assert v.headline_intensity is HeadlineIntensity.MODERATE  # unknown intensity -> moderate


def test_top_effects_salvaged_from_item_markup() -> None:
    # The model sometimes returns the array as an <item>-tagged string (even leaking a
    # stray tool token) instead of a JSON list; salvage it rather than fail the verdict.
    raw = "\n<item>NYSE delisting notice</item>\n<item>Workforce cut to 56%</item>\n</invoke>"
    v = DisclosureSynthesis(
        headline_direction="worsening", headline_intensity="major", thesis="t", top_effects=raw
    )
    assert v.top_effects == ["NYSE delisting notice", "Workforce cut to 56%"]


def test_top_effects_salvaged_from_newline_string() -> None:
    v = DisclosureSynthesis(
        headline_direction="worsening",
        headline_intensity="minor",
        thesis="t",
        top_effects="- first effect\n- second effect",
    )
    assert v.top_effects == ["first effect", "second effect"]


def test_synthesize_raises_without_tool_call() -> None:
    with pytest.raises(RuntimeError):
        synthesize(_NoToolModel(), findings=[], model_name="m")


def test_synthesis_version_is_stable_and_names_model() -> None:
    v1 = synthesis_version("claude-x")
    assert v1 == synthesis_version("claude-x")
    assert v1.startswith("claude-x+synthesis-")


# --- persistence: seed a diff + verdicts, then gap/load/insert ---


def _seed_verdicts(engine: Engine, directions: list[str], *, material: bool = True) -> None:
    """Seed one filing pair with a diff and one verdict per given direction."""
    insert_periodic_filing(
        engine,
        accession_number="prior",
        cik="C",
        company_name="ACME",
        form="10-K",
        filed_at="2026-01-01",
        period_of_report="2024-12-31",
        fiscal_year=2024,
        parsed=True,
        blocks=[
            RiskFactorBlock(index=i, heading="H", text=f"old {i}", block_hash=f"p{i}")
            for i in range(len(directions))
        ],
        ingested_at="t",
    )
    insert_periodic_filing(
        engine,
        accession_number="current",
        cik="C",
        company_name="ACME",
        form="10-K",
        filed_at="2026-01-01",
        period_of_report="2025-12-31",
        fiscal_year=2025,
        parsed=True,
        blocks=[
            RiskFactorBlock(index=i, heading="H", text=f"new {i}", block_hash=f"c{i}")
            for i in range(len(directions))
        ],
        ingested_at="t",
    )
    changes = [BlockChange("changed", i, i, 0.8) for i in range(len(directions))]
    insert_filing_diff(
        engine,
        accession_number="current",
        prior_accession_number="prior",
        section=_SECTION,
        model_id=_MODEL,
        result=DiffResult(changes=changes, added=0, changed=len(directions), carried=0, dropped=0),
        computed_at="t",
    )
    pending = select_changes_needing_verdict(engine, _JUDGE_V, limit=100)
    for change, direction in zip(pending, directions, strict=True):
        insert_change_verdict(
            engine,
            change=change,
            judge_version=_JUDGE_V,
            verdict=MaterialityVerdict(
                is_material=material,
                confidence=0.9,
                category=RiskChangeCategory.RESTRUCTURING_WORKFORCE,
                direction=RiskChangeDirection(direction),
                explanation=f"{direction} change",
            ),
            needs_review=False,
            judged_at="t",
        )


def test_gap_query_finds_filing_with_material_verdicts(engine: Engine) -> None:
    _seed_verdicts(engine, ["worse", "worse", "eased"])
    targets = select_filings_needing_synthesis(engine, _JUDGE_V, _SYNTH_V, limit=10)
    assert len(targets) == 1
    assert targets[0] == SynthesisTarget("current", _SECTION, _MODEL)


def test_gap_query_ignores_filing_with_only_immaterial_verdicts(engine: Engine) -> None:
    _seed_verdicts(engine, ["neutral", "neutral"], material=False)
    assert select_filings_needing_synthesis(engine, _JUDGE_V, _SYNTH_V, limit=10) == []


def test_load_material_verdicts_returns_directions(engine: Engine) -> None:
    _seed_verdicts(engine, ["worse", "worse", "eased"])
    verdicts = load_material_verdicts(
        engine,
        accession_number="current",
        section=_SECTION,
        model_id=_MODEL,
        judge_version=_JUDGE_V,
    )
    assert len(verdicts) == 3
    assert sorted(v.direction for v in verdicts) == ["eased", "worse", "worse"]


def test_insert_synthesis_round_trip_and_idempotent(engine: Engine) -> None:
    target = SynthesisTarget("current", _SECTION, _MODEL)
    for thesis in ("first", "second"):
        insert_change_synthesis(
            engine,
            target=target,
            judge_version=_JUDGE_V,
            synthesis_version=_SYNTH_V,
            headline_direction="worsening",
            headline_intensity="major",
            material_count=3,
            worse_count=2,
            eased_count=1,
            neutral_count=0,
            thesis=thesis,
            top_effects=["a", "b"],
            synthesized_at="t",
        )
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT thesis, top_effects, worse_count FROM filing_change_synthesis")
        ).fetchall()
    assert len(rows) == 1  # overwrote under the same versions, no duplicate
    assert rows[0][0] == "second"
    assert json.loads(rows[0][1]) == ["a", "b"]
    assert rows[0][2] == 2


# --- reconciler pass ---


def test_synthesis_pass_stores_model_headline_and_code_counts(engine: Engine) -> None:
    _seed_verdicts(engine, ["worse", "worse", "worse", "worse", "eased"])
    # The model deliberately returns MINOR intensity though five material changes lean
    # worse — proving the stored headline is the model's holistic judgment (severity, not
    # volume), while the counts are code's.
    model = _FakeModel(
        [_synth(direction=HeadlineDirection.WORSENING, intensity=HeadlineIntensity.MINOR)]
    )
    counts = synthesis_pass(
        engine, model, model_name="m", judge_ver=_JUDGE_V, synth_ver=_SYNTH_V, limit=10
    )
    assert counts == {"synthesized": 1, "failed": 0, "candidates": 1}
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT headline_direction, headline_intensity, material_count, worse_count, "
                "eased_count FROM filing_change_synthesis"
            )
        ).one()
    # headline (direction + intensity) = the model's judgment; counts = code-rolled
    assert row == ("worsening", "minor", 5, 4, 1)

    # Second pass: everything synthesized for these versions -> nothing to do.
    again = synthesis_pass(
        engine, _FakeModel([]), model_name="m", judge_ver=_JUDGE_V, synth_ver=_SYNTH_V, limit=10
    )
    assert again["candidates"] == 0
