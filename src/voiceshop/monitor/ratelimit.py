"""Token-bucket rate limit. Each key (a person, or a client address) gets ``capacity`` requests, refilled at
``per_minute``. Used to slow down bursts of changes and repeated wrong keys."""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, per_minute: int) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # tokens per second
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last seen)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            if len(self._state) > 10_000:  # forget idle keys so memory stays bounded
                self._state = {k: v for k, v in self._state.items() if now - v[1] < 3600}
            return True
