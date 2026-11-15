"""Named API keys with roles. Keys are never stored: the key file keeps a SHA-256 hash of each key.

Roles, lowest to highest:
  viewer    read only (everything GET-able)
  operator  start and cancel test suites
  admin     everything operators can do, plus settings and clearing history

A key is shown once, when it is created. Lose it and you create a new one and revoke the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class KeyFileError(ValueError):
    """The key file is damaged or describes an invalid key."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    def allows(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return secrets.token_urlsafe(32)


class KeyRing:
    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self._entries: list[dict[str, Any]] = []
        names: set[str] = set()
        for e in entries or []:
            name, role, digest = e.get("name"), e.get("role"), e.get("sha256")
            if not isinstance(name, str) or not _NAME.match(name):
                raise KeyFileError("key names use letters, digits, '.', '_' or '-' (1 to 64 characters)")
            if name in names:
                raise KeyFileError(f"duplicate key name {name!r}")
            if role not in ROLE_RANK:
                raise KeyFileError(f"key {name!r}: role must be viewer, operator or admin")
            if not isinstance(digest, str) or not _HASH.match(digest):
                raise KeyFileError(f"key {name!r}: the stored hash is not a SHA-256 hex digest")
            names.add(name)
            self._entries.append({"name": name, "role": role, "sha256": digest})

    @classmethod
    def single(cls, api_key: str | None, name: str = "default-admin") -> KeyRing:
        """One admin key from the environment (VOICESHOP_API_KEY). Empty when no key is given."""
        if not api_key:
            return cls([])
        return cls([{"name": name, "role": "admin", "sha256": hash_key(api_key)}])

    @classmethod
    def from_file(cls, path: Path) -> KeyRing:
        if not path.is_file():
            return cls([])
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KeyFileError(f"key file is not valid JSON: {exc}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise KeyFileError("key file must contain a 'keys' list")
        return cls(doc["keys"])

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def authenticate(self, presented: str | None) -> Principal | None:
        if not presented:
            return None
        digest = hash_key(presented)
        for e in self._entries:  # compare every entry so timing does not reveal which one matched
            if hmac.compare_digest(digest, e["sha256"]):
                return Principal(e["name"], e["role"])
        return None


def save_keys(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keys": entries}, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("keys", []))


class TicketStore:
    """Short-lived, single-use tickets for live streams. A browser's EventSource cannot send the API key
    header, so the key is exchanged once for a ticket that opens one stream and then expires."""

    def __init__(self, ttl_s: float = 60.0, clock=None) -> None:
        self._ttl = ttl_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[Principal, float]] = {}

    def issue(self, principal: Principal) -> str:
        token = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._tickets = {t: v for t, v in self._tickets.items() if v[1] > now}  # drop expired ones
            self._tickets[token] = (principal, now + self._ttl)
        return token

    def redeem(self, token: str | None) -> Principal | None:
        """Returns the principal once; a second use, or an expired ticket, returns None."""
        if not token:
            return None
        with self._lock:
            entry = self._tickets.pop(token, None)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]
