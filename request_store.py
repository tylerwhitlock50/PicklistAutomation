"""Request & exception queue: the structured replacement for the Sales-Shipping chat.

Four request types cover what the chat carried:
  ship_request          "can SO-X ship today?"  -> owned by Shipping, may become a
                         release-gate exception when accepted as an expedite
  hold_exception        the only sanctioned "set it aside": tied to an SO or WO,
                         typed (marketing / vip / international / rework / approved
                         special) and always with an expiry -> becomes a manual hold
  inventory_discrepancy "we can't find it" / "it's in the wrong bin"
  order_problem         wrong tracking, duplicate unit, RMA on the picklist, ...

Pure SQLite store (same pattern as shipping_store.py). Side effects (gate
exceptions, notifications) live in request_service.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

REQUEST_TYPES: dict[str, dict[str, Any]] = {
    "ship_request": {
        "label": "Ship request",
        "owner_team": "shipping",
        "sla_hours": 4,
        "fields": ("needed_by", "expedite", "service_level", "ship_complete"),
        "requires": ("cust_order_id",),
    },
    "hold_exception": {
        "label": "Hold / set-aside",
        "owner_team": "shipping",
        "sla_hours": 8,
        "fields": ("exception_kind", "expires_at", "work_order_id"),
        "requires": (),
    },
    "inventory_discrepancy": {
        "label": "Inventory discrepancy",
        "owner_team": "shipping",
        "sla_hours": 24,
        "fields": ("part_id", "serial_no", "expected_location", "actual_location", "qty"),
        "requires": (),
    },
    "order_problem": {
        "label": "Order problem",
        "owner_team": "sales",
        "sla_hours": 24,
        "fields": ("problem_kind",),
        "requires": (),
    },
}
EXCEPTION_KINDS: dict[str, str] = {
    "marketing": "Marketing build / sample",
    "vip": "VIP or executive review",
    "international": "International cage",
    "rework": "Rework / work-backwards",
    "approved_special": "Approved special (management)",
}
PROBLEM_KINDS: dict[str, str] = {
    "wrong_tracking": "Wrong or duplicate tracking number",
    "duplicate_unit": "Duplicate / extra unit on an order",
    "rma_on_picklist": "RMA showing on the picklist",
    "status_correction": "Order status needs correcting (firm / release / undo)",
    "address_change": "Ship-to or FFL change after packlist",
    "other": "Something else",
}
SERVICE_LEVELS: dict[str, str] = {
    "ground": "Ground",
    "2day": "2nd day air",
    "overnight": "Next day air",
    "pickup": "Customer pickup",
    "other": "Other / see note",
}
STATUSES = ("open", "acknowledged", "in_progress", "done", "declined")
OPEN_STATUSES = ("open", "acknowledged", "in_progress")
TERMINAL_STATUSES = ("done", "declined")
TRANSITIONS: dict[str, set[str]] = {
    "open": {"acknowledged", "in_progress", "done", "declined"},
    "acknowledged": {"in_progress", "done", "declined"},
    "in_progress": {"done", "declined"},
    "done": {"open"},
    "declined": {"open"},
}
PRIORITIES = ("low", "normal", "urgent")
DEFAULT_HOLD_DAYS = 7
MAX_HOLD_DAYS = 30

_get_conn: Optional[Callable[[], sqlite3.Connection]] = None
_sla_overrides: dict[str, int] = {}


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                cust_order_id TEXT,
                work_order_id TEXT,
                customer_id TEXT,
                part_id TEXT,
                serial_no TEXT,
                title TEXT NOT NULL,
                body TEXT,
                fields_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL,
                created_team TEXT NOT NULL DEFAULT '',
                owner_team TEXT NOT NULL,
                assigned_to TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                acknowledged_at TEXT,
                started_at TEXT,
                closed_at TEXT,
                sla_due_at TEXT,
                resolution TEXT,
                linked_exception_id INTEGER,
                linked_manual_hold_id INTEGER,
                linked_hold_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_requests_queue ON requests(status, owner_team, sla_due_at);
            CREATE INDEX IF NOT EXISTS idx_requests_order ON requests(cust_order_id);
            CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);

            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                actor TEXT NOT NULL,
                actor_team TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_request_events_request ON request_events(request_id, id);

            CREATE TABLE IF NOT EXISTS manual_order_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cust_order_id TEXT NOT NULL,
                work_order_id TEXT,
                hold_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                request_id INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                released_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_manual_holds_open
                ON manual_order_holds(released_at, expires_at, cust_order_id);
            """
        )
        conn.commit()
    finally:
        _close_if_owned(conn)


