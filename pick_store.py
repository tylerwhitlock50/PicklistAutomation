"""SQLite-backed pick-confirm sessions.

A pick session snapshots one successful picklist run into scannable lines
(one per order + part + location allocation) and then verifies each item the
picker pulls against that plan. Lives in the same SQLite database as run
history so pick confirm works even when the Postgres audit store is not
configured.

The ERP is only consulted by the caller (app.py) to resolve a scanned serial
to a part + current location; everything in this module is plain SQLite so
the matching rules stay unit-testable.

Scanning is ORDER-FIRST: the picker scans the sales order they are pulling
for, then each item. The point is destination verification — batch pulling
puts the right gun in the wrong customer's box, and part-level matching
alone would never catch that.

  - A scan resolves to one or more candidate parts (a serial can carry a
    lineage: receiver part with no stock plus the finished gun on hand).
  - The item must match an open line ON THE SCANNED ORDER. Right part but
    a different order's gun → wrong_order, and the message says which order
    it actually belongs to.
  - A serial that was already counted in this session is a duplicate — logged
    but never double-counted.
  - The order already has its full quantity of that part → overpick.
  - A part no order on the picklist needs → wrong_item.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Set by initialize(); returns a sqlite3.Connection with row_factory=Row.
_get_conn: Optional[Callable[[], sqlite3.Connection]] = None

RESULT_OK = "ok"
RESULT_DUPLICATE = "duplicate"
RESULT_OVERPICK = "overpick"
RESULT_WRONG_ORDER = "wrong_order"
RESULT_WRONG_ITEM = "wrong_item"
RESULT_UNKNOWN = "unknown"

PROBLEM_RESULTS = ("wrong_order", "wrong_item", "overpick", "unknown")


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                query_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                operator TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                cust_order_id TEXT,
                customer_id TEXT,
                part_id TEXT NOT NULL,
                location TEXT,
                planned_qty INTEGER NOT NULL,
                picked_qty INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(session_id) REFERENCES pick_sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                line_id INTEGER,
                scan_value TEXT NOT NULL,
                serial TEXT,
                part_id TEXT,
                target_order TEXT,
                result TEXT NOT NULL,
                message TEXT,
                operator TEXT,
                scanned_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES pick_sessions(id) ON DELETE CASCADE
            )
            """
        )
        scan_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pick_scans)").fetchall()
        }
        if "target_order" not in scan_columns:
            conn.execute("ALTER TABLE pick_scans ADD COLUMN target_order TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_lines_session ON pick_lines(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_scans_session ON pick_scans(session_id)"
        )


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("pick_store.initialize() has not been called")
    return _get_conn()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def start_session(
    run_id: int,
    query_type: str,
    plan_rows: list[dict],
    operator: Optional[str] = None,
) -> int:
    """Create a session from picklist run rows (one line per order+part+location)."""
    lines: dict[tuple, dict] = {}
    for row in plan_rows:
        order = str(row.get("Cust Order ID") or "").strip()
        part = str(row.get("Part Id") or "").strip()
        if not part:
            continue
        location = str(row.get("Location") or "").strip()
        key = (order.upper(), part.upper(), location.upper())
        entry = lines.setdefault(key, {
            "cust_order_id": order or None,
            "customer_id": str(row.get("Customer ID") or "").strip() or None,
            "part_id": part,
            "location": location or None,
            "planned_qty": 0,
        })
        try:
            entry["planned_qty"] += int(float(row.get("SO Qty") or 0))
        except (TypeError, ValueError):
            pass

    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pick_sessions (run_id, query_type, status, operator, started_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            (run_id, query_type, (operator or "").strip() or None, _now_iso()),
        )
        session_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO pick_lines
                (session_id, cust_order_id, customer_id, part_id, location, planned_qty)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    line["cust_order_id"],
                    line["customer_id"],
                    line["part_id"],
                    line["location"],
                    line["planned_qty"],
                )
                for line in lines.values()
                if line["planned_qty"] > 0
            ],
        )
    return session_id


def get_session(session_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pick_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def recent_sessions(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT COALESCE(SUM(planned_qty), 0) FROM pick_lines WHERE session_id = s.id) AS planned_units,
                   (SELECT COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) FROM pick_lines WHERE session_id = s.id) AS picked_units,
                   (SELECT COUNT(*) FROM pick_scans
                     WHERE session_id = s.id
                       AND result IN ('wrong_order', 'wrong_item', 'overpick', 'unknown')) AS problem_scans
            FROM pick_sessions s
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_lines(session_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM pick_lines
            WHERE session_id = ?
            ORDER BY cust_order_id, part_id, location
            """,
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scans(session_id: int, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT sc.*, pl.cust_order_id AS line_cust_order_id, pl.location AS line_location
            FROM pick_scans sc
            LEFT JOIN pick_lines pl ON pl.id = sc.line_id
            WHERE sc.session_id = ?
            ORDER BY sc.id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_counts(session_id: int) -> dict:
    with _conn() as conn:
        line_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(planned_qty), 0) AS planned_units,
                COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) AS picked_units,
                COALESCE(SUM(CASE WHEN picked_qty >= planned_qty THEN 1 ELSE 0 END), 0) AS lines_complete,
                COUNT(*) AS lines_total
            FROM pick_lines
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        scan_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN result = 'ok' THEN 1 ELSE 0 END), 0) AS ok_scans,
                COALESCE(SUM(CASE WHEN result = 'duplicate' THEN 1 ELSE 0 END), 0) AS duplicate_scans,
                COALESCE(SUM(CASE WHEN result IN ('wrong_order', 'wrong_item', 'overpick', 'unknown') THEN 1 ELSE 0 END), 0) AS problem_scans
            FROM pick_scans
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    counts = {**dict(line_row), **dict(scan_row)}
    counts["remaining_units"] = max(0, counts["planned_units"] - counts["picked_units"])
    return counts


def complete_session(session_id: int) -> dict:
    with _conn() as conn:
        conn.execute(
            "UPDATE pick_sessions SET status = 'completed', completed_at = ? WHERE id = ?",
            (_now_iso(), session_id),
        )
    session = get_session(session_id) or {}
    session["counts"] = compute_counts(session_id)
    return session


# ---------------------------------------------------------------------------
# Scan allocation
# ---------------------------------------------------------------------------
def _serial_already_counted(conn: sqlite3.Connection, session_id: int, serial: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM pick_scans
        WHERE session_id = ? AND serial = ? AND result = 'ok'
        LIMIT 1
        """,
        (session_id, serial),
    ).fetchone()
    return row is not None


