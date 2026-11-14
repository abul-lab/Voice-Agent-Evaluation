"""Exception hierarchy. Every error carries a stable machine-readable ``code`` so API clients can branch on it."""

from __future__ import annotations


class VoiceShopError(Exception):
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ConfigError(VoiceShopError):
    code = "config_invalid"


class ScenarioError(VoiceShopError):
    code = "scenario_invalid"
