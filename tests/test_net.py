"""The shared JSON client: retries only what is worth retrying, and never leaks headers into errors."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from voiceshop.net import ServiceError, post_json


def _server(responses: list[tuple[int, dict]]):
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"body": json.loads(self.rfile.read(n) or b"{}"), "key": self.headers.get("x-goog-api-key")})
            status, payload = responses[min(len(seen) - 1, len(responses) - 1)]
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen, f"http://127.0.0.1:{srv.server_address[1]}/x"


def test_rate_limits_are_retried_until_the_service_answers():
    srv, seen, url = _server([(429, {"error": "slow down"}), (503, {"error": "busy"}), (200, {"ok": True})])
    try:
        out = post_json(url, {"q": 1}, {"x-goog-api-key": "secret-value"}, base_delay_s=0)
        assert out == {"ok": True} and len(seen) == 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_bad_request_is_not_retried_and_the_key_is_not_in_the_error():
    srv, seen, url = _server([(400, {"error": "bad"}), (200, {"ok": True})])
    try:
        with pytest.raises(ServiceError) as err:
            post_json(url, {"q": 1}, {"x-goog-api-key": "secret-value"}, base_delay_s=0)
        assert err.value.status == 400 and len(seen) == 1
        assert "secret-value" not in str(err.value)
    finally:
        srv.shutdown()
        srv.server_close()


def test_giving_up_after_the_last_attempt_reports_the_status():
    srv, seen, url = _server([(429, {"error": "slow"})])
    try:
        with pytest.raises(ServiceError) as err:
            post_json(url, {"q": 1}, {}, attempts=3, base_delay_s=0)
        assert err.value.status == 429 and len(seen) == 3
    finally:
        srv.shutdown()
        srv.server_close()