def record_scan(
    session_id: int,
    scan_value: str,
    *,
    target_order: Optional[str] = None,
    serial: Optional[str] = None,
    part_candidates: Optional[list[dict]] = None,
    operator: Optional[str] = None,
    unknown: bool = False,
) -> dict:
    """Verify one scan against the scanned order's open lines and log it.

    target_order: the sales order the picker is pulling for (scanned first).
    part_candidates: [{"part_id": str, "locations": [str, ...]}] — the parts
    the scan could represent (from a direct part-ID match or the ERP serial
    lookup), with the bin locations the item is currently on hand in.
    """
    scan_value = _norm(scan_value)
    serial = _norm(serial) or None
    target = _norm(target_order) or None
    candidates = part_candidates or []
    operator = (operator or "").strip() or None

    with _conn() as conn:
        result: str
        message: str
        line_id: Optional[int] = None
        matched_part: Optional[str] = None

        lines = conn.execute(
            """
            SELECT id, cust_order_id, part_id, location, planned_qty, picked_qty
            FROM pick_lines
            WHERE session_id = ?
            ORDER BY cust_order_id, id
            """,
            (session_id,),
        ).fetchall()
        candidate_parts = [_norm(c.get("part_id")) for c in candidates if c.get("part_id")]
        part_lines = [l for l in lines if _norm(l["part_id"]) in candidate_parts]

        if unknown or not candidates:
            result = RESULT_UNKNOWN
            message = f"{scan_value} is not a picklist part and no ERP serial matched."
        elif serial and _serial_already_counted(conn, session_id, serial):
            result = RESULT_DUPLICATE
            message = f"{serial} was already scanned in this session — not counted twice."
        elif not target:
            result = RESULT_WRONG_ORDER
            message = "Scan the sales order barcode first — no order set for this item."
        elif not any(_norm(l["cust_order_id"]) == target for l in lines):
            result = RESULT_WRONG_ORDER
            message = f"{target} is not on this picklist — check the order number."
        else:
            order_lines = [l for l in part_lines if _norm(l["cust_order_id"]) == target]
            open_order_lines = [l for l in order_lines if l["picked_qty"] < l["planned_qty"]]

            if open_order_lines:
                chosen = open_order_lines[0]
                conn.execute(
                    "UPDATE pick_lines SET picked_qty = picked_qty + 1 WHERE id = ?",
                    (chosen["id"],),
                )
                line_id = chosen["id"]
                matched_part = chosen["part_id"]
                result = RESULT_OK
                progress = f"{chosen['picked_qty'] + 1} of {chosen['planned_qty']}"
                loc_bit = f" from {chosen['location']}" if chosen["location"] else ""
                message = (
                    f"{matched_part} is correct for {chosen['cust_order_id']}"
                    f"{loc_bit} — {progress}."
                )
            elif order_lines:
                result = RESULT_OVERPICK
                message = (
                    f"{target} already has all {order_lines[0]['planned_qty']} of "
                    f"{order_lines[0]['part_id']} — this is one too many."
                )
            elif part_lines:
                # Right part, wrong box: tell the picker whose gun this is.
                open_elsewhere = sorted(
                    {l["cust_order_id"] for l in part_lines
                     if l["picked_qty"] < l["planned_qty"] and l["cust_order_id"]}
                )
                shown = part_lines[0]["part_id"]
                result = RESULT_WRONG_ORDER
                if open_elsewhere:
                    belongs = ", ".join(open_elsewhere[:3])
                    message = (
                        f"{target} does not need {shown} — this one belongs to {belongs}."
                    )
                else:
                    message = (
                        f"{target} does not need {shown}, and every order that did "
                        "is already filled."
                    )
            else:
                result = RESULT_WRONG_ITEM
                shown = candidates[0].get("part_id") or scan_value
                message = f"{shown} is not on this picklist at all — wrong item."

        conn.execute(
            """
            INSERT INTO pick_scans
                (session_id, line_id, scan_value, serial, part_id, target_order,
                 result, message, operator, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                line_id,
                scan_value,
                serial,
                matched_part or (candidates[0].get("part_id") if candidates else None),
                target,
                result,
                message,
                operator,
                _now_iso(),
            ),
        )

    line = None
    if line_id is not None:
        with _conn() as conn:
            row = conn.execute("SELECT * FROM pick_lines WHERE id = ?", (line_id,)).fetchone()
        line = dict(row) if row else None

    return {
        "result": result,
        "message": message,
        "line": line,
        "counts": compute_counts(session_id),
    }
