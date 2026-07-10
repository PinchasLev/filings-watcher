"""Unit tests for the shared business-day helpers.

Anchored on 2024-01-01, a Monday, so the weekday of every date below is
unambiguous:

    Mon 2024-01-01  Tue -02  Wed -03  Thu -04  Fri -05  Sat -06  Sun -07  Mon -08
"""

from __future__ import annotations

from datetime import date

from filings_orchestrator.business_days import (
    business_days_between,
    is_business_day,
    parse_filed_at_to_date,
)

MON_1 = date(2024, 1, 1)
WED_3 = date(2024, 1, 3)
FRI_5 = date(2024, 1, 5)
SAT_6 = date(2024, 1, 6)
SUN_7 = date(2024, 1, 7)
MON_8 = date(2024, 1, 8)


def test_parse_filed_at_accepts_yyyymmdd() -> None:
    assert parse_filed_at_to_date("20240101") == MON_1


def test_parse_filed_at_accepts_iso() -> None:
    assert parse_filed_at_to_date("2024-01-01") == MON_1


def test_parse_filed_at_strips_whitespace() -> None:
    assert parse_filed_at_to_date("  20240101  ") == MON_1


def test_is_business_day_weekdays() -> None:
    assert is_business_day(MON_1)
    assert is_business_day(FRI_5)


def test_is_business_day_weekend() -> None:
    assert not is_business_day(SAT_6)
    assert not is_business_day(SUN_7)


def test_between_same_date_is_zero() -> None:
    assert business_days_between(MON_1, MON_1) == 0


def test_between_end_before_start_is_zero() -> None:
    assert business_days_between(WED_3, MON_1) == 0


def test_between_adjacent_days_excludes_both_ends() -> None:
    # Nothing lies strictly between Monday and Tuesday.
    assert business_days_between(MON_1, date(2024, 1, 2)) == 0


def test_between_counts_only_the_interior_weekday() -> None:
    # Mon..Wed: only Tuesday is strictly between.
    assert business_days_between(MON_1, WED_3) == 1


def test_between_skips_the_weekend() -> None:
    # Fri..Mon: only Sat + Sun lie between, neither a business day.
    assert business_days_between(FRI_5, MON_8) == 0


def test_between_full_week_counts_four_interior_weekdays() -> None:
    # Mon..Mon: Tue, Wed, Thu, Fri are interior weekdays; Sat + Sun are not.
    assert business_days_between(MON_1, MON_8) == 4
