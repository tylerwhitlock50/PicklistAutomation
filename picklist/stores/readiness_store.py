"""SQLite persistence for order-readiness snapshots and the hold punch list.

Holds are reconciled on every refresh: a hold that is still true keeps its
``first_seen_at`` (so age is measurable), a hold that disappeared gets
``cleared_at``, and a brand-new one is returned so the service can notify the
owning team exactly once.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

_get_conn: Optional[Callable[[], sqlite3.Connection]] = None


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readiness_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at TEXT NOT NULL,
                source_as_of TEXT,
                trigger TEXT NOT NULL,
                order_count INTEGER NOT NULL,
                hold_count INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                orders_json TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS readiness_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cust_order_id TEXT NOT NULL,
                line_no TEXT NOT NULL DEFAULT '',
                reason_code TEXT NOT NULL,
                owner_team TEXT NOT NULL,
                blocking INTEGER NOT NULL DEFAULT 1,
                customer_id TEXT,
                customer_name TEXT,
                label TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                cleared_at TEXT,
                cleared_how TEXT,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                notified_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_readiness_holds_open
                ON readiness_holds(cust_order_id, line_no, reason_code)
                WHERE cleared_at IS NULL;

            CREATE INDEX IF NOT EXISTS idx_readiness_holds_owner
                ON readiness_holds(cleared_at, owner_team, cust_order_id);

            CREATE INDEX IF NOT EXISTS idx_readiness_holds_order
                ON readiness_holds(cust_order_id, first_seen_at);

            CREATE TABLE IF NOT EXISTS readiness_hold_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hold_id INTEGER NOT NULL REFERENCES readiness_holds(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_team TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_readiness_hold_events_hold
                ON readiness_hold_events(hold_id, id);

            CREATE TABLE IF NOT EXISTS ffl_doc_cache (
                doc_key TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                doc_path TEXT NOT NULL,
                method TEXT,
                text TEXT,
                parsed_json TEXT,
                error TEXT,
                extracted_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        _close_if_owned(conn)


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("readiness_store.initialize() must be called first")
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _close_if_owned(conn: sqlite3.Connection) -> None:
    if getattr(conn, "_readiness_store_shared", False):
        return
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "detail_json" in data:
        try:
            data["detail"] = json.loads(data.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            data["detail"] = {}
    return data


# --------------------------------------------------------------------------- snapshots


def save_snapshot(payload: dict[str, Any], *, trigger: str, source_as_of: Optional[str] = None,
                  error: Optional[str] = None) -> int:
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO readiness_snapshots
                (evaluated_at, source_as_of, trigger, order_count, hold_count, summary_json, orders_json, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("evaluated_at") or _now_iso(),
                source_as_of,
                trigger,
                int(payload.get("summary", {}).get("orders") or 0),
                int(payload.get("summary", {}).get("holds") or 0),
                json.dumps(payload.get("summary") or {}, sort_keys=True, default=str),
                json.dumps(payload.get("orders") or [], default=str),
                error,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        _close_if_owned(conn)


def latest_snapshot() -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM readiness_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "evaluated_at": row["evaluated_at"],
            "source_as_of": row["source_as_of"],
            "trigger": row["trigger"],
            "order_count": row["order_count"],
            "hold_count": row["hold_count"],
            "summary": json.loads(row["summary_json"] or "{}"),
            "orders": json.loads(row["orders_json"] or "[]"),
            "error": row["error"],
        }
    finally:
        _close_if_owned(conn)


def recent_snapshots(limit: int = 30) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, evaluated_at, trigger, order_count, hold_count, summary_json, error "
            "FROM readiness_snapshots ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["summary"] = json.loads(data.pop("summary_json") or "{}")
            out.append(data)
        return out
    finally:
        _close_if_owned(conn)


def prune_snapshots(keep: int = 200) -> int:
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            DELETE FROM readiness_snapshots
            WHERE id NOT IN (SELECT id FROM readiness_snapshots ORDER BY id DESC LIMIT ?)
            """,
            (int(keep),),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        _close_if_owned(conn)


# --------------------------------------------------------------------------- holds


