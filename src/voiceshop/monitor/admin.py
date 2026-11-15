"""Admin routes of the monitor: settings, the scenario list and clearing history. A mixin for the handler."""

from __future__ import annotations

import json
from typing import Any

from voiceshop.catalog.model import Catalog  # noqa: F401  (type of the store bundle)
from voiceshop.monitor.settings import AGENT_NAMES, _save_settings, _store_scopes, _valid_storefront_url


class AdminRoutes:
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
        principal = self._authorise("admin", "/api/settings")
        if principal is None:
            return
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
        self.app.ctx.audit.record("monitor", principal.name, "settings.changed", None, storefront_url=url)
        return self._json(200, self._settings_view(), "/api/settings")

    def _history_clear(self) -> None:
        principal = self._authorise("admin", "/api/history/clear")
        if principal is None:
            return
        if self.app.metrics.active_runs > 0:  # only work that is really executing in this process blocks a clear
            return self._error(409, "busy", "a test is still running; stop it before clearing history",
                               "/api/history/clear")
        removed = self.app.ctx.runs.clear()
        self.app.ctx.audit.record("monitor", principal.name, "history.cleared", None, removed=removed)
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
                "test_types": ["regression"] + [t for t in ("smoke", "targeted", "barge_in", "barge_in_demo") if t in sc.tags],
            })
        return out
