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


def load_config() -> Config:
    """Load the full Config object. Use this at process startup."""
    return Config(
        anthropic_api_key=get_secret("ANTHROPIC_API_KEY"),
        langsmith_api_key=get_secret("LANGSMITH_API_KEY"),
        langsmith_project=get_config_str("LANGSMITH_PROJECT", "filings-watcher"),
        langsmith_tracing=get_config_bool("LANGSMITH_TRACING", default=True),
        edgar_user_agent=require_env("EDGAR_USER_AGENT"),
    )
