"""Structured JSON logging with PII redaction.

Conversation text is never logged by the framework; only identifiers, acts and timings are.
The redaction filter is a second line of defence for any free text that reaches the logger.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{6,}\d)(?!\w)")
_API_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)\S+")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _API_KEY.sub(r"\1\2[redacted]", text)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()))
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    _RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Keep third-party noise out of the audit trail.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
