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

import hmac
import json
import logging
import os
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
from voiceshop.storage.db import SCHEMA_VERSION

PASS_THRESHOLD = 0.9  # share of fully passing cases needed for the gate

log = logging.getLogger("voiceshop.monitor")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SID = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")  # \Z: no trailing newline allowed
_PAGE = re.compile(r"(home|cart|checkout|category:[a-z_]{1,40}|product:[a-z0-9-]{1,40})\Z")
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


def _store_scopes() -> dict[str, frozenset[str]]:
    """Each specialist's granted scopes, read from the store module so the UI shows the real permissions."""
    from voiceshop.catalog.store import SCOPES

    return SCOPES


class Metrics:
    """Minimal in-process counters rendered in Prometheus text format at /metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.http: dict[tuple[str, int], int] = {}
        self.runs_started = 0
        self.active_runs = 0
        self.runs_rejected = 0

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
            for status, n in sorted(counts.items()):
                if status == "turns":
                    continue
                lines.append(f'voiceshop_runs_total{{status="{status}"}} {n}')
            lines.append(f"voiceshop_turns_total {counts.get('turns', 0)}")
        return "\n".join(lines) + "\n"


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


class MonitorApp:
    """Holds shared state for the HTTP handlers. One instance per server process."""

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        api_key: str | None,
        max_concurrent_runs: int,
        max_body_bytes: int,
        local_ui_no_key: bool = False,
        store_bundle: dict[str, Any] | None = None,
        settings_path: Path | None = None,
    ) -> None:
        self.ctx = ctx
        # Monitor settings (the storefront URL). None means "this monitor's own /shop page".
        self.settings_path = settings_path
        self.storefront_url: str | None = _load_settings(settings_path)
        # {"catalog": Catalog, "store": StoreData, "scenarios": list[StoreScenario], "shops": ShopSessions}
        self.store_bundle = store_bundle
        self.local_ui_no_key = local_ui_no_key
        self.api_key = api_key
        self.max_body = max_body_bytes
        self.metrics = Metrics()
        self._slots = threading.BoundedSemaphore(max_concurrent_runs)
        self._cancels: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent_runs, thread_name_prefix="voiceshop-run")

    # ----------------------------------------------------------- auth / limits
    def authorised(self, presented: str | None) -> bool:
        if not self.api_key or not presented:
            return False
        return hmac.compare_digest(presented.encode(), self.api_key.encode())

    def try_acquire(self) -> bool:
        if self._slots.acquire(blocking=False):
            return True
        self.metrics.runs_rejected += 1
        return False

    def release(self) -> None:
        self._slots.release()

    # ------------------------------------------------------------- background
    def cancel_suite(self, batch_id: str) -> bool:
        event = self._cancels.get(batch_id)
        if event is None:
            return False
        event.set()
        return True

    def submit_store_suite(self, test_type: str, pace_ms: int = 0) -> str:
        """Run the multi-agent store suite for a test type, paced and cancellable."""
        from voiceshop.catalog.storerun import run_store_suite

        bundle = self.store_bundle or {}
        tag_map = {"smoke": {"smoke"}, "targeted": {"targeted"}, "regression": set()}
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
                run_store_suite(self.ctx, bundle["catalog"], bundle["store"], bundle["scenarios"],
                                threshold=PASS_THRESHOLD, batch_id=batch_id,
                                tags=tag_map[test_type], on_turn=pace, stop_event=stop,
                                shops=bundle["shops"])
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


class _Handler(BaseHTTPRequestHandler):
    server_version = f"voiceshop/{__version__}"
    protocol_version = "HTTP/1.1"
    app: MonitorApp  # injected by factory

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # route to structured logger, without bodies
        log.info("http", extra={"method": self.command, "path": urlparse(self.path).path,
                                "client": self.client_address[0]})

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
        cancel = re.fullmatch(r"/api/batches/([A-Za-z0-9._-]{1,80})/cancel", path)
        if path == "/shop/api/action":
            return self._shop_action()
        if path != "/api/evals" and not cancel:
            return self._error(404, "not_found", "no such route", "not_found")
        local_ok = self._local_ui_allowed()
        if not self.app.api_key and not local_ok:
            return self._error(403, "api_disabled", "mutating API is disabled: VOICESHOP_API_KEY not set",
                               path)
        if not local_ok and not self.app.authorised(self.headers.get("X-API-Key")):
            return self._error(401, "unauthorised", "missing or invalid API key", path)
        try:
            body = self._read_json() or {}
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, "bad_request", str(exc), path)
        pace_raw = body.get("pace_ms", 800)
        if not isinstance(pace_raw, int) or not 0 <= pace_raw <= 3000:
            return self._error(400, "bad_request", "pace_ms must be an integer between 0 and 3000", path)
        pace_ms = pace_raw
        if cancel:
            found = self.app.cancel_suite(cancel.group(1))
            if not found:
                return self._error(404, "not_found", "no running suite with that id", "/api/batches/{id}/cancel")
            return self._json(200, {"cancelled": True}, "/api/batches/{id}/cancel")
        test_type = body.get("test_type", "smoke")
        if test_type not in ("smoke", "targeted", "regression"):
            return self._error(400, "bad_request", "test_type must be smoke, targeted or regression", path)
        if not self.app.store_bundle:
            return self._error(400, "domain_unavailable", "the store suite is not configured", path)
        if not self.app.try_acquire():
            return self._error(429, "busy", "too many runs in progress; retry later", path)
        batch = self.app.submit_store_suite(test_type, pace_ms)
        return self._json(202, {"batch_id": batch, "runs": f"/api/runs?batch={batch}"}, path)

    # --------------------------------------------------------------- helpers
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
    def _store(self):
        bundle = self.app.store_bundle
        if not bundle:
            raise ValueError("storefront is not configured")
        return bundle

    @staticmethod
    def _sid(query: dict) -> str:
        raw = (query.get("sid") or [""])[0]
        if raw and _SID.match(raw):
            return raw
        return uuid.uuid4().hex

    def _shop_state(self, query: dict) -> None:
        bundle = self._store()
        sid = self._sid(query)
        at = (query.get("at") or [""])[0]
        if at.isdigit():
            return self._json(200, bundle["shops"].snapshot_at(sid, bundle["catalog"], int(at)),
                              "/shop/api/state")
        return self._json(200, bundle["shops"].snapshot(sid, bundle["catalog"]), "/shop/api/state")

    def _shop_stream(self, query: dict) -> None:
        """SSE: pushes the storefront state each time the voice agent or the shopper changes it."""
        bundle = self._store()
        sid = self._sid(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True
        shops, catalog = bundle["shops"], bundle["catalog"]
        last = -1
        deadline = _time.monotonic() + 15 * 60
        try:
            while _time.monotonic() < deadline:
                snap = shops.snapshot(sid, catalog)
                if snap["version"] != last:
                    last = snap["version"]
                    self._sse("state", snap)
                else:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                shops.wait_for_change(sid, last, 10)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _shop_action(self) -> None:
        """The shopper's own clicks. They follow the same rules the voice agent follows."""
        from voiceshop.catalog import shop as sh

        try:
            bundle = self._store()
            body = self._read_json() or {}
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, "bad_request", str(exc), "/shop/api/action")
        sid = body.get("sid")
        if not isinstance(sid, str) or not _SID.match(sid):
            return self._error(400, "bad_request", "invalid session id", "/shop/api/action")
        action = body.get("action")
        catalog, shops = bundle["catalog"], bundle["shops"]
        pid = body.get("product_id")
        if pid is not None and (not isinstance(pid, str) or pid not in catalog.products):
            return self._error(400, "bad_request", "unknown product", "/shop/api/action")
        try:
            if action == "add":
                shops.mutate(sid, catalog, lambda s: sh.op_add(s, catalog, pid), actor="customer",
                             action="add_to_cart", detail={"product": pid})
            elif action == "remove":
                shops.mutate(sid, catalog, lambda s: sh.op_remove(s, pid), actor="customer",
                             action="remove_from_cart", detail={"product": pid})
            elif action == "navigate":
                page = body.get("page")
                if not isinstance(page, str) or not _PAGE.match(page):
                    return self._error(400, "bad_request", "invalid page", "/shop/api/action")
                shops.mutate(sid, catalog, lambda s: sh.op_navigate(s, page), actor="customer",
                             action="navigate", detail={"page": page})
            elif action == "checkout":
                shops.mutate(sid, catalog, lambda s: sh.op_request_order(s, catalog), actor="customer",
                             action="checkout")
            elif action == "place_order":
                def both(s):
                    if s.pending != "place_order":
                        sh.op_request_order(s, catalog)  # the click itself is the shopper's confirmation
                    return sh.op_place_order(s, catalog)
                shops.mutate(sid, catalog, both, actor="customer", action="place_order")
            elif action == "cancel_order":
                shops.mutate(sid, catalog, sh.op_cancel_order, actor="customer", action="cancel_order")
            elif action in ("takeover", "resume"):
                mode = "interactive" if action == "takeover" else "voice"
                shops.mutate(sid, catalog, lambda s: sh.op_set_mode(s, mode), actor="customer", action=action)
            else:
                return self._error(400, "bad_request", "unknown action", "/shop/api/action")
        except sh.ShopError as exc:
            return self._error(409, exc.code, str(exc), "/shop/api/action")
        return self._json(200, shops.snapshot(sid, catalog), "/shop/api/action")

    def _settings_view(self) -> dict[str, Any]:
        scopes = _store_scopes()
        return {
            "storefront_url": self.app.storefront_url,
            "default_storefront": "this monitor's /shop page",
            "voice_agents": [
                {"id": aid, "name": name, "scopes": sorted(scopes.get(aid, ()))}
                for aid, name in AGENT_NAMES.items()
            ],
        }

    def _settings_save(self) -> None:
        if not (self.app.api_key or self.app.local_ui_no_key):
            return self._error(403, "api_disabled", "saving settings is disabled: VOICESHOP_API_KEY not set",
                               "/api/settings")
        if not (self._local_ui_allowed() or self.app.authorised(self.headers.get("X-API-Key"))):
            return self._error(401, "unauthorised", "missing or invalid API key", "/api/settings")
        try:
            body = self._read_json() or {}
            url = _valid_storefront_url(body.get("storefront_url"))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, "bad_request", str(exc), "/api/settings")
        if self.app.metrics.active_runs > 0:
            return self._error(409, "busy", "a test is running; stop it before changing the storefront address",
                               "/api/settings")
        self.app.storefront_url = url
        if self.app.settings_path is not None:
            _save_settings(self.app.settings_path, url)
        return self._json(200, self._settings_view(), "/api/settings")

    def _history_clear(self) -> None:
        if not (self.app.api_key or self.app.local_ui_no_key):
            return self._error(403, "api_disabled", "clearing history is disabled: VOICESHOP_API_KEY not set",
                               "/api/history/clear")
        if not (self._local_ui_allowed() or self.app.authorised(self.headers.get("X-API-Key"))):
            return self._error(401, "unauthorised", "missing or invalid API key", "/api/history/clear")
        if self.app.metrics.active_runs > 0:  # only work that is really executing in this process blocks a clear
            return self._error(409, "busy", "a test is still running; stop it before clearing history",
                               "/api/history/clear")
        removed = self.app.ctx.runs.clear()
        return self._json(200, {"removed": removed}, "/api/history/clear")

    def _store_items(self) -> list[dict[str, Any]]:
        out = []
        for sc in self._store()["scenarios"]:
            exp = sc.expect
            if exp.get("order"):
                outcome = f"Order placed for ${exp['order']['total']}"
            elif exp.get("no_order"):
                outcome = "No order placed"
            elif exp.get("refund"):
                outcome = f"Refund {exp['refund']['decision']}"
            elif exp.get("act"):
                outcome = exp["act"].replace("_", " ")
            else:
                outcome = "Question answered"
            out.append({
                "id": sc.id, "language": "en", "tags": list(sc.tags), "description": sc.description,
                "intent": sc.goals[0].intent, "expected_outcome": outcome,
                "max_turns": exp.get("max_turns", sc.max_turns),
                "test_types": ["regression"] + [t for t in ("smoke", "targeted") if t in sc.tags],
            })
        return out

    def _static(self, name: str, ctype: str, route: str, extra: dict | None = None) -> None:
        body = resources.files("voiceshop.monitor").joinpath("static", name).read_bytes()
        self._send(200, body, ctype, route, {"Cache-Control": "no-cache", **(extra or {})})

    def _readyz(self) -> None:
        try:
            self.app.ctx.runs.counts()
            ok, detail = True, {"schema_version": SCHEMA_VERSION}
        except Exception as exc:  # readiness must report, not crash
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


def make_server(app: MonitorApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if app.local_ui_no_key and host not in ("127.0.0.1", "localhost", "::1"):
        raise VoiceShopError("VOICESHOP_LOCAL_UI_NO_KEY is only allowed on a loopback address",
                              code="unsafe_binding")
    if host not in ("127.0.0.1", "localhost", "::1") and not app.api_key:
        raise VoiceShopError("refusing to bind a non-loopback address without VOICESHOP_API_KEY",
                              code="unsafe_binding")
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


__all__ = ["MonitorApp", "make_server", "HTTPStatus"]