def reconcile_holds(holds: Iterable[dict[str, Any]], *, evaluated_at: str,
                    seen_orders: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Upsert the open punch list against this evaluation.

    Returns ``{"new": [rows], "cleared": [rows], "kept": n, "ids": {key: id}}``
    where key is ``(order_id, line_no, reason_code)``.
    """
    desired: dict[tuple[str, str, str], dict[str, Any]] = {}
    for hold in holds:
        key = (
            str(hold.get("order_id") or "").upper(),
            str(hold.get("line_no") or ""),
            str(hold.get("reason_code") or ""),
        )
        if key[0] and key[2]:
            desired[key] = hold
    seen = {str(o).upper() for o in seen_orders} if seen_orders is not None else None

    conn = _conn()
    try:
        open_rows = conn.execute(
            "SELECT * FROM readiness_holds WHERE cleared_at IS NULL"
        ).fetchall()
        existing = {
            (row["cust_order_id"], row["line_no"], row["reason_code"]): row for row in open_rows
        }
        new_rows: list[dict[str, Any]] = []
        cleared_rows: list[dict[str, Any]] = []
        ids: dict[tuple[str, str, str], int] = {}
        kept = 0

        for key, hold in desired.items():
            detail_json = json.dumps(hold.get("detail") or {}, sort_keys=True, default=str)
            row = existing.get(key)
            if row is not None:
                conn.execute(
                    """
                    UPDATE readiness_holds
                    SET last_seen_at = ?, detail_json = ?, label = ?, owner_team = ?, blocking = ?,
                        customer_id = COALESCE(?, customer_id), customer_name = COALESCE(?, customer_name)
                    WHERE id = ?
                    """,
                    (
                        evaluated_at, detail_json, hold.get("label") or row["label"],
                        hold.get("owner_team") or row["owner_team"], 1 if hold.get("blocking") else 0,
                        hold.get("customer_id"), hold.get("customer_name"), row["id"],
                    ),
                )
                ids[key] = int(row["id"])
                kept += 1
                continue
            cursor = conn.execute(
                """
                INSERT INTO readiness_holds
                    (cust_order_id, line_no, reason_code, owner_team, blocking, customer_id, customer_name,
                     label, detail_json, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key[0], key[1], key[2], hold.get("owner_team") or "shipping",
                    1 if hold.get("blocking") else 0, hold.get("customer_id"), hold.get("customer_name"),
                    hold.get("label") or key[2], detail_json, evaluated_at, evaluated_at,
                ),
            )
            hold_id = int(cursor.lastrowid)
            ids[key] = hold_id
            new_rows.append({**hold, "id": hold_id, "first_seen_at": evaluated_at})

        for key, row in existing.items():
            if key in desired:
                continue
            how = "order_closed" if (seen is not None and key[0] not in seen) else "erp_resolved"
            conn.execute(
                "UPDATE readiness_holds SET cleared_at = ?, cleared_how = ? WHERE id = ?",
                (evaluated_at, how, row["id"]),
            )
            cleared = _row_dict(row)
            cleared.update({"cleared_at": evaluated_at, "cleared_how": how})
            cleared_rows.append(cleared)

        conn.commit()
        return {"new": new_rows, "cleared": cleared_rows, "kept": kept, "ids": ids}
    finally:
        _close_if_owned(conn)


