"""Tests for the cost-observability surface (ADR 0029).

Covers the pricing math, the module-level sink contract, and the db sink's
DB-write + structured-event emission. The pre-tick cap check itself is
exercised in test_scan_daily_index_cli.py against the live CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from filings_orchestrator.cost import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICING_USD_PER_MILLION_TOKENS,
    LLMCallObservation,
    clear_cost_sink,
    db_llm_call_sink,
    emit_llm_call,
    estimate_cost_usd,
    set_cost_sink,
)
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import daily_cost_usd, daily_token_usage

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_engine():
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _fake_response(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> SimpleNamespace:
    """Build a stand-in for LangChain's AIMessage with usage_metadata."""
    return SimpleNamespace(
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_token_details": {
                "cache_read": cache_read,
                "cache_creation": cache_creation,
            },
        }
    )


@pytest.fixture(autouse=True)
def _no_leaked_sink():
    """Ensure the module-level sink does not leak between tests."""
    clear_cost_sink()
    yield
    clear_cost_sink()


def test_estimate_cost_known_model_regular_tokens_only() -> None:
    # 1_000_000 input + 1_000_000 output against Haiku 4.5 at $1/$5 per Mtok.
    cost, known = estimate_cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert known is True
    assert cost == pytest.approx(1.00 + 5.00, rel=1e-9)


def test_estimate_cost_cache_read_priced_at_discount() -> None:
    # All input tokens cache-read: 1_000_000 * $1/Mtok * 0.10 = $0.10.
    cost, _ = estimate_cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(1.00 * CACHE_READ_MULTIPLIER, rel=1e-9)


def test_estimate_cost_cache_creation_priced_at_premium() -> None:
    # All input tokens cache-creation: 1_000_000 * $1/Mtok * 1.25 = $1.25.
    cost, _ = estimate_cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    assert cost == pytest.approx(1.00 * CACHE_WRITE_MULTIPLIER, rel=1e-9)


def test_estimate_cost_unknown_model_uses_fallback_and_flags() -> None:
    cost, known = estimate_cost_usd(
        "claude-unknown-future-2027",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert known is False
    # Fallback rates are the worst-case Opus tier — verify against the table.
    opus = PRICING_USD_PER_MILLION_TOKENS["claude-opus-4-7"]
    assert cost == pytest.approx(opus["input"] + opus["output"], rel=1e-9)


def test_emit_llm_call_is_noop_without_a_sink() -> None:
    # No sink installed; emit_llm_call must not raise and must not record anything.
    emit_llm_call(model="haiku", stage="classify", response=_fake_response(100, 50))


def test_emit_llm_call_routes_through_installed_sink() -> None:
    observations: list[LLMCallObservation] = []
    set_cost_sink(observations.append)
    emit_llm_call(
        model="claude-haiku-4-5-20251001",
        stage="classify",
        response=_fake_response(500, 200, cache_read=100, cache_creation=50),
        accession_number="0001-26-001",
    )
    assert len(observations) == 1
    obs = observations[0]
    assert obs.model == "claude-haiku-4-5-20251001"
    assert obs.stage == "classify"
    assert obs.accession_number == "0001-26-001"
    assert obs.input_tokens == 500
    assert obs.output_tokens == 200
    assert obs.cache_read_tokens == 100
    assert obs.cache_creation_tokens == 50
    assert obs.pricing_unknown is False
    assert obs.estimated_cost_usd > 0


def test_emit_llm_call_handles_missing_usage_metadata() -> None:
    observations: list[LLMCallObservation] = []
    set_cost_sink(observations.append)
    response_no_meta = SimpleNamespace()  # no usage_metadata attribute
    emit_llm_call(model="haiku", stage="classify", response=response_no_meta)
    # Zero-token observation is preferable to a silently dropped row.
    assert len(observations) == 1
    assert observations[0].input_tokens == 0
    assert observations[0].output_tokens == 0
    assert observations[0].estimated_cost_usd == 0.0


def test_db_sink_persists_row_and_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _fresh_engine()
    set_cost_sink(db_llm_call_sink(engine))
    emit_llm_call(
        model="claude-haiku-4-5-20251001",
        stage="classify",
        response=_fake_response(500, 200),
        accession_number="0001-26-001",
    )

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT model, stage, accession_number, input_tokens, output_tokens, "
                "estimated_cost_usd FROM llm_calls"
            )
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "claude-haiku-4-5-20251001"
    assert row[1] == "classify"
    assert row[2] == "0001-26-001"
    assert row[3] == 500
    assert row[4] == 200
    assert row[5] > 0

    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    observed = [e for e in events if e["event"] == "llm_call_observed"]
    assert len(observed) == 1
    assert observed[0]["model"] == "claude-haiku-4-5-20251001"
    assert observed[0]["stage"] == "classify"
    assert observed[0]["accession_number"] == "0001-26-001"


