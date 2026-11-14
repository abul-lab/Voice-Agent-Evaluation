"""Repositories for the audit trail, runs and conversation turns."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from voiceshop.storage.db import session


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AuditLog:
    """Append-only audit trail of every state change. Never updated or deleted by the app."""

    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def record(self, tenant_id: str, actor: str, action: str, entity_id: str | None, **detail: Any) -> None:
        with session(self._db) as conn:
            conn.execute(
                "INSERT INTO audit_events (ts, tenant_id, actor, action, entity_id, detail) VALUES (?,?,?,?,?,?)",
                (_now(), tenant_id, actor, action, entity_id, json.dumps(detail, default=str)),
            )

    def list(self, tenant_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return self.since(tenant_id, after_id)

    def since(self, tenant_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        """Audit entries for one tenant with id greater than after_id, in order. Used for live streaming."""
        with session(self._db) as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, entity_id, detail FROM audit_events "
                "WHERE tenant_id=? AND id>? ORDER BY id",
                (tenant_id, after_id),
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


class RefundLedger:
    """Approved refunds, one per order within a scope. Checking and recording happen in one transaction."""

    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def issue(self, scope: str, order_id: str, amount: int, policy_version: str, actor: str) -> tuple[dict[str, Any], bool]:
        """Returns (record, duplicate). The first call approves; later calls return the original record."""
        with session(self._db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM refunds WHERE scope=? AND order_id=?", (scope, order_id)).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return dict(row), True
                record = {"scope": scope, "order_id": order_id, "amount": amount,
                          "policy_version": policy_version, "status": "approved", "actor": actor, "created_at": _now()}
                conn.execute(
                    "INSERT INTO refunds (scope, order_id, amount, policy_version, status, actor, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (scope, order_id, amount, policy_version, "approved", actor, record["created_at"]),
                )
                conn.execute("COMMIT")
                return record, False
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get(self, scope: str, order_id: str) -> dict[str, Any] | None:
        with session(self._db) as conn:
            row = conn.execute("SELECT * FROM refunds WHERE scope=? AND order_id=?", (scope, order_id)).fetchone()
        return dict(row) if row else None


class RunRepository:
    """Persistence for simulation runs and their turn-by-turn transcripts (read by the monitor)."""

    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def create(self, run_id: str, scenario_id: str, mode: str, source: str, batch_id: str | None) -> None:
        with session(self._db) as conn:
            conn.execute(
                """INSERT INTO runs (id, batch_id, scenario_id, mode, source, status, created_at)
                   VALUES (?,?,?,?,?,'running',?)""",
                (run_id, batch_id, scenario_id, mode, source, _now()),
            )

    def append_turn(self, run_id: str, turn: Any) -> None:
        data = asdict(turn)
        with session(self._db) as conn:
            conn.execute(
                """INSERT INTO turns (run_id, seq, speaker, language, text_spoken, text_heard, act, intent,
                   latency_ms, created_at, agent, shop_version, stages) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    data["seq"],
                    data["speaker"],
                    data["language"],
                    data["text_spoken"],
                    data["text_heard"],
                    data["act"],
                    data["intent"],
                    data["latency_ms"],
                    data["created_at"],
                    data.get("agent"),
                    data.get("shop_version"),
                    json.dumps(data.get("stages")) if data.get("stages") else None,
                ),
            )

    def finish(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with session(self._db) as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, summary=? WHERE id=?",
                (status, _now(), json.dumps(summary, default=str), run_id),
            )

    def list(self, limit: int = 50, batch_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if batch_id:
            sql += " WHERE batch_id=?"
            params.append(batch_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with session(self._db) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_dict(r) for r in rows]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with session(self._db) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_dict(row) if row else None

    def turns(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with session(self._db) as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear(self) -> int:
        """Delete all run records and their turns. Returns the number of runs removed.
        The audit trail is not touched: it is a compliance record, not a view of history."""
        with session(self._db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                conn.execute("DELETE FROM turns")
                conn.execute("DELETE FROM runs")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return n

    def prune_older_than(self, days: int, *, dry_run: bool = False) -> dict[str, int]:
        """Delete run transcripts older than ``days``. The audit trail is never removed (it is a record of
        what happened, kept under its own policy). Returns how many runs and turns were, or would be, removed."""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with session(self._db) as conn:
            runs = conn.execute("SELECT COUNT(*) FROM runs WHERE created_at < ? AND status != 'running'",
                                (cutoff,)).fetchone()[0]
            turns = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE run_id IN (SELECT id FROM runs WHERE created_at < ? "
                "AND status != 'running')", (cutoff,)).fetchone()[0]
            if not dry_run and runs:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM runs WHERE created_at < ? AND status != 'running'", (cutoff,))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return {"runs": runs, "turns": turns, "cutoff": cutoff}

    def reconcile_interrupted(self) -> int:
        """At start-up no suite is running in this process, so any run still marked running was cut off by a
        crash or a stop. Mark those as errors so the monitor never shows work that has stopped. Returns the count."""
        with session(self._db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT id FROM runs WHERE status='running'").fetchall()
                conn.execute(
                    "UPDATE runs SET status='error', finished_at=?, summary=? WHERE status='running'",
                    (_now(), json.dumps({"error": "interrupted", "detail": "the server stopped before this run finished"})),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    def running_count(self) -> int:
        with session(self._db) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]

    def counts(self) -> dict[str, int]:
        with session(self._db) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM runs GROUP BY status").fetchall()
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        out = {r["status"]: r["n"] for r in rows}
        out["turns"] = turns
        return out

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["summary"] = json.loads(d["summary"] or "{}")
        return d