def configure(*, sla_overrides: Optional[dict[str, int]] = None) -> None:
    global _sla_overrides  # noqa: PLW0603
    _sla_overrides = {str(k): int(v) for k, v in (sla_overrides or {}).items()}


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("request_store.initialize() must be called first")
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _close_if_owned(conn: sqlite3.Connection) -> None:
    if getattr(conn, "_request_store_shared", False):
        return
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "fields_json" in data:
        try:
            data["fields"] = json.loads(data.pop("fields_json") or "{}")
        except json.JSONDecodeError:
            data["fields"] = {}
    data["type_label"] = REQUEST_TYPES.get(data.get("request_type", ""), {}).get("label", data.get("request_type"))
    data["is_open"] = data.get("status") in OPEN_STATUSES
    due = _parse_iso(data.get("sla_due_at"))
    data["overdue"] = bool(data["is_open"] and due and due < _now())
    return data


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------- validation


def validate_fields(request_type: str, fields: dict[str, Any], *, cust_order_id: str, work_order_id: str,
                    part_id: str, serial_no: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Normalize type-specific fields; raise ValueError with a user-facing message."""
    meta = REQUEST_TYPES.get(request_type)
    if meta is None:
        raise ValueError(f"Unknown request type '{request_type}'.")
    now = now or _now()
    clean: dict[str, Any] = {}
    if request_type == "ship_request":
        if not cust_order_id:
            raise ValueError("A ship request needs the sales order id.")
        needed_by = _text(fields.get("needed_by"))
        if needed_by:
            try:
                datetime.fromisoformat(needed_by[:10])
            except ValueError as exc:
                raise ValueError("Needed-by must be a date (YYYY-MM-DD).") from exc
            clean["needed_by"] = needed_by[:10]
        clean["expedite"] = _bool(fields.get("expedite"))
        level = _text(fields.get("service_level")).lower()
        if level and level not in SERVICE_LEVELS:
            raise ValueError(f"Service level must be one of {', '.join(SERVICE_LEVELS)}.")
        clean["service_level"] = level or "ground"
        clean["ship_complete"] = _bool(fields.get("ship_complete"))
    elif request_type == "hold_exception":
        if not cust_order_id and not work_order_id:
            raise ValueError("A hold must be tied to a sales order or a work order. No SO, no set-aside.")
        kind = _text(fields.get("exception_kind")).lower()
        if kind not in EXCEPTION_KINDS:
            raise ValueError(f"Hold type must be one of {', '.join(EXCEPTION_KINDS)}.")
        clean["exception_kind"] = kind
        expires_raw = _text(fields.get("expires_at"))
        if expires_raw:
            expires = _parse_iso(expires_raw if "T" in expires_raw else f"{expires_raw}T23:59:00+00:00")
            if expires is None:
                raise ValueError("Hold expiry must be a date (YYYY-MM-DD).")
        else:
            expires = now + timedelta(days=DEFAULT_HOLD_DAYS)
        if expires <= now:
            raise ValueError("Hold expiry must be in the future.")
        if expires > now + timedelta(days=MAX_HOLD_DAYS):
            raise ValueError(f"Holds are limited to {MAX_HOLD_DAYS} days; ask again when it expires.")
        clean["expires_at"] = expires.isoformat()
        clean["work_order_id"] = work_order_id
    elif request_type == "inventory_discrepancy":
        if not part_id and not serial_no:
            raise ValueError("Give at least a part number or a serial number.")
        clean["part_id"] = part_id
        clean["serial_no"] = serial_no
        clean["expected_location"] = _text(fields.get("expected_location")).upper()
        clean["actual_location"] = _text(fields.get("actual_location")).upper()
        qty_raw = _text(fields.get("qty"))
        if qty_raw:
            try:
                clean["qty"] = float(qty_raw)
            except ValueError as exc:
                raise ValueError("Quantity must be a number.") from exc
    elif request_type == "order_problem":
        kind = _text(fields.get("problem_kind")).lower() or "other"
        if kind not in PROBLEM_KINDS:
            raise ValueError(f"Problem type must be one of {', '.join(PROBLEM_KINDS)}.")
        clean["problem_kind"] = kind
    return clean


def default_title(request_type: str, fields: dict[str, Any], *, cust_order_id: str, work_order_id: str,
                  part_id: str, serial_no: str) -> str:
    if request_type == "ship_request":
        level = SERVICE_LEVELS.get(fields.get("service_level", ""), "")
        prefix = "Expedite" if fields.get("expedite") else "Ship"
        suffix = f" ({level})" if fields.get("expedite") and level else ""
        return f"{prefix} {cust_order_id}{suffix}"
    if request_type == "hold_exception":
        target = cust_order_id or work_order_id
        return f"Hold {target}: {EXCEPTION_KINDS.get(fields.get('exception_kind', ''), 'hold')}"
    if request_type == "inventory_discrepancy":
        what = serial_no or part_id
        return f"Can't reconcile {what}"
    if request_type == "order_problem":
        label = PROBLEM_KINDS.get(fields.get("problem_kind", "other"), "Problem")
        return f"{label}{' on ' + cust_order_id if cust_order_id else ''}"
    return request_type


# --------------------------------------------------------------------------- requests


def create_request(
    *,
    request_type: str,
    actor: str,
    actor_team: str = "",
    title: Optional[str] = None,
    body: str = "",
    cust_order_id: Optional[str] = None,
    work_order_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    part_id: Optional[str] = None,
    serial_no: Optional[str] = None,
    fields: Optional[dict[str, Any]] = None,
    priority: str = "normal",
    linked_hold_id: Optional[int] = None,
) -> dict[str, Any]:
    actor_name = _text(actor)
    if not actor_name:
        raise ValueError("Pick your name in the top bar first.")
    meta = REQUEST_TYPES.get(request_type)
    if meta is None:
        raise ValueError(f"Unknown request type '{request_type}'.")
    prio = _text(priority).lower() or "normal"
    if prio not in PRIORITIES:
        raise ValueError("Priority must be low, normal or urgent.")
    so = _text(cust_order_id).upper()
    wo = _text(work_order_id).upper()
    part = _text(part_id).upper()
    serial = _text(serial_no).upper()
    now = _now()
    clean_fields = validate_fields(
        request_type, fields or {}, cust_order_id=so, work_order_id=wo, part_id=part, serial_no=serial, now=now
    )
    title_text = _text(title) or default_title(
        request_type, clean_fields, cust_order_id=so, work_order_id=wo, part_id=part, serial_no=serial
    )
    sla_hours = _sla_overrides.get(request_type, meta["sla_hours"])
    sla_due = (now + timedelta(hours=int(sla_hours))).isoformat() if sla_hours else None
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO requests
                (request_type, status, priority, cust_order_id, work_order_id, customer_id, part_id, serial_no,
                 title, body, fields_json, created_by, created_team, owner_team, created_at, updated_at,
                 sla_due_at, linked_hold_id)
            VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_type, prio, so or None, wo or None, _text(customer_id) or None, part or None, serial or None,
                title_text[:200], _text(body) or None, json.dumps(clean_fields, default=str), actor_name,
                _text(actor_team).lower(), meta["owner_team"], now.isoformat(), now.isoformat(), sla_due,
                linked_hold_id,
            ),
        )
        request_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO request_events (request_id, event_type, from_status, to_status, actor, actor_team, note, created_at)
            VALUES (?, 'created', NULL, 'open', ?, ?, ?, ?)
            """,
            (request_id, actor_name, _text(actor_team).lower() or None, _text(body) or None, now.isoformat()),
        )
        conn.commit()
    finally:
        _close_if_owned(conn)
    return get_request(request_id)  # type: ignore[return-value]


