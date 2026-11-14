"""Composition root: builds the shared runtime context from settings.

The CLI, the monitor server and the tests all wire the system the same way through this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from voiceshop.config import RuntimeSettings, load_settings
from voiceshop.storage.db import migrate
from voiceshop.storage.repository import AuditLog, RefundLedger, RunRepository


@dataclass
class RuntimeContext:
    db_path: Path
    audit: AuditLog
    runs: RunRepository
    refunds: RefundLedger


def build_context(settings: RuntimeSettings | None = None, db_path: Path | None = None) -> RuntimeContext:
    settings = settings or load_settings()
    path = Path(db_path or settings.db_path)
    migrate(path)
    return RuntimeContext(db_path=path, audit=AuditLog(path), runs=RunRepository(path),
                          refunds=RefundLedger(path))


def new_run_id(scenario_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scenario_id)[:48]
    return f"{safe}-{uuid.uuid4().hex[:8]}"
