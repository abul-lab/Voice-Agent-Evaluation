"""Monitor settings: the storefront address (validated and saved), and the agent names shown in settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

AGENT_NAMES = {
    "router": "Front Desk Specialist",
    "shopping": "Shopping Agent",
    "order": "Order Tracking Agent",
    "refund": "Refunds Agent",
    "care": "Customer Care Agent",
}


def _valid_storefront_url(raw: Any) -> str | None:
    """None means the default. Anything else must be an http(s) address that ends in /shop."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or len(raw) > 300:
        raise ValueError("storefront_url must be a text address of at most 300 characters")
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("storefront_url must start with http:// or https:// and name a host")
    if parsed.path.rstrip("/") != "/shop":
        raise ValueError("storefront_url must point to the storefront page, which ends in /shop")
    return raw.strip()


def _load_settings(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return _valid_storefront_url(json.loads(path.read_text(encoding="utf-8")).get("storefront_url"))
    except (ValueError, OSError, AttributeError):
        return None  # a damaged settings file falls back to the default rather than stopping the monitor


def _save_settings(path: Path, url: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"storefront_url": url}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _store_scopes() -> dict[str, frozenset[str]]:
    """Each specialist's granted scopes, read from the store module so the UI shows the real permissions."""
    from voiceshop.catalog.store import SCOPES

    return SCOPES
