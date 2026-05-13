"""Configuration / secret access seam.

All code that needs API keys or runtime config reads it through this module
instead of `os.environ` directly. That gives us a single point to swap the
source — `.env` for local dev today, AWS SSM Parameter Store / Secrets Manager
when deployed — without touching call sites.

Discipline:
    Never log a secret. Never print a secret. Never include a secret in an
    exception message. The `get_secret` helper raises a generic exception
    on miss so the call site can decide what to do without ever seeing the
    underlying value.
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


_loaded = False


def _ensure_dotenv_loaded() -> None:
    global _loaded
    if not _loaded:
        load_dotenv()
        _loaded = True


def get_secret(name: str) -> str:
    """Fetch a secret by name.

    Today: reads from the process environment, after loading `.env` once.
    Tomorrow (deployed): swap to AWS SSM Parameter Store / Secrets Manager
    by changing only this function — call sites stay the same.
    """
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(f"required config not set: {name}")
    return value


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
    )
