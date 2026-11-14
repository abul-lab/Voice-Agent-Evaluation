"""A check is one pass/fail property of a run, with a human-readable detail.

The store suite builds its checks in ``voiceshop.catalog.storerun`` and reports them through the harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}
