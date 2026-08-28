"""SQLite persistence for shipping KPI snapshots and release-gate audit records."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional


_get_conn: Optional[Callable[[], sqlite3.Connection]] = None


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_gate_policies (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                due_override_days INTEGER NOT NULL,
                customer_policies_json TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS release_gate_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at TEXT NOT NULL,
                source_as_of TEXT,
                mode TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                summary_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS release_gate_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id INTEGER NOT NULL,
                cust_order_id TEXT NOT NULL,
                customer_id TEXT,
                decision TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                label TEXT NOT NULL,
                open_qty REAL NOT NULL,
                ready_qty REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                FOREIGN KEY(evaluation_id) REFERENCES release_gate_evaluations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_release_decisions_eval
                ON release_gate_decisions(evaluation_id, decision, cust_order_id);

            CREATE TABLE IF NOT EXISTS release_gate_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cust_order_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                revoked_by TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_release_exceptions_order
                ON release_gate_exceptions(cust_order_id, expires_at, revoked_at);

            CREATE TABLE IF NOT EXISTS shipping_metric_snapshots (
                snapshot_date TEXT NOT NULL,
                period_days INTEGER NOT NULL,
                source_as_of TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(snapshot_date, period_days)
            );
            """
        )
        conn.commit()
    finally:
        _close_if_owned(conn)

def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("shipping_store.initialize() must be called first")
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _close_if_owned(conn: sqlite3.Connection) -> None:
    # App connections are fresh per call; unit tests deliberately return one shared
    # in-memory connection. sqlite3 exposes no ownership marker, so callers may set it.
    if getattr(conn, "_shipping_store_shared", False):
        return
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_policy_config(
    *,
    mode: str,
    due_override_days: int,
    customer_policies: dict[str, dict],
    changed_by: str,
) -> int:
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode not in {"off", "advisory", "enforced"}:
        raise ValueError("mode must be off, advisory, or enforced")
    if int(due_override_days) < 0:
        raise ValueError("due_override_days must be zero or greater")
    actor = (changed_by or "").strip()
    if not actor:
        raise ValueError("changed_by is required")
    normalized = {
        str(customer).strip().upper(): dict(policy)
        for customer, policy in customer_policies.items()
        if str(customer).strip()
    }
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO release_gate_policies
                (mode, due_override_days, customer_policies_json, changed_by, changed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalized_mode,
                int(due_override_days),
                json.dumps(normalized, sort_keys=True),
                actor,
                _now_iso(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        _close_if_owned(conn)


def latest_policy_config() -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM release_gate_policies ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "version": row["version"],
            "mode": row["mode"],
            "due_override_days": row["due_override_days"],
            "customer_policies": json.loads(row["customer_policies_json"] or "{}"),
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
        }
    finally:
        _close_if_owned(conn)


def record_evaluation(payload: dict, source_as_of: Optional[str] = None) -> int:
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO release_gate_evaluations
                (evaluated_at, source_as_of, mode, policy_version, summary_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload["evaluated_at"],
                source_as_of,
                payload["mode"],
                int(payload.get("policy_version") or 1),
                json.dumps(payload.get("summary") or {}, sort_keys=True),
            ),
        )
        evaluation_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO release_gate_decisions
                (evaluation_id, cust_order_id, customer_id, decision, reason_code,
                 label, open_qty, ready_qty, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    evaluation_id,
                    row["order_id"],
                    row.get("customer_id"),
                    row["decision"],
                    row["reason_code"],
                    row["label"],
                    float(row.get("open_qty") or 0),
                    float(row.get("ready_qty") or 0),
                    json.dumps(row, sort_keys=True),
                )
                for row in payload.get("decisions") or []
            ],
        )
        conn.commit()
        return evaluation_id
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_if_owned(conn)


def latest_evaluation() -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM release_gate_evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        decisions = conn.execute(
            "SELECT evidence_json FROM release_gate_decisions WHERE evaluation_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "evaluated_at": row["evaluated_at"],
            "source_as_of": row["source_as_of"],
            "mode": row["mode"],
            "policy_version": row["policy_version"],
            "summary": json.loads(row["summary_json"] or "{}"),
            "decisions": [json.loads(item["evidence_json"]) for item in decisions],
        }
    finally:
        _close_if_owned(conn)


def add_exception(
    *,
    cust_order_id: str,
    reason: str,
    created_by: str,
    expires_at: str,
) -> int:
    order_id = (cust_order_id or "").strip().upper()
    actor = (created_by or "").strip()
    reason_text = (reason or "").strip()
    if not order_id or not actor or not reason_text:
        raise ValueError("cust_order_id, reason, and created_by are required")
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("expires_at must be an ISO datetime") from exc
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO release_gate_exceptions
                (cust_order_id, reason, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, reason_text, actor, _now_iso(), expires.isoformat()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        _close_if_owned(conn)


def active_exceptions(as_of: Optional[datetime] = None) -> list[dict[str, Any]]:
    instant = as_of or datetime.now(timezone.utc)
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM release_gate_exceptions
            WHERE revoked_at IS NULL AND expires_at >= ?
            ORDER BY expires_at, cust_order_id
            """,
            (instant.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def revoke_exception(exception_id: int, revoked_by: str) -> bool:
    actor = (revoked_by or "").strip()
    if not actor:
        raise ValueError("revoked_by is required")
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            UPDATE release_gate_exceptions
            SET revoked_at = ?, revoked_by = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (_now_iso(), actor, int(exception_id)),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        _close_if_owned(conn)


def save_metric_snapshot(
    *,
    snapshot_date: date,
    period_days: int,
    payload: dict,
    source_as_of: Optional[str],
) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO shipping_metric_snapshots
                (snapshot_date, period_days, source_as_of, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, period_days) DO UPDATE SET
                source_as_of = excluded.source_as_of,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                snapshot_date.isoformat(),
                int(period_days),
                source_as_of,
                json.dumps(payload, sort_keys=True),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        _close_if_owned(conn)


def get_metric_snapshot(snapshot_date: date, period_days: int) -> Optional[dict]:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT payload_json FROM shipping_metric_snapshots
            WHERE snapshot_date = ? AND period_days = ?
            """,
            (snapshot_date.isoformat(), int(period_days)),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    finally:
        _close_if_owned(conn)
