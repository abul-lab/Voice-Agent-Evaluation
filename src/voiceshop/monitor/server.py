"""Live transcript monitor: JSON API, Server-Sent Events stream and a two-speaker web UI.

Security posture (defaults)
  * binds to 127.0.0.1 unless explicitly told otherwise
  * read endpoints are open on the bound interface; mutating endpoints require X-API-Key
    and are disabled entirely when VOICESHOP_API_KEY is not set
  * constant-time key comparison, request body size limit, JSON content-type enforcement
  * strict security headers (CSP, nosniff, frame-deny, no-store on API responses)
  * all UI text is inserted with textContent, never innerHTML
  * request ids on every response for correlation; no conversation text in access logs
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from voiceshop import __version__
from voiceshop.context import RuntimeContext
from voiceshop.errors import VoiceShopError
from voiceshop.monitor.admin import AdminRoutes
from voiceshop.monitor.auth import KeyRing, Principal, TicketStore
from voiceshop.monitor.ratelimit import TokenBucket
from voiceshop.monitor.runs import RunRoutes
from voiceshop.monitor.security import SECURITY_HEADERS
from voiceshop.monitor.settings import _load_settings, _store_scopes
from voiceshop.monitor.storefront import ShopRoutes

PASS_THRESHOLD = 0.9  # share of fully passing cases needed for the gate

log = logging.getLogger("voiceshop.monitor")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SID = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")  # \Z: no trailing newline allowed
_PAGE = re.compile(r"(home|cart|checkout|category:[a-z_]{1,40}|product:[a-z0-9-]{1,40})\Z")
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")



class Metrics:
    """Minimal in-process counters rendered in Prometheus text format at /metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.http: dict[tuple[str, int], int] = {}
        self.runs_started = 0
        self.active_runs = 0
        self.runs_rejected = 0
        # Quality of the last finished suite (None until a suite has finished in this process).
        self.last_gate_passed: bool | None = None
        self.last_reply_p95_ms: float | None = None
        self.last_attack_success_rate: float | None = None
        self.last_pass_rate: float | None = None

    def record_suite(self, summary: dict) -> None:
        with self._lock:
            self.last_gate_passed = bool(summary.get("gate_passed"))
            self.last_reply_p95_ms = summary.get("latency_gate", {}).get("p95_ms")
            self.last_attack_success_rate = summary.get("security", {}).get("attack_success_rate")
            self.last_pass_rate = summary.get("pass_rate")

    def http_request(self, route: str, code: int) -> None:
        with self._lock:
            self.http[(route, code)] = self.http.get((route, code), 0) + 1

    def render(self, counts: dict[str, int]) -> str:
        with self._lock:
            lines = [
                "# HELP voiceshop_http_requests_total HTTP requests by route and status.",
                "# TYPE voiceshop_http_requests_total counter",
            ]
            for (route, code), n in sorted(self.http.items()):
                lines.append(f'voiceshop_http_requests_total{{route="{route}",code="{code}"}} {n}')
            lines += [
                "# HELP voiceshop_runs_started_total Simulation runs started by the API.",
                "# TYPE voiceshop_runs_started_total counter",
                f"voiceshop_runs_started_total {self.runs_started}",
                "# HELP voiceshop_runs_rejected_total Runs rejected because the server was busy.",
                "# TYPE voiceshop_runs_rejected_total counter",
                f"voiceshop_runs_rejected_total {self.runs_rejected}",
                "# HELP voiceshop_active_runs Runs currently executing.",
                "# TYPE voiceshop_active_runs gauge",
                f"voiceshop_active_runs {self.active_runs}",
            ]
            if self.last_gate_passed is not None:
                lines += [
                    "# HELP voiceshop_last_suite_gate_passed 1 when the last finished suite passed its release gate.",
                    "# TYPE voiceshop_last_suite_gate_passed gauge",
                    f"voiceshop_last_suite_gate_passed {1 if self.last_gate_passed else 0}",
                    "# HELP voiceshop_last_suite_pass_rate Share of cases that passed in the last finished suite.",
                    "# TYPE voiceshop_last_suite_pass_rate gauge",
                    f"voiceshop_last_suite_pass_rate {self.last_pass_rate}",
                    "# HELP voiceshop_last_suite_attack_success_rate Share of red-team attacks that succeeded.",
                    "# TYPE voiceshop_last_suite_attack_success_rate gauge",
                    f"voiceshop_last_suite_attack_success_rate {self.last_attack_success_rate}",
                ]
                if self.last_reply_p95_ms is not None:
                    lines += [
                        "# HELP voiceshop_last_suite_reply_p95_ms 95th percentile of agent reply time, last suite.",
                        "# TYPE voiceshop_last_suite_reply_p95_ms gauge",
                        f"voiceshop_last_suite_reply_p95_ms {self.last_reply_p95_ms}",
                    ]
            for status, n in sorted(counts.items()):
                if status == "turns":
                    continue
                lines.append(f'voiceshop_runs_total{{status="{status}"}} {n}')
            lines.append(f"voiceshop_turns_total {counts.get('turns', 0)}")
        return "\n".join(lines) + "\n"


