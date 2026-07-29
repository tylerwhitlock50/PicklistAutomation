"""SQLite-backed audit trail for Promise Del Date changes.

The allocation screen is the app's only writer to the ERP (one column:
CUST_ORDER_LINE.PROMISE_DEL_DATE). Every save records who changed what, when,
old -> new, and the allocation position before/after, in the same SQLite
database as run history — deliberately NOT the optional Postgres audit store,
because a save must hard-fail when its audit row cannot be written, and the
local SQLite file is always present.

Write ordering lives in app.py's save handler: the ERP UPDATE and this audit
insert happen inside the ERP transaction window (audit failure rolls the ERP
back; an ERP commit failure triggers delete_change as a compensating
removal). This module is plain SQLite so those rules stay unit-testable.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Set by initialize(); returns a sqlite3.Connection with row_factory=Row.
_get_conn: Optional[Callable[[], sqlite3.Connection]] = None


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promise_del_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                cust_order_id TEXT NOT NULL,
                line_no INTEGER NOT NULL,
                part_id TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                position_before INTEGER,
                position_after INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pda_part"
            " ON promise_del_audit(part_id, changed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pda_order"
            " ON promise_del_audit(cust_order_id, line_no)"
        )


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("allocation_store.initialize() has not been called")
    return _get_conn()


def record_change(
    *,
    changed_by: str,
    cust_order_id: str,
    line_no: int,
    part_id: str,
    old_value: Optional[str],
    new_value: Optional[str],
    reason: Optional[str] = None,
    position_before: Optional[int] = None,
    position_after: Optional[int] = None,
) -> int:
    """Insert one audit row and return its id. changed_by must be a real name."""
    changed_by = (changed_by or "").strip()
    if not changed_by:
        raise ValueError("changed_by is required for the Promise Del audit trail")
    if not cust_order_id or not part_id:
        raise ValueError("cust_order_id and part_id are required")
    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO promise_del_audit (
                changed_at, changed_by, cust_order_id, line_no, part_id,
                old_value, new_value, reason, position_before, position_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                changed_by,
                cust_order_id,
                int(line_no),
                part_id,
                old_value,
                new_value,
                (reason or "").strip() or None,
                position_before,
                position_after,
            ),
        )
        return int(cursor.lastrowid)


def set_position_after(audit_id: int, position_after: Optional[int]) -> None:
    """Best-effort post-commit patch of the recomputed position."""
    with _conn() as conn:
        conn.execute(
            "UPDATE promise_del_audit SET position_after = ? WHERE id = ?",
            (position_after, audit_id),
        )


def delete_change(audit_id: int) -> None:
    """Compensating removal when the ERP commit fails after the audit insert."""
    with _conn() as conn:
        conn.execute("DELETE FROM promise_del_audit WHERE id = ?", (audit_id,))


def recent_changes(
    part_id: Optional[str] = None,
    cust_order_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if part_id:
        clauses.append("part_id = ?")
        params.append(part_id)
    if cust_order_id:
        clauses.append("cust_order_id = ?")
        params.append(cust_order_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 200)))
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, changed_at, changed_by, cust_order_id, line_no, part_id,
                   old_value, new_value, reason, position_before, position_after
            FROM promise_del_audit
            {where}
            ORDER BY changed_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]
