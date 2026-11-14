"""One JSON-over-HTTPS client for the external services (speech and the judge).

Retries only what is worth retrying: rate limits (429) and temporary server errors (500, 503), and network
timeouts. Anything else fails at once with the status code. Error messages never contain request headers,
so API keys cannot leak into logs or reports.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

RETRY_STATUSES = frozenset({429, 500, 503})
log = logging.getLogger("voiceshop.net")


class ServiceError(RuntimeError):
    """An external service failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def post_json(url: str, body: dict[str, Any], headers: dict[str, str], *, timeout_s: float = 120.0,
              attempts: int = 4, base_delay_s: float = 2.0) -> dict[str, Any]:
    """POST a JSON body and return the JSON answer. Waits base_delay_s, then twice as long each retry."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    data = json.dumps(body).encode("utf-8")
    last_status: int | None = None
    last_error = "no attempt was made"
    for attempt in range(attempts):
        req = urllib.request.Request(  # noqa: S310 - the URL is configured by the operator, https in production
            url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUSES:
                raise ServiceError(f"service returned HTTP {exc.code}", status=exc.code) from None
            last_status, last_error = exc.code, f"HTTP {exc.code}"
            log.warning("service busy, retrying", extra={"status": exc.code, "attempt": attempt + 1,
                                                        "attempts": attempts})
        except (urllib.error.URLError, TimeoutError) as exc:
            last_status, last_error = None, type(exc).__name__
            log.warning("service did not answer, retrying", extra={"error": type(exc).__name__,
                                                                  "attempt": attempt + 1, "attempts": attempts})
        if attempt < attempts - 1:
            time.sleep(base_delay_s * (2 ** attempt))
    raise ServiceError(f"service unavailable after {attempts} attempts ({last_error})", status=last_status)