class MonitorApp:
    """Holds shared state for the HTTP handlers. One instance per server process."""

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        api_key: str | None = None,
        keys: KeyRing | None = None,
        max_concurrent_runs: int,
        max_body_bytes: int,
        local_ui_no_key: bool = False,
        store_bundle: dict[str, Any] | None = None,
        settings_path: Path | None = None,
        changes_per_minute: int = 30,
        gemini_key: str | None = None,
        trust_proxy: bool = False,
        attempts_per_minute: int = 60,
    ) -> None:
        self.ctx = ctx
        self.trust_proxy = trust_proxy
        self.gemini_key = gemini_key  # for runs whose front desk routes with Gemini
        self.limit_changes = TokenBucket(changes_per_minute)  # per person, for changes
        self.limit_attempts = TokenBucket(attempts_per_minute)  # per client address, for every mutating try
        # Monitor settings (the storefront URL). None means "this monitor's own /shop page".
        self.settings_path = settings_path
        self.storefront_url: str | None = _load_settings(settings_path)
        # {"catalog": Catalog, "store": StoreData, "scenarios": list[StoreScenario], "shops": ShopSessions}
        self.store_bundle = store_bundle
        self.local_ui_no_key = local_ui_no_key
        self.keys = keys if keys is not None else KeyRing.single(api_key)
        self.tickets = TicketStore()  # single-use tickets for live streams (see auth.TicketStore)
        self.api_key = api_key  # kept for callers that still pass one shared key
        self.max_body = max_body_bytes
        self.metrics = Metrics()
        self._slots = threading.BoundedSemaphore(max_concurrent_runs)
        self._cancels: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent_runs, thread_name_prefix="voiceshop-run")

    # ----------------------------------------------------------- auth / limits
    def authorised(self, presented: str | None) -> bool:
        return self.keys.authenticate(presented) is not None

    def try_acquire(self) -> bool:
        if self._slots.acquire(blocking=False):
            return True
        self.metrics.runs_rejected += 1
        return False

    def release(self) -> None:
        self._slots.release()

    # ------------------------------------------------------------- background
    def cancel_suite(self, batch_id: str, *, actor: str = "api") -> bool:
        event = self._cancels.get(batch_id)
        if event is None:
            return False
        event.set()
        self.ctx.audit.record("monitor", actor, "suite.cancelled", batch_id)
        return True

    def submit_store_suite(self, test_type: str, pace_ms: int = 0, *, actor: str = "api", mode: str = "rules") -> str:
        """Run the multi-agent store suite for a test type, paced and cancellable."""
        from voiceshop.catalog.route_gemini import GeminiRouteClassifier
        from voiceshop.catalog.storerun import run_store_suite

        classifier = GeminiRouteClassifier(self.gemini_key) if mode == "gemini" else None

        bundle = self.store_bundle or {}
        tag_map = {"smoke": {"smoke"}, "targeted": {"targeted"}, "regression": set(), "barge_in": {"barge_in"},
                   "barge_in_demo": {"barge_in_demo"}}
        batch_id = f"store-{uuid.uuid4().hex[:10]}"
        stop = threading.Event()
        self._cancels[batch_id] = stop
        delay = pace_ms / 1000.0

        def pace(_turn) -> None:
            if stop.is_set():
                raise VoiceShopError("suite cancelled", code="cancelled")
            if delay:
                _time.sleep(delay)

        def job() -> None:
            self.metrics.active_runs += 1
            try:
                report = run_store_suite(self.ctx, bundle["catalog"], bundle["store"], bundle["scenarios"],
                                threshold=PASS_THRESHOLD, batch_id=batch_id,
                                tags=tag_map[test_type], on_turn=pace, stop_event=stop,
                                shops=bundle["shops"], source=actor, mode=mode, classifier=classifier)
                if not stop.is_set():  # a stopped suite is incomplete: it must not change the quality measures
                    self.metrics.record_suite(report["summary"])
            except Exception:
                log.exception("store suite crashed", extra={"batch_id": batch_id})
            finally:
                self.metrics.active_runs -= 1
                self.release()

        self.metrics.runs_started += 1
        self._pool.submit(job)
        return batch_id

    def drain(self, timeout_s: float = 30.0) -> bool:
        """Stop the work in progress: ask every running suite to stop, then wait up to ``timeout_s`` for them.
        Returns True when nothing is left running."""
        for event in list(self._cancels.values()):
            event.set()
        deadline = _time.monotonic() + timeout_s
        while self.metrics.active_runs > 0 and _time.monotonic() < deadline:
            _time.sleep(0.05)
        return self.metrics.active_runs == 0

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class _Handler(ShopRoutes, AdminRoutes, RunRoutes, BaseHTTPRequestHandler):
    server_version = f"voiceshop/{__version__}"
    protocol_version = "HTTP/1.1"
    app: MonitorApp  # injected by factory

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # route to structured logger, without bodies
        log.info("http", extra={"request_id": getattr(self, "_request_id", None), "method": self.command,
                                "path": urlparse(self.path).path, "client": self.client_address[0]})

    def _rid(self) -> str:
        incoming = self.headers.get("X-Request-Id", "")
        return incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex

    def _send(self, code: int, body: bytes, content_type: str, route: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._request_id)
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.app.metrics.http_request(route, code)

    def _json(self, code: int, payload: Any, route: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", route, {"Cache-Control": "no-store"})

    def _error(self, code: int, err_code: str, message: str, route: str) -> None:
        self._json(code, {"error": {"code": err_code, "message": message, "request_id": self._request_id}}, route)

    def _read_json(self) -> dict | None:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise ValueError("content-type must be application/json")
        raw = getattr(self, "_raw_body", b"")
        if not raw or len(raw) > self.app.max_body:
            raise ValueError("body missing or too large")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # --------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802
        self._request_id = self._rid()
        parsed = urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
        try:
            if path == "/api/runs" or path.startswith("/api/runs/"):
                # Transcripts and the audit trail are personal data: a valid key is required to read them.
                ticket = (query.get("ticket") or [None])[0] if path.endswith("/stream") else None
                if self._read_principal("/api/runs/{id}", ticket) is None:
                    return
            static_routes = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/shop": ("shop.html", "text/html; charset=utf-8"),
                "/history": ("history.html", "text/html; charset=utf-8"),
                "/history.js": ("history.js", "text/javascript; charset=utf-8"),
                "/shop.js": ("shop.js", "text/javascript; charset=utf-8"),
                "/shop.css": ("shop.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/monitor.css": ("monitor.css", "text/css; charset=utf-8"),
                "/tests": ("tests.html", "text/html; charset=utf-8"),
                "/tests.js": ("tests.js", "text/javascript; charset=utf-8"),
                "/voice.js": ("voice.js", "text/javascript; charset=utf-8"),
                "/agents.js": ("agents.js", "text/javascript; charset=utf-8"),
                "/config": ("config.html", "text/html; charset=utf-8"),
                "/config.js": ("config.js", "text/javascript; charset=utf-8"),
            }
            if path in static_routes:
                name, ctype = static_routes[path]
                # The storefront is embedded in the monitor, so it may be framed by this origin only.
                frame = {"X-Frame-Options": "SAMEORIGIN",
                         "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"].replace(
                             "frame-ancestors 'none'", "frame-ancestors 'self'")} if path == "/shop" else None
                return self._static(name, ctype, "static" if path != "/" else "/", frame)
            if path == "/api/settings":
                return self._json(200, self._settings_view(), "/api/settings")
            if path == "/api/capabilities":
                # Never includes secrets.
                return self._json(200, {
                    "api_key_required": not self._local_ui_allowed(),
                    "domains": ["store"],
                    "agents": {k: sorted(v) for k, v in _store_scopes().items()},
                    "local_ui_no_key": self.app.local_ui_no_key,
                    "gemini_routing": bool(self.app.gemini_key),
                }, "/api/capabilities")
            if path == "/healthz":
                return self._json(200, {"status": "ok", "version": __version__}, "/healthz")
            if path == "/readyz":
                return self._readyz()
            if path == "/metrics":
                counts = self.app.ctx.runs.counts()
                return self._send(200, self.app.metrics.render(counts).encode(),
                                  "text/plain; version=0.0.4; charset=utf-8", "/metrics")
            audit = re.fullmatch(r"/api/runs/([A-Za-z0-9._-]{1,120})/audit", path)
            if audit:
                if self.app.ctx.runs.get(audit.group(1)) is None:
                    return self._error(404, "not_found", "no such run", "/api/runs/{id}/audit")
                return self._json(200, {"items": self.app.ctx.audit.list(audit.group(1))},
                                  "/api/runs/{id}/audit")
            if path == "/shop/api/catalog":
                bundle = self._store()
                cat = bundle["catalog"]
                products = {p.id: {"id": p.id, "name": p.name, "category": p.category, "price": p.price,
                                   "weight_kg": p.weight_kg, "capacity": p.capacity, "season": p.season,
                                   "stock": p.stock} for p in cat.products.values()}
                return self._json(200, {"store": cat.store_name, "products": products}, "/shop/api/catalog")
            if path == "/shop/api/state":
                return self._shop_state(query)
            if path == "/shop/api/stream":
                return self._shop_stream(query)
            if path == "/api/scenarios":
                return self._json(200, {"items": self._store_items() if self.app.store_bundle else []},
                                  "/api/scenarios")
            if path == "/api/runs":
                limit = int((query.get("limit") or ["50"])[0])
                batch = (query.get("batch") or [None])[0]
                items = self.app.ctx.runs.list(limit=limit, batch_id=batch)
                return self._json(200, {"items": items}, "/api/runs")
            m = re.fullmatch(r"/api/runs/([^/]+)(/turns|/stream)?", path)
            if m:
                return self._run_resource(m.group(1), m.group(2), query)
            return self._error(404, "not_found", "no such route", "not_found")
        except ValueError as exc:
            return self._error(400, "bad_request", str(exc), "bad_request")
        except Exception:
            log.exception("request failed", extra={"request_id": self._request_id})
            return self._error(500, "internal_error", "internal error", "error")

    def do_POST(self) -> None:  # noqa: N802
        self._request_id = self._rid()
        # Always consume the body first: replying early on a keep-alive socket with unread data
        # makes the client see a connection reset instead of our error response.
        length = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(min(length, self.app.max_body + 1)) if 0 < length else b""
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/history/clear":
            return self._history_clear()
        if path == "/api/settings":
            return self._settings_save()
        if path == "/api/stream-tickets":
            principal = self._read_principal("/api/stream-tickets")
            if principal is None:
                return
            return self._json(200, {"ticket": self.app.tickets.issue(principal), "expires_in_s": 60},
                              "/api/stream-tickets")
        cancel = re.fullmatch(r"/api/batches/([A-Za-z0-9._-]{1,80})/cancel", path)
        if path == "/shop/api/action":
            return self._shop_action()
        if path != "/api/evals" and not cancel:
            return self._error(404, "not_found", "no such route", "not_found")
        principal = self._authorise("operator", path)
        if principal is None:
            return
        try:
            body = self._read_json() or {}
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, "bad_request", str(exc), path)
        pace_raw = body.get("pace_ms", 800)
        if not isinstance(pace_raw, int) or not 0 <= pace_raw <= 3000:
            return self._error(400, "bad_request", "pace_ms must be an integer between 0 and 3000", path)
        pace_ms = pace_raw
        if cancel:
            found = self.app.cancel_suite(cancel.group(1), actor=principal.name)
            if not found:
                return self._error(404, "not_found", "no running suite with that id", "/api/batches/{id}/cancel")
            return self._json(200, {"cancelled": True}, "/api/batches/{id}/cancel")
        test_type = body.get("test_type", "smoke")
        if test_type not in ("smoke", "targeted", "regression", "barge_in", "barge_in_demo"):
            return self._error(400, "bad_request", "test_type must be smoke, targeted, regression, barge_in or barge_in_demo", path)
        mode = body.get("mode", "rules")
        if mode not in ("rules", "gemini"):
            return self._error(400, "bad_request", "mode must be rules or gemini", path)
        if mode == "gemini" and not self.app.gemini_key:
            return self._error(400, "gemini_not_configured", "Gemini is not configured: set GEMINI_API_KEY", path)
        if not self.app.store_bundle:
            return self._error(400, "domain_unavailable", "the store suite is not configured", path)
        if not self.app.try_acquire():
            return self._error(429, "busy", "too many runs in progress; retry later", path)
        batch = self.app.submit_store_suite(test_type, pace_ms, actor=principal.name, mode=mode)
        # The audit entry carries the request id, so one identifier links the HTTP call, its log lines and the work.
        self.app.ctx.audit.record("monitor", principal.name, "suite.started", batch,
                                  request_id=self._request_id, test_type=test_type)
        return self._json(202, {"batch_id": batch, "runs": f"/api/runs?batch={batch}"}, path)

    # --------------------------------------------------------------- helpers
    def _authorise(self, role: str, route: str) -> Principal | None:
        """Who is calling, and whether their role allows this route. Sends the error itself and returns None."""
        if not self.app.limit_attempts.allow(self._client_address()):
            self._error(429, "rate_limited", "too many requests from this address; slow down", route)
            return None
        if self._local_ui_allowed():
            return Principal("local-ui", "admin")  # the explicit loopback-only exception
        if self.app.keys.is_empty:
            self._error(403, "api_disabled", "mutating API is disabled: no API key is configured", route)
            return None
        principal = self.app.keys.authenticate(self.headers.get("X-API-Key"))
        if principal is None:
            self._error(401, "unauthorised", "missing or invalid API key", route)
            return None
        if not principal.allows(role):
            self._error(403, "forbidden", f"this action needs the {role} role", route)
            return None
        if not self.app.limit_changes.allow(principal.name):
            self._error(429, "rate_limited", f"too many changes by {principal.name}; slow down", route)
            return None
        return principal

    def _read_principal(self, route: str, ticket: str | None = None) -> Principal | None:
        """Who may read run transcripts and audit records. Any valid key may read; the storefront stays open.
        A stream ticket (from POST /api/stream-tickets) stands in for the header on EventSource streams."""
        if not self.app.limit_attempts.allow(self._client_address()):
            self._error(429, "rate_limited", "too many requests from this address; slow down", route)
            return None
        if self._local_ui_allowed():
            return Principal("local-ui", "admin")
        if ticket is not None:
            principal = self.app.tickets.redeem(ticket)
            if principal is None:
                self._error(401, "unauthorised", "stream ticket is missing, used or expired", route)
                return None
            return principal
        if self.app.keys.is_empty:
            self._error(403, "api_disabled", "reading run records is disabled: no API key is configured", route)
            return None
        principal = self.app.keys.authenticate(self.headers.get("X-API-Key"))
        if principal is None:
            self._error(401, "unauthorised", "this page needs a valid API key", route)
            return None
        return principal

    def _client_address(self) -> str:
        """The address a request came from. Behind a proxy (only when trusted) it is the forwarded client."""
        if self.app.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()[:64]  # the address the trusted proxy added
        return self.client_address[0]

    def _local_ui_allowed(self) -> bool:
        """Opt-in, loopback-only bypass for the dashboard on this machine.

        Requires: the server is switched on for it, the client is on loopback, the Host header names
        this server on localhost, and any Origin header is this same local origin. A page from another
        website cannot satisfy the Origin check, because browsers always send the real origin.
        """
        if not self.app.local_ui_no_key:
            return False
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # ----------------------------------------------------------- storefront
    def _static(self, name: str, ctype: str, route: str, extra: dict | None = None) -> None:
        body = resources.files("voiceshop.monitor").joinpath("static", name).read_bytes()
        self._send(200, body, ctype, route, {"Cache-Control": "no-cache", **(extra or {})})

def make_server(app: MonitorApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if app.local_ui_no_key and host not in ("127.0.0.1", "localhost", "::1"):
        raise VoiceShopError("VOICESHOP_LOCAL_UI_NO_KEY is only allowed on a loopback address",
                              code="unsafe_binding")
    if host not in ("127.0.0.1", "localhost", "::1") and app.keys.is_empty:
        raise VoiceShopError("refusing to bind a non-loopback address without VOICESHOP_API_KEY",
                              code="unsafe_binding")
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


__all__ = ["MonitorApp", "make_server", "HTTPStatus"]
