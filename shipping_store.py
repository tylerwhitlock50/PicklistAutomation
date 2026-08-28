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

            CREATE TABLE IF NOT EXISTS release_gate_serial_reservations (
                serial_no TEXT PRIMARY KEY,
                part_id TEXT NOT NULL,
                customer_id TEXT,
                cust_order_id TEXT NOT NULL,
                line_no TEXT,
                first_assigned_at TEXT NOT NULL,
                accumulation_started_at TEXT NOT NULL,
                last_verified_at TEXT NOT NULL,
                warehouse_id TEXT,
                location_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                invalidated_at TEXT,
                invalid_reason TEXT,
                released_at TEXT,
                released_run_id INTEGER,
                policy_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_release_serial_reservations_order
                ON release_gate_serial_reservations(status, cust_order_id, part_id);

            CREATE INDEX IF NOT EXISTS idx_release_serial_reservations_customer
                ON release_gate_serial_reservations(status, customer_id, first_assigned_at);

            CREATE TABLE IF NOT EXISTS release_gate_release_log (
                released_date TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                ship_to_id TEXT NOT NULL,
                cust_order_id TEXT NOT NULL,
                run_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY(released_date, customer_id, ship_to_id, cust_order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_release_log_date
                ON release_gate_release_log(released_date);

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
        _prune_evaluations(conn)
        conn.commit()
        return evaluation_id
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_if_owned(conn)


EVALUATION_HISTORY_KEEP = 500


def _prune_evaluations(conn: sqlite3.Connection, keep: Optional[int] = None) -> int:
    """Cap the evaluation audit history; decisions follow via ON DELETE CASCADE."""
    if keep is None:
        keep = EVALUATION_HISTORY_KEEP
    cursor = conn.execute(
        """
        DELETE FROM release_gate_evaluations
        WHERE id NOT IN (
            SELECT id FROM release_gate_evaluations ORDER BY id DESC LIMIT ?
        )
        """,
        (max(1, int(keep)),),
    )
    return cursor.rowcount


def prune_evaluations(keep: Optional[int] = None) -> int:
    conn = _conn()
    try:
        removed = _prune_evaluations(conn, keep)
        conn.commit()
        return removed
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


def serial_reservations_for_gate() -> list[dict[str, Any]]:
    """Return sticky serial assignments still protecting eligible inventory."""
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM release_gate_serial_reservations
            WHERE status = 'active'
            ORDER BY first_assigned_at, serial_no
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def recent_serial_reservations(limit: int = 250) -> list[dict[str, Any]]:
    """Reservation audit feed for management views and troubleshooting."""
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM release_gate_serial_reservations
            ORDER BY last_verified_at DESC, serial_no
            LIMIT ?
            """,
            (max(1, min(int(limit), 2000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def sync_serial_reservations(
    *,
    desired: list[dict[str, Any]],
    live_serials: list[dict[str, Any]],
    evaluated_at: str,
    policy_version: int,
) -> dict[str, int]:
    """Reconcile sticky local assignments with a fresh read-only ERP serial snapshot.

    A valid assignment keeps its original timestamps. A serial is freed only when it
    leaves eligible ERP inventory or the current policy evaluation no longer assigns it
    to that order. Reassignments are retained in this table as invalidated history until
    the serial is selected again, at which point the row becomes active with a new
    ``first_assigned_at`` while the order's original accumulation clock is preserved.
    """
    live_by_serial: dict[str, dict[str, Any]] = {}
    for raw in live_serials:
        serial = str(raw.get("serial_no") or raw.get("SERIAL_NO") or "").strip().upper()
        if not serial:
            continue
        live_by_serial[serial] = raw

    desired_by_serial: dict[str, dict[str, Any]] = {}
    for raw in desired:
        serial = str(raw.get("serial_no") or "").strip().upper()
        order_id = str(raw.get("cust_order_id") or raw.get("order_id") or "").strip().upper()
        part_id = str(raw.get("part_id") or "").strip().upper()
        if serial and order_id and part_id and serial in live_by_serial:
            desired_by_serial[serial] = {**raw, "cust_order_id": order_id, "part_id": part_id}

    counts = {"kept": 0, "assigned": 0, "invalidated": 0, "fulfilled": 0}
    conn = _conn()
    try:
        existing_rows = conn.execute(
            "SELECT * FROM release_gate_serial_reservations"
        ).fetchall()
        existing = {str(row["serial_no"]).upper(): dict(row) for row in existing_rows}

        for serial, row in existing.items():
            if row["status"] != "active":
                continue
            if serial not in live_by_serial:
                conn.execute(
                    """
                    UPDATE release_gate_serial_reservations
                    SET status = 'fulfilled', invalidated_at = ?,
                        invalid_reason = 'left eligible ERP inventory', last_verified_at = ?
                    WHERE serial_no = ? AND status = 'active'
                    """,
                    (evaluated_at, evaluated_at, serial),
                )
                counts["fulfilled"] += 1
            elif serial not in desired_by_serial:
                conn.execute(
                    """
                    UPDATE release_gate_serial_reservations
                    SET status = 'invalidated', invalidated_at = ?,
                        invalid_reason = 'no longer selected by release policy',
                        last_verified_at = ?
                    WHERE serial_no = ? AND status = 'active'
                    """,
                    (evaluated_at, evaluated_at, serial),
                )
                counts["invalidated"] += 1

        for serial, row in desired_by_serial.items():
            live = live_by_serial[serial]
            prior = existing.get(serial)
            same_active_assignment = bool(
                prior
                and prior.get("status") == "active"
                and str(prior.get("cust_order_id") or "").upper() == row["cust_order_id"]
                and str(prior.get("part_id") or "").upper() == row["part_id"]
            )
            if same_active_assignment:
                conn.execute(
                    """
                    UPDATE release_gate_serial_reservations
                    SET customer_id = ?, line_no = ?, last_verified_at = ?,
                        warehouse_id = ?, location_id = ?, policy_version = ?,
                        invalidated_at = NULL, invalid_reason = NULL
                    WHERE serial_no = ?
                    """,
                    (
                        row.get("customer_id"),
                        row.get("line_no"),
                        evaluated_at,
                        live.get("warehouse_id") or live.get("WAREHOUSE_ID"),
                        live.get("location_id") or live.get("LOCATION_ID"),
                        int(policy_version),
                        serial,
                    ),
                )
                counts["kept"] += 1
                continue

            order_clock = conn.execute(
                """
                SELECT MIN(accumulation_started_at) AS started_at
                FROM release_gate_serial_reservations
                WHERE cust_order_id = ?
                """,
                (row["cust_order_id"],),
            ).fetchone()["started_at"]
            first_assigned = str(row.get("first_assigned_at") or evaluated_at)
            accumulation_started = str(
                row.get("accumulation_started_at") or order_clock or first_assigned
            )
            conn.execute(
                """
                INSERT INTO release_gate_serial_reservations
                    (serial_no, part_id, customer_id, cust_order_id, line_no,
                     first_assigned_at, accumulation_started_at, last_verified_at,
                     warehouse_id, location_id, status, invalidated_at,
                     invalid_reason, released_at, released_run_id, policy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(serial_no) DO UPDATE SET
                    part_id = excluded.part_id,
                    customer_id = excluded.customer_id,
                    cust_order_id = excluded.cust_order_id,
                    line_no = excluded.line_no,
                    first_assigned_at = excluded.first_assigned_at,
                    accumulation_started_at = excluded.accumulation_started_at,
                    last_verified_at = excluded.last_verified_at,
                    warehouse_id = excluded.warehouse_id,
                    location_id = excluded.location_id,
                    status = 'active',
                    invalidated_at = NULL,
                    invalid_reason = NULL,
                    released_at = NULL,
                    released_run_id = NULL,
                    policy_version = excluded.policy_version
                """,
                (
                    serial,
                    row["part_id"],
                    row.get("customer_id"),
                    row["cust_order_id"],
                    row.get("line_no"),
                    first_assigned,
                    accumulation_started,
                    evaluated_at,
                    live.get("warehouse_id") or live.get("WAREHOUSE_ID"),
                    live.get("location_id") or live.get("LOCATION_ID"),
                    int(policy_version),
                ),
            )
            counts["assigned"] += 1

        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_if_owned(conn)


RELEASE_LOG_RETENTION_DAYS = 31


def record_released_ship_tos(
    *,
    run_id: Optional[int],
    released_decisions: list[dict[str, Any]],
    released_date: date,
) -> int:
    """Log which customer/ship-to pairs a picklist run released.

    The gate merges this ledger into the ERP ship-to shipment history so a
    picklist generated earlier today triggers the ship-to cooldown even before
    VISUAL records a SHIPPED_DATE. Idempotent per (date, customer, ship-to, order).
    """
    rows = []
    for decision in released_decisions:
        customer_id = str(decision.get("customer_id") or "").strip().upper()
        order_id = str(decision.get("order_id") or decision.get("cust_order_id") or "").strip().upper()
        if not customer_id or not order_id:
            continue
        ship_to_id = str(decision.get("ship_to_id") or "").strip().upper() or "DEFAULT"
        rows.append(
            (
                released_date.isoformat(),
                customer_id,
                ship_to_id,
                order_id,
                int(run_id) if run_id is not None else None,
                _now_iso(),
            )
        )
    if not rows:
        return 0
    conn = _conn()
    try:
        conn.executemany(
            """
            INSERT INTO release_gate_release_log
                (released_date, customer_id, ship_to_id, cust_order_id, run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(released_date, customer_id, ship_to_id, cust_order_id)
                DO NOTHING
            """,
            rows,
        )
        conn.execute(
            "DELETE FROM release_gate_release_log WHERE released_date < date(?, ?)",
            (released_date.isoformat(), f"-{RELEASE_LOG_RETENTION_DAYS} days"),
        )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_if_owned(conn)


def recent_released_ship_tos(days: int = RELEASE_LOG_RETENTION_DAYS) -> list[dict[str, Any]]:
    """Local release history shaped like the ERP ship-to history rows."""
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT customer_id, ship_to_id, MAX(released_date) AS last_released_date
            FROM release_gate_release_log
            WHERE released_date >= date('now', ?)
            GROUP BY customer_id, ship_to_id
            """,
            (f"-{max(1, int(days))} days",),
        ).fetchall()
        return [
            {
                "CUSTOMER_ID": row["customer_id"],
                "SHIP_TO_ID": row["ship_to_id"],
                "LAST_SHIPPED_DATE": row["last_released_date"],
                "SOURCE": "picklist",
            }
            for row in rows
        ]
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
