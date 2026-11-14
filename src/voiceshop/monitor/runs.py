"""Run routes of the monitor: readiness, a run's details, its turns and its live stream. A mixin for the handler."""

from __future__ import annotations

import json
import re
import sqlite3
import time as _time
from typing import Any

from voiceshop.monitor.security import SECURITY_HEADERS
from voiceshop.storage.db import SCHEMA_VERSION

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class RunRoutes:
    def _readyz(self) -> None:
        try:
            self.app.ctx.runs.counts()
            ok, detail = True, {"schema_version": SCHEMA_VERSION}
        except (sqlite3.Error, OSError) as exc:  # readiness reports a database problem instead of crashing
            ok, detail = False, {"error": type(exc).__name__}
        self._json(200 if ok else 503, {"status": "ready" if ok else "not_ready", **detail}, "/readyz")

    def _run_resource(self, run_id: str, sub: str | None, query: dict) -> None:
        if not _RUN_ID.match(run_id):
            raise ValueError("invalid run id")
        route = "/api/runs/{id}" + (sub or "")
        run = self.app.ctx.runs.get(run_id)
        if run is None:
            return self._error(404, "not_found", "no such run", route)
        if sub == "/turns":
            after = int((query.get("after") or ["0"])[0])
            return self._json(200, {"items": self.app.ctx.runs.turns(run_id, after)}, route)
        if sub == "/stream":
            return self._stream(run_id, route)
        return self._json(200, run, route)

    def _stream(self, run_id: str, route: str) -> None:
        """SSE: replays existing turns, then pushes new ones until the run finishes (or 15 min)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Request-Id", self._request_id)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.app.metrics.http_request(route, 200)
        last = 0
        last_audit = 0
        deadline = _time.monotonic() + 15 * 60
        last_beat = _time.monotonic()
        try:
            while _time.monotonic() < deadline:
                for turn in self.app.ctx.runs.turns(run_id, last):
                    last = turn["seq"]
                    self._sse("turn", turn)
                for entry in self.app.ctx.audit.since(run_id, last_audit):
                    last_audit = entry["id"]
                    self._sse("tool", entry)
                run = self.app.ctx.runs.get(run_id)
                if run and run["status"] != "running":
                    self._sse("end", {"status": run["status"], "summary": run["summary"]})
                    return
                if _time.monotonic() - last_beat > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = _time.monotonic()
                _time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _sse(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
        self.wfile.flush()
