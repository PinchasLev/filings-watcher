"""Configuration access seam.

All runtime configuration — credentials and non-credential required values
alike — reads through this module instead of `os.environ` directly. That
gives us a single point to swap the source: `.env` for local dev today,
AWS SSM Parameter Store / Secrets Manager when deployed, without touching
call sites.

Two helpers, distinguished by semantics rather than mechanism:

- `get_secret(name)` — for credentials (API keys, passwords, tokens). Never
  log, print, or include in exception messages. Treat the value as sensitive
  end-to-end.
- `require_env(name)` — for required configuration that isn't a credential
  (user agents, region names, endpoint URLs). Safe to log or surface in
  error messages.

Both raise `MissingConfigError` on miss. The functions are mechanically
identical today; the distinction is for the reader.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class MissingConfigError(RuntimeError):
    """Raised when a required configuration value is not set."""


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    langsmith_api_key: str
    langsmith_project: str
    langsmith_tracing: bool
    edgar_user_agent: str
    filings_db_path: str
    # Per-day spend ceiling for Anthropic API usage, USD (ADR 0029).
    # The tick fails fast with cost_cap_exceeded when today's aggregate
    # estimated cost is at or above this value; below the warn threshold
    # but at or above the warn level, a structured cost_warning event fires.
    anthropic_daily_cost_cap_usd: float
    anthropic_daily_cost_warn_usd: float
    # Back-pressure: max new filings classified per ingest tick. A backlog
    # (e.g. after an outage) drains as a bounded stream across ticks rather than
    # in one long invocation that risks the 12-min systemd TimeoutStartSec. The
    # filings-PK dedup carries forward progress between ticks. See ADR 0035.
    max_filings_per_tick: int


_loaded = False


def _ensure_dotenv_loaded() -> None:
    global _loaded
    if not _loaded:
        load_dotenv()
        _loaded = True


def _read_required(name: str) -> str:
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(f"required config not set: {name}")
    return value


def get_secret(name: str) -> str:
    """Fetch a credential by name. See module docstring for handling rules."""
    return _read_required(name)


def require_env(name: str) -> str:
    """Fetch a required non-credential configuration value by name."""
    return _read_required(name)


def get_config_bool(name: str, default: bool = False) -> bool:
    _ensure_dotenv_loaded()
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_config_str(name: str, default: str) -> str:
    _ensure_dotenv_loaded()
    return os.environ.get(name) or default


def get_config_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back to `default`.

    Malformed values raise MissingConfigError so a typo in operator config
    is loud rather than silently reverting to the default.
    """
    _ensure_dotenv_loaded()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise MissingConfigError(f"config {name} must be a number, got {raw!r}") from e


def get_config_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to `default`.

    Malformed or non-positive values raise MissingConfigError so an operator
    typo is loud rather than silently stalling ingestion or reverting to default.
    """
    _ensure_dotenv_loaded()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise MissingConfigError(f"config {name} must be an integer, got {raw!r}") from e
    if value < 1:
        raise MissingConfigError(f"config {name} must be >= 1, got {value}")
    return value


def load_config() -> Config:
    """Load the full Config object. Use this at process startup."""
    import os

    default_db_path = os.path.expanduser("~/.filings-watcher/v0.db")
    # Conservative starting cap until the spend-tracking surface has data to
    # tune against. Operator overrides via env per ADR 0012's tunables rule.
    default_cost_cap_usd = 5.00
    default_cost_warn_usd = 4.00
    return Config(
        anthropic_api_key=get_secret("ANTHROPIC_API_KEY"),
        langsmith_api_key=get_secret("LANGSMITH_API_KEY"),
        langsmith_project=get_config_str("LANGSMITH_PROJECT", "filings-watcher"),
        langsmith_tracing=get_config_bool("LANGSMITH_TRACING", default=True),
        edgar_user_agent=require_env("EDGAR_USER_AGENT"),
        filings_db_path=get_config_str("FILINGS_DB_PATH", default_db_path),
        anthropic_daily_cost_cap_usd=get_config_float(
            "ANTHROPIC_DAILY_COST_CAP_USD", default_cost_cap_usd
        ),
        anthropic_daily_cost_warn_usd=get_config_float(
            "ANTHROPIC_DAILY_COST_WARN_USD", default_cost_warn_usd
        ),
        # Conservative starting batch: ~10 filings per 30s atom tick (~20/min)
        # comfortably clears a normal backlog while keeping a tick well under the
        # 12-min timeout. Operator-tunable via MAX_FILINGS_PER_TICK per ADR 0012.
        max_filings_per_tick=get_config_int("MAX_FILINGS_PER_TICK", 10),
    )
