"""Storefront routes of the monitor: the shop page's state, live stream and actions.

A mixin for the request handler. The storefront is open to shoppers: its actions change only in-memory
sessions held by the storefront, never the back office. Every change is audited.
"""

from __future__ import annotations

import json
import re
import time as _time
import uuid

from voiceshop.monitor.security import SECURITY_HEADERS

_SID = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")  # \Z: no trailing newline allowed
_PAGE = re.compile(r"(home|cart|checkout|category:[a-z_]{1,40}|product:[a-z0-9-]{1,40})\Z")


class ShopRoutes:
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