def get_request(request_id: int, *, with_events: bool = True) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
        if row is None:
            return None
        data = _row(row)
        if with_events:
            events = conn.execute(
                "SELECT * FROM request_events WHERE request_id = ? ORDER BY id", (int(request_id),)
            ).fetchall()
            data["events"] = [dict(e) for e in events]
        return data
    finally:
        _close_if_owned(conn)


def _event(conn: sqlite3.Connection, request_id: int, event_type: str, *, actor: str, actor_team: Optional[str],
           note: Optional[str] = None, from_status: Optional[str] = None, to_status: Optional[str] = None) -> None:
    conn.execute(
        """
        INSERT INTO request_events (request_id, event_type, from_status, to_status, actor, actor_team, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (int(request_id), event_type, from_status, to_status, actor, actor_team or None, (note or "").strip() or None, _now_iso()),
    )


def transition(request_id: int, to_status: str, *, actor: str, actor_team: str = "",
               note: Optional[str] = None, resolution: Optional[str] = None) -> dict[str, Any]:
    actor_name = _text(actor)
    if not actor_name:
        raise ValueError("Pick your name in the top bar first.")
    target = _text(to_status).lower()
    if target not in STATUSES:
        raise ValueError(f"Unknown status '{to_status}'.")
    current = get_request(request_id, with_events=False)
    if current is None:
        raise LookupError(f"Request #{request_id} not found.")
    if target not in TRANSITIONS[current["status"]]:
        raise ValueError(f"A request that is {current['status'].replace('_', ' ')} cannot move to {target.replace('_', ' ')}.")
    now = _now_iso()
    stamps: dict[str, Any] = {"status": target, "updated_at": now}
    if target == "acknowledged" and not current.get("acknowledged_at"):
        stamps["acknowledged_at"] = now
    if target == "in_progress":
        stamps["started_at"] = now
        if not current.get("acknowledged_at"):
            stamps["acknowledged_at"] = now
    if target in TERMINAL_STATUSES:
        stamps["closed_at"] = now
        if not current.get("acknowledged_at"):
            stamps["acknowledged_at"] = now
        if resolution is not None:
            stamps["resolution"] = _text(resolution) or None
    if target == "open":
        stamps["closed_at"] = None
        stamps["resolution"] = None
    assignments = ", ".join(f"{key} = ?" for key in stamps)
    conn = _conn()
    try:
        conn.execute(f"UPDATE requests SET {assignments} WHERE id = ?", (*stamps.values(), int(request_id)))
        _event(conn, request_id, "transition", actor=actor_name, actor_team=_text(actor_team).lower(),
               note=note, from_status=current["status"], to_status=target)
        conn.commit()
    finally:
        _close_if_owned(conn)
    return get_request(request_id)  # type: ignore[return-value]


def assign(request_id: int, assignee: str, *, actor: str, actor_team: str = "") -> dict[str, Any]:
    actor_name = _text(actor)
    if not actor_name:
        raise ValueError("Pick your name in the top bar first.")
    current = get_request(request_id, with_events=False)
    if current is None:
        raise LookupError(f"Request #{request_id} not found.")
    who = _text(assignee) or None
    conn = _conn()
    try:
        conn.execute("UPDATE requests SET assigned_to = ?, updated_at = ? WHERE id = ?", (who, _now_iso(), int(request_id)))
        _event(conn, request_id, "assigned", actor=actor_name, actor_team=_text(actor_team).lower(),
               note=f"assigned to {who}" if who else "unassigned")
        conn.commit()
    finally:
        _close_if_owned(conn)
    return get_request(request_id)  # type: ignore[return-value]


def comment(request_id: int, note: str, *, actor: str, actor_team: str = "") -> dict[str, Any]:
    actor_name = _text(actor)
    if not actor_name:
        raise ValueError("Pick your name in the top bar first.")
    if not _text(note):
        raise ValueError("Comment cannot be empty.")
    if get_request(request_id, with_events=False) is None:
        raise LookupError(f"Request #{request_id} not found.")
    conn = _conn()
    try:
        conn.execute("UPDATE requests SET updated_at = ? WHERE id = ?", (_now_iso(), int(request_id)))
        _event(conn, request_id, "comment", actor=actor_name, actor_team=_text(actor_team).lower(), note=note)
        conn.commit()
    finally:
        _close_if_owned(conn)
    return get_request(request_id)  # type: ignore[return-value]


def record_link_event(request_id: int, event_type: str, *, actor: str, actor_team: str = "",
                      note: Optional[str] = None, **links: Optional[int]) -> None:
    conn = _conn()
    try:
        if links:
            assignments = ", ".join(f"{key} = ?" for key in links)
            conn.execute(f"UPDATE requests SET {assignments}, updated_at = ? WHERE id = ?",
                         (*links.values(), _now_iso(), int(request_id)))
        _event(conn, request_id, event_type, actor=_text(actor) or "system", actor_team=_text(actor_team).lower(), note=note)
        conn.commit()
    finally:
        _close_if_owned(conn)


def list_requests(*, status: Optional[str] = None, owner_team: Optional[str] = None,
                  request_type: Optional[str] = None, cust_order_id: Optional[str] = None,
                  created_by: Optional[str] = None, assigned_to: Optional[str] = None,
                  open_only: bool = False, overdue_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(_text(status).lower())
    if open_only:
        clauses.append(f"status IN ({','.join('?' for _ in OPEN_STATUSES)})")
        params.extend(OPEN_STATUSES)
    if owner_team:
        clauses.append("owner_team = ?")
        params.append(_text(owner_team).lower())
    if request_type:
        clauses.append("request_type = ?")
        params.append(_text(request_type))
    if cust_order_id:
        clauses.append("cust_order_id = ?")
        params.append(_text(cust_order_id).upper())
    if created_by:
        clauses.append("LOWER(created_by) = ?")
        params.append(_text(created_by).lower())
    if assigned_to:
        clauses.append("LOWER(assigned_to) = ?")
        params.append(_text(assigned_to).lower())
    if overdue_only:
        clauses.append(f"status IN ({','.join('?' for _ in OPEN_STATUSES)}) AND sla_due_at IS NOT NULL AND sla_due_at < ?")
        params.extend(OPEN_STATUSES)
        params.append(_now_iso())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    conn = _conn()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM requests {where}
            ORDER BY CASE WHEN status IN ('open','acknowledged','in_progress') THEN 0 ELSE 1 END,
                     CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     COALESCE(sla_due_at, created_at), id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        _close_if_owned(conn)


def open_counts() -> dict[str, int]:
    """Open and past-SLA request counts across every team, for the nav badge."""
    now_iso = _now_iso()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS open_count, "
            "SUM(CASE WHEN sla_due_at IS NOT NULL AND sla_due_at < ? THEN 1 ELSE 0 END) AS overdue_count "
            f"FROM requests WHERE status IN ({','.join('?' for _ in OPEN_STATUSES)})",
            (now_iso, *OPEN_STATUSES),
        ).fetchone()
    finally:
        _close_if_owned(conn)
    return {"open": int(row["open_count"] or 0), "overdue": int(row["overdue_count"] or 0)}


def queue_summary(days: int = 30) -> dict[str, Any]:
    since = (_now() - timedelta(days=int(days))).isoformat()
    now_iso = _now_iso()
    conn = _conn()
    try:
        open_rows = conn.execute(
            f"SELECT request_type, owner_team, priority, sla_due_at FROM requests WHERE status IN ({','.join('?' for _ in OPEN_STATUSES)})",
            OPEN_STATUSES,
        ).fetchall()
        acked = conn.execute(
            "SELECT created_at, acknowledged_at, closed_at, request_type FROM requests WHERE created_at >= ? AND acknowledged_at IS NOT NULL",
            (since,),
        ).fetchall()
        created_count = conn.execute("SELECT COUNT(*) FROM requests WHERE created_at >= ?", (since,)).fetchone()[0]
    finally:
        _close_if_owned(conn)

    by_team: dict[str, int] = {}
    by_type: dict[str, int] = {}
    overdue = 0
    urgent = 0
    for row in open_rows:
        by_team[row["owner_team"]] = by_team.get(row["owner_team"], 0) + 1
        by_type[row["request_type"]] = by_type.get(row["request_type"], 0) + 1
        if row["sla_due_at"] and row["sla_due_at"] < now_iso:
            overdue += 1
        if row["priority"] == "urgent":
            urgent += 1

    def _hours(start: Any, end: Any) -> Optional[float]:
        a, b = _parse_iso(start), _parse_iso(end)
        if a is None or b is None:
            return None
        return max(0.0, (b - a).total_seconds() / 3600.0)

    ack_hours = [h for h in (_hours(r["created_at"], r["acknowledged_at"]) for r in acked) if h is not None]
    close_hours = [h for h in (_hours(r["created_at"], r["closed_at"]) for r in acked if r["closed_at"]) if h is not None]

    def _median(values: list[float]) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        return round(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2, 1)

    return {
        "window_days": int(days),
        "open": len(open_rows),
        "overdue": overdue,
        "urgent": urgent,
        "by_team": by_team,
        "by_type": by_type,
        "created_in_window": int(created_count),
        "median_hours_to_ack": _median(ack_hours),
        "median_hours_to_close": _median(close_hours),
    }


# --------------------------------------------------------------------------- manual holds


def add_manual_hold(*, cust_order_id: str, hold_kind: str, reason: str, expires_at: str, created_by: str,
                    work_order_id: Optional[str] = None, request_id: Optional[int] = None) -> int:
    so = _text(cust_order_id).upper()
    wo = _text(work_order_id).upper()
    kind = _text(hold_kind).lower()
    if not so and not wo:
        raise ValueError("A hold must be tied to a sales order or work order.")
    if kind not in EXCEPTION_KINDS:
        raise ValueError(f"Hold type must be one of {', '.join(EXCEPTION_KINDS)}.")
    if not _text(created_by):
        raise ValueError("created_by is required")
    expires = _parse_iso(expires_at)
    if expires is None or expires <= _now():
        raise ValueError("expires_at must be a future ISO datetime")
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO manual_order_holds
                (cust_order_id, work_order_id, hold_kind, reason, request_id, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (so or wo, wo or None, kind, _text(reason) or EXCEPTION_KINDS[kind], request_id, _text(created_by), _now_iso(), expires.isoformat()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        _close_if_owned(conn)


def release_manual_hold(hold_id: int, *, actor: str) -> bool:
    conn = _conn()
    try:
        cursor = conn.execute(
            "UPDATE manual_order_holds SET released_at = ?, released_by = ? WHERE id = ? AND released_at IS NULL",
            (_now_iso(), _text(actor) or "system", int(hold_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        _close_if_owned(conn)


def active_manual_holds(as_of: Optional[datetime] = None) -> list[dict[str, Any]]:
    instant = (as_of or _now()).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM manual_order_holds WHERE released_at IS NULL AND expires_at > ? ORDER BY expires_at, id",
            (instant,),
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["kind_label"] = EXCEPTION_KINDS.get(data["hold_kind"], data["hold_kind"])
            out.append(data)
        return out
    finally:
        _close_if_owned(conn)


def expire_manual_holds(as_of: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Mark overdue holds released (by 'expired'); returns the rows that expired."""
    instant = (as_of or _now()).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM manual_order_holds WHERE released_at IS NULL AND expires_at <= ?", (instant,)
        ).fetchall()
        expired = [dict(r) for r in rows]
        if expired:
            conn.executemany(
                "UPDATE manual_order_holds SET released_at = ?, released_by = 'expired' WHERE id = ?",
                [(instant, r["id"]) for r in expired],
            )
            conn.commit()
        return expired
    finally:
        _close_if_owned(conn)


def get_manual_hold(hold_id: int) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM manual_order_holds WHERE id = ?", (int(hold_id),)).fetchone()
        return dict(row) if row else None
    finally:
        _close_if_owned(conn)


def manual_holds_for_order(cust_order_id: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM manual_order_holds WHERE cust_order_id = ? ORDER BY id DESC LIMIT ?",
            (_text(cust_order_id).upper(), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        _close_if_owned(conn)
