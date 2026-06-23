"""Unit tests for config env-parsing helpers."""

from __future__ import annotations

import pytest

from filings_orchestrator.config import MissingConfigError, get_config_int


def test_get_config_int_returns_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_FILINGS_PER_TICK", raising=False)
    assert get_config_int("MAX_FILINGS_PER_TICK", 10) == 10


def test_get_config_int_parses_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_FILINGS_PER_TICK", "25")
    assert get_config_int("MAX_FILINGS_PER_TICK", 10) == 25


def test_get_config_int_empty_string_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_FILINGS_PER_TICK", "")
    assert get_config_int("MAX_FILINGS_PER_TICK", 10) == 10


def test_get_config_int_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_FILINGS_PER_TICK", "abc")
    with pytest.raises(MissingConfigError):
        get_config_int("MAX_FILINGS_PER_TICK", 10)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_get_config_int_rejects_non_positive(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """A zero or negative batch size would silently stall ingestion, so it must
    be loud rather than accepted."""
    monkeypatch.setenv("MAX_FILINGS_PER_TICK", bad)
    with pytest.raises(MissingConfigError):
        get_config_int("MAX_FILINGS_PER_TICK", 10)
