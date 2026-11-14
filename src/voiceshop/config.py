"""Runtime settings, read from environment variables (VOICESHOP_*) and the Gemini key variables.

Secrets (API keys) are only read from the environment and are never logged or serialised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiceshop.errors import ConfigError


@dataclass(frozen=True)
class RuntimeSettings:
    db_path: Path
    api_key: str | None  # one shared admin key from the environment (optional; named keys are preferred)
    keys_file: Path  # named API keys with roles (hashes only)
    log_level: str
    gemini_api_key: str | None
    gemini_model: str
    local_ui_no_key: bool  # explicit opt-in: loopback UI may change things without the API key
    max_concurrent_runs: int
    max_body_bytes: int
    trust_proxy: bool  # behind a reverse proxy: use X-Forwarded-For for per-client limits
    retention_days: int  # run transcripts older than this are removed (the audit trail is kept)


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None


def load_settings(env: dict[str, str] | None = None) -> RuntimeSettings:
    env = dict(os.environ if env is None else env)
    return RuntimeSettings(
        db_path=Path(env.get("VOICESHOP_DB", "results/voiceshop.db")),
        api_key=env.get("VOICESHOP_API_KEY") or None,
        keys_file=Path(env.get("VOICESHOP_KEYS_FILE", "results/api_keys.json")),
        log_level=env.get("VOICESHOP_LOG_LEVEL", "INFO").upper(),
        gemini_api_key=env.get("VOICESHOP_GEMINI_API_KEY") or env.get("GEMINI_API_KEY") or None,
        gemini_model=env.get("VOICESHOP_GEMINI_MODEL", "gemini-3.5-flash"),
        local_ui_no_key=env.get("VOICESHOP_LOCAL_UI_NO_KEY", "").lower() in ("1", "true", "yes"),
        max_concurrent_runs=_int(env, "VOICESHOP_MAX_CONCURRENT_RUNS", 2),
        max_body_bytes=_int(env, "VOICESHOP_MAX_BODY_BYTES", 65536),
        trust_proxy=env.get("VOICESHOP_TRUST_PROXY", "").lower() in ("1", "true", "yes"),
        retention_days=_int(env, "VOICESHOP_RETENTION_DAYS", 14),
    )


_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def problems(settings: RuntimeSettings) -> list[str]:
    """Everything wrong with the configuration, in plain words. An empty list means it can start."""
    out: list[str] = []
    if not 1 <= settings.max_concurrent_runs <= 16:
        out.append("VOICESHOP_MAX_CONCURRENT_RUNS must be between 1 and 16")
    if not 1024 <= settings.max_body_bytes <= 10_000_000:
        out.append("VOICESHOP_MAX_BODY_BYTES must be between 1024 and 10000000")
    if not 1 <= settings.retention_days <= 3650:
        out.append("VOICESHOP_RETENTION_DAYS must be between 1 and 3650")
    if settings.log_level not in _LEVELS:
        out.append(f"VOICESHOP_LOG_LEVEL must be one of {', '.join(_LEVELS)}")
    parent = settings.db_path.parent if str(settings.db_path.parent) else Path(".")
    if parent.exists() and not parent.is_dir():
        out.append(f"the database folder {parent} is a file, not a folder")
    try:
        from voiceshop.monitor.auth import KeyFileError, KeyRing, load_entries

        KeyRing(load_entries(settings.keys_file))
    except (KeyFileError, ValueError, OSError) as exc:
        out.append(f"the API key file {settings.keys_file} cannot be used: {exc}")
    return out


def describe(settings: RuntimeSettings) -> dict[str, Any]:
    """The effective settings for humans. Secrets are reported as set or not set, never as values."""
    return {
        "database": str(settings.db_path),
        "keys_file": str(settings.keys_file),
        "shared_admin_key": "set" if settings.api_key else "not set",
        "gemini_key": "set" if settings.gemini_api_key else "not set",
        "gemini_model": settings.gemini_model,
        "local_ui_without_key": settings.local_ui_no_key,
        "max_concurrent_runs": settings.max_concurrent_runs,
        "max_body_bytes": settings.max_body_bytes,
        "log_level": settings.log_level,
        "transcript_retention_days": settings.retention_days,
    }