def open_holds(*, owner_team: Optional[str] = None, cust_order_id: Optional[str] = None,
               limit: int = 2000) -> list[dict[str, Any]]:
    clauses = ["cleared_at IS NULL"]
    params: list[Any] = []
    if owner_team:
        clauses.append("owner_team = ?")
        params.append(owner_team)
    if cust_order_id:
        clauses.append("cust_order_id = ?")
        params.append(str(cust_order_id).upper())
    params.append(int(limit))
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM readiness_holds WHERE {' AND '.join(clauses)} "
            "ORDER BY first_seen_at, cust_order_id, id LIMIT ?",
            params,
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def hold_history(cust_order_id: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM readiness_holds WHERE cust_order_id = ? ORDER BY id DESC LIMIT ?",
            (str(cust_order_id).upper(), int(limit)),
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def get_hold(hold_id: int) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM readiness_holds WHERE id = ?", (int(hold_id),)).fetchone()
        return _row_dict(row) if row else None
    finally:
        _close_if_owned(conn)


def add_hold_event(hold_id: int, event_type: str, *, actor: str, actor_team: Optional[str] = None,
                   note: Optional[str] = None) -> int:
    actor_name = (actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO readiness_hold_events (hold_id, event_type, actor, actor_team, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(hold_id), event_type, actor_name, actor_team, (note or "").strip() or None, _now_iso()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        _close_if_owned(conn)


def acknowledge_hold(hold_id: int, *, actor: str, actor_team: Optional[str] = None,
                     note: Optional[str] = None) -> dict[str, Any]:
    hold = get_hold(hold_id)
    if hold is None:
        raise LookupError(f"hold {hold_id} not found")
    if hold.get("cleared_at"):
        raise ValueError("hold is already cleared")
    actor_name = (actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    now = _now_iso()
    conn = _conn()
    try:
        conn.execute(
            "UPDATE readiness_holds SET acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
            (now, actor_name, int(hold_id)),
        )
        conn.commit()
    finally:
        _close_if_owned(conn)
    add_hold_event(hold_id, "ack", actor=actor_name, actor_team=actor_team, note=note)
    return get_hold(hold_id) or hold


def mark_notified(hold_ids: Iterable[int], *, when: Optional[str] = None) -> None:
    ids = [int(i) for i in hold_ids]
    if not ids:
        return
    conn = _conn()
    try:
        conn.executemany(
            "UPDATE readiness_holds SET notified_at = ? WHERE id = ?",
            [(when or _now_iso(), hold_id) for hold_id in ids],
        )
        conn.commit()
    finally:
        _close_if_owned(conn)


def hold_events(hold_id: int, limit: int = 100) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM readiness_hold_events WHERE hold_id = ? ORDER BY id LIMIT ?",
            (int(hold_id), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def events_for_order(cust_order_id: str, limit: int = 200) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT e.*, h.reason_code, h.label
            FROM readiness_hold_events e
            JOIN readiness_holds h ON h.id = e.hold_id
            WHERE h.cust_order_id = ?
            ORDER BY e.id DESC LIMIT ?
            """,
            (str(cust_order_id).upper(), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


def hold_durations(days: int = 30) -> dict[str, Any]:
    """Age statistics for the scorecard: open hold ages and time-to-clear."""
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    conn = _conn()
    try:
        open_rows = conn.execute(
            "SELECT reason_code, owner_team, first_seen_at FROM readiness_holds WHERE cleared_at IS NULL"
        ).fetchall()
        cleared_rows = conn.execute(
            "SELECT reason_code, owner_team, first_seen_at, cleared_at FROM readiness_holds "
            "WHERE cleared_at IS NOT NULL AND cleared_at >= ?",
            (since,),
        ).fetchall()
    finally:
        _close_if_owned(conn)

    now = datetime.now(timezone.utc)

    def _age_hours(start: str, end: Optional[str] = None) -> float:
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end) if end else now
        except ValueError:
            return 0.0
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)

    def _stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"count": 0, "mean_hours": None, "median_hours": None, "max_hours": None}
        ordered = sorted(values)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        return {
            "count": len(ordered),
            "mean_hours": round(sum(ordered) / len(ordered), 1),
            "median_hours": round(median, 1),
            "max_hours": round(ordered[-1], 1),
        }

    open_by_owner: dict[str, list[float]] = {}
    open_by_reason: dict[str, list[float]] = {}
    for row in open_rows:
        age = _age_hours(row["first_seen_at"])
        open_by_owner.setdefault(row["owner_team"], []).append(age)
        open_by_reason.setdefault(row["reason_code"], []).append(age)
    cleared_by_owner: dict[str, list[float]] = {}
    cleared_by_reason: dict[str, list[float]] = {}
    for row in cleared_rows:
        age = _age_hours(row["first_seen_at"], row["cleared_at"])
        cleared_by_owner.setdefault(row["owner_team"], []).append(age)
        cleared_by_reason.setdefault(row["reason_code"], []).append(age)

    return {
        "window_days": int(days),
        "open": {
            "total": _stats([a for v in open_by_owner.values() for a in v]),
            "by_owner": {k: _stats(v) for k, v in open_by_owner.items()},
            "by_reason": {k: _stats(v) for k, v in open_by_reason.items()},
        },
        "cleared": {
            "total": _stats([a for v in cleared_by_owner.values() for a in v]),
            "by_owner": {k: _stats(v) for k, v in cleared_by_owner.items()},
            "by_reason": {k: _stats(v) for k, v in cleared_by_reason.items()},
        },
    }


# --------------------------------------------------------------------------- FFL document cache (Phase 4)


def get_doc_cache(doc_key: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM ffl_doc_cache WHERE doc_key = ?", (doc_key,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["parsed"] = json.loads(data.pop("parsed_json") or "null")
        except json.JSONDecodeError:
            data["parsed"] = None
        return data
    finally:
        _close_if_owned(conn)


def put_doc_cache(doc_key: str, *, document_id: str, doc_path: str, method: Optional[str],
                  text: Optional[str], parsed: Optional[dict[str, Any]], error: Optional[str]) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO ffl_doc_cache
                (doc_key, document_id, doc_path, method, text, parsed_json, error, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_key, document_id, doc_path, method, text,
                json.dumps(parsed, default=str) if parsed is not None else None,
                error, _now_iso(),
            ),
        )
        conn.commit()
    finally:
        _close_if_owned(conn)
