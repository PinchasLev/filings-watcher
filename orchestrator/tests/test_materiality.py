"""Tests for the materiality judge (ADR 0042, PR 5): prompt, verdict, storage, pass.

The LLM is a fake model that returns scripted tool-call verdicts, so no network or
Anthropic config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    BlockChange,
    DiffResult,
    MaterialityVerdict,
    RiskFactorBlock,
    judge_change,
    judge_version,
)
from filings_orchestrator.change_detection.materiality import _build_user_prompt
from filings_orchestrator.cli.judge_changes import judge_pass
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    insert_change_verdict,
    insert_filing_diff,
    insert_periodic_filing,
    select_changes_needing_verdict,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_MODEL = "test-embed-model"
_SECTION = "risk_factors"
_VERSION = "test-judge-v1"


class _ToolResponse:
    def __init__(self, args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_materiality", "args": args, "id": "t"}]
        self.usage_metadata: dict[str, Any] = {}


class _FakeModel:
    """Returns the given verdicts in order, one per invoke; records the last prompt."""

    def __init__(self, verdicts: list[MaterialityVerdict]) -> None:
        self._it = iter(verdicts)
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


def _verdict(material: bool, conf: float) -> MaterialityVerdict:
    return MaterialityVerdict(
        is_material=material, confidence=conf, category="x", explanation="because"
    )


# --- prompt ---


def test_prompt_changed_shows_both_sides() -> None:
    p = _build_user_prompt("changed", current_text="NEW TEXT", prior_text="OLD TEXT")
    assert "PRIOR YEAR" in p and "OLD TEXT" in p
    assert "THIS YEAR" in p and "NEW TEXT" in p


def test_prompt_added_shows_only_current() -> None:
    p = _build_user_prompt("added", current_text="NEW TEXT", prior_text=None)
    assert "NEW" in p and "NEW TEXT" in p
    assert "PRIOR YEAR" not in p


def test_prompt_dropped_shows_only_prior() -> None:
    p = _build_user_prompt("dropped", current_text=None, prior_text="OLD TEXT")
    assert "REMOVED" in p and "OLD TEXT" in p


# --- judge_change ---


def test_judge_change_parses_verdict() -> None:
    model = _FakeModel([_verdict(True, 0.9)])
    v = judge_change(
        model,
        change_type="changed",
        current_text="new",
        prior_text="old",
        model_name="m",
    )
    assert v.is_material is True and v.confidence == 0.9
    assert model.last_user is not None and "old" in model.last_user


def test_judge_change_raises_without_tool_call() -> None:
    with pytest.raises(RuntimeError):
        judge_change(
            _NoToolModel(), change_type="added", current_text="x", prior_text=None, model_name="m"
        )


def test_judge_version_is_stable_and_names_model() -> None:
    v1 = judge_version("claude-x")
    assert v1 == judge_version("claude-x")
    assert v1.startswith("claude-x+materiality-")


# --- persistence: seed a diff, then select/insert verdicts ---


def _seed_diff(engine: Engine) -> None:
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
            RiskFactorBlock(index=0, heading="H", text="old risk", block_hash="a"),
            RiskFactorBlock(index=1, heading="H", text="dropped risk", block_hash="b"),
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
            RiskFactorBlock(index=0, heading="H", text="changed risk", block_hash="c"),
            RiskFactorBlock(index=1, heading="H", text="added risk", block_hash="d"),
        ],
        ingested_at="t",
    )
    result = DiffResult(
        changes=[
            BlockChange("changed", 0, 0, 0.8),
            BlockChange("added", 1, None, 0.3),
            BlockChange("dropped", None, 1, 0.2),
        ],
        added=1,
        changed=1,
        carried=0,
        dropped=1,
    )
    insert_filing_diff(
        engine,
        accession_number="current",
        prior_accession_number="prior",
        section=_SECTION,
        model_id=_MODEL,
        result=result,
        computed_at="t",
    )


def test_select_changes_joins_both_sides_text(engine: Engine) -> None:
    _seed_diff(engine)
    changes = select_changes_needing_verdict(engine, _VERSION, limit=10)
    by_type = {c.change_type: c for c in changes}
    assert by_type["changed"].current_text == "changed risk"
    assert by_type["changed"].prior_text == "old risk"
    assert by_type["added"].current_text == "added risk"
    assert by_type["added"].prior_text is None
    assert by_type["dropped"].current_text is None
    assert by_type["dropped"].prior_text == "dropped risk"


def test_insert_verdict_round_trip_and_idempotent(engine: Engine) -> None:
    _seed_diff(engine)
    change = select_changes_needing_verdict(engine, _VERSION, limit=1)[0]
    insert_change_verdict(
        engine,
        change=change,
        judge_version=_VERSION,
        verdict=_verdict(True, 0.9),
        needs_review=False,
        judged_at="t1",
    )
    insert_change_verdict(
        engine,
        change=change,
        judge_version=_VERSION,
        verdict=_verdict(False, 0.4),
        needs_review=True,
        judged_at="t2",
    )
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT is_material, needs_review FROM block_change_verdicts")
        ).fetchall()
    assert len(rows) == 1  # overwrote under the same version, no duplicate
    assert rows[0] == (0, 1)


# --- judge_pass ---


def test_judge_pass_stores_verdicts_and_derives_review(engine: Engine) -> None:
    _seed_diff(engine)
    # ordered by change_seq: 0 changed, 1 added, 2 dropped
    model = _FakeModel([_verdict(True, 0.9), _verdict(True, 0.5), _verdict(False, 0.8)])
    counts = judge_pass(engine, model, model_name="m", version=_VERSION, limit=10, review_below=0.6)
    assert counts == {
        "judged": 3,
        "material": 2,
        "needs_review": 1,  # the 0.5-confidence one
        "failed": 0,
        "candidates": 3,
    }
    # Second pass: everything judged for this version -> nothing to do.
    again = judge_pass(
        engine, _FakeModel([]), model_name="m", version=_VERSION, limit=10, review_below=0.6
    )
    assert again["candidates"] == 0