def test_db_sink_emits_pricing_unknown_event_for_unknown_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _fresh_engine()
    set_cost_sink(db_llm_call_sink(engine))
    emit_llm_call(
        model="claude-unknown-future-2027",
        stage="classify",
        response=_fake_response(100, 50),
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(e["event"] == "cost_pricing_unknown" for e in events)
    assert any(e["event"] == "llm_call_observed" for e in events)


def test_daily_cost_usd_aggregates_by_utc_day() -> None:
    engine = _fresh_engine()
    insert_sql = text(
        """
        INSERT INTO llm_calls (
            emitted_at, model, stage, accession_number,
            input_tokens, output_tokens, estimated_cost_usd
        ) VALUES (
            :emitted_at, :model, :stage, :accession,
            :input_tokens, :output_tokens, :cost
        )
        """
    )
    with engine.begin() as conn:
        # Two rows on the target day, one on a different day.
        for emitted_at, cost in [
            ("2026-06-04T08:00:00+00:00", 0.50),
            ("2026-06-04T22:30:00+00:00", 1.25),
            ("2026-06-03T23:59:00+00:00", 9.99),  # different day, must be excluded
        ]:
            conn.execute(
                insert_sql,
                {
                    "emitted_at": emitted_at,
                    "model": "claude-haiku-4-5-20251001",
                    "stage": "classify",
                    "accession": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost": cost,
                },
            )

    assert daily_cost_usd(engine, "2026-06-04") == pytest.approx(0.50 + 1.25, rel=1e-9)
    assert daily_cost_usd(engine, "2026-06-03") == pytest.approx(9.99, rel=1e-9)
    assert daily_cost_usd(engine, "2026-06-05") == 0.0


def test_daily_token_usage_aggregates_each_token_dimension() -> None:
    """Tokens are the controllable engineering metric (separate from cost);
    daily_token_usage rolls each dimension up per UTC day so trend analysis
    is independent of pricing changes."""
    engine = _fresh_engine()
    insert_sql = text(
        """
        INSERT INTO llm_calls (
            emitted_at, model, stage, accession_number,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            estimated_cost_usd
        ) VALUES (
            :emitted_at, :model, 'classify', NULL,
            :input_tokens, :output_tokens,
            :cache_read, :cache_creation,
            0.0
        )
        """
    )
    with engine.begin() as conn:
        for emitted_at, inp, out, cr, cc in [
            ("2026-06-04T08:00:00+00:00", 1000, 200, 800, 100),
            ("2026-06-04T22:30:00+00:00", 500, 100, 400, 50),
            ("2026-06-03T23:59:00+00:00", 9999, 999, 999, 999),  # different day, excluded
        ]:
            conn.execute(
                insert_sql,
                {
                    "emitted_at": emitted_at,
                    "model": "claude-haiku-4-5-20251001",
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read": cr,
                    "cache_creation": cc,
                },
            )

    target = daily_token_usage(engine, "2026-06-04")
    assert target == {
        "input_tokens": 1500,
        "output_tokens": 300,
        "cache_read_tokens": 1200,
        "cache_creation_tokens": 150,
    }

    empty = daily_token_usage(engine, "2026-06-05")
    assert empty == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
