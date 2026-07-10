"""Tests for check-ingest-freshness: the daily-index cursor dead-man's switch.

Hermetic — a tmp_path SQLite DB with migrations applied. `run_check` takes the
"today" date as a parameter, so lag and the alarm decision are exercised against
a seeded cursor and a fixed date without patching the clock. Assertions land on
the alerts_outbox rows the alarm writes (delivery is the drainer's concern).

Weekday anchor (2024-01-01 is a Monday):
    Mon 2024-01-01  Tue -02  Wed -03  Thu -04  Fri -05  Sat -06  Sun -07  Mon -08
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.cli.check_ingest_freshness import run_check
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import advance_ingest_cursor

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

THRESHOLD = 3


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


def _outbox_rows(engine: Engine) -> list[tuple[str, str | None]]:
    with engine.begin() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(text("SELECT severity, dedup_key FROM alerts_outbox")).fetchall()
        ]


def test_no_cursor_does_not_alarm(engine: Engine) -> None:
    # A fresh install (cursor unset) must not page — we cannot tell "never ran"
    # from "just installed", and a wholly dead system is caught elsewhere.
    run_check(engine, today_et=date(2024, 1, 8), threshold=THRESHOLD)
    assert _outbox_rows(engine) == []


def test_recent_cursor_below_threshold_does_not_alarm(engine: Engine) -> None:
    # cursor Wed-03, today Mon-08 -> interior weekdays Thu-04, Fri-05 = lag 2 < 3.
    advance_ingest_cursor(engine, "0000000000-24-000001", "20240103")
    run_check(engine, today_et=date(2024, 1, 8), threshold=THRESHOLD)
    assert _outbox_rows(engine) == []


def test_lag_at_threshold_alarms(engine: Engine) -> None:
    # cursor Mon-01, today Fri-05 -> interior weekdays Tue,Wed,Thu = lag 3 == 3.
    advance_ingest_cursor(engine, "0000000000-24-000001", "20240101")
    run_check(engine, today_et=date(2024, 1, 5), threshold=THRESHOLD)
    assert _outbox_rows(engine) == [("alert", "ingest_cursor_stale")]


def test_multi_day_stall_alarms_once(engine: Engine) -> None:
    # cursor Mon-01, today Mon-08 -> lag 4 >= 3. One row, the per-cause dedup key.
    advance_ingest_cursor(engine, "0000000000-24-000001", "20240101")
    run_check(engine, today_et=date(2024, 1, 8), threshold=THRESHOLD)
    rows = _outbox_rows(engine)
    assert rows == [("alert", "ingest_cursor_stale")]


def test_weekend_does_not_alarm(engine: Engine) -> None:
    # cursor Fri-05, checked Mon-08: only Sat + Sun lie between -> lag 0, no page.
    advance_ingest_cursor(engine, "0000000000-24-000001", "20240105")
    run_check(engine, today_et=date(2024, 1, 8), threshold=THRESHOLD)
    assert _outbox_rows(engine) == []
