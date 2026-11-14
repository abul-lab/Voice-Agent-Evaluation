"""Runtime settings, read from environment variables (VOICESHOP_*) and the Gemini key variables.

Secrets (API keys) are only read from the environment and are never logged or serialised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
        max_concurrent_runs=int(env.get("VOICESHOP_MAX_CONCURRENT_RUNS", "2")),
        max_body_bytes=int(env.get("VOICESHOP_MAX_BODY_BYTES", "65536")),
    )
