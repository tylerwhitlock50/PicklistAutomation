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

MAX_ORDERS_PER_SESSION = 3

# Set by initialize(); returns a sqlite3.Connection with row_factory=Row.
_get_conn: Optional[Callable[[], sqlite3.Connection]] = None

RESULT_OK = "ok"
RESULT_DUPLICATE = "duplicate"
RESULT_OVERPICK = "overpick"
RESULT_WRONG_ORDER = "wrong_order"
RESULT_WRONG_ITEM = "wrong_item"
RESULT_WRONG_LOCATION = "wrong_location"
RESULT_WRONG_TOTE = "wrong_tote"
RESULT_WRONG_OPERATOR = "wrong_operator"
RESULT_UNKNOWN = "unknown"

PROBLEM_RESULTS = (
    "wrong_order", "wrong_item", "wrong_location", "wrong_tote",
    "wrong_operator", "overpick", "unknown"
)


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                cust_order_id TEXT NOT NULL,
                customer_id TEXT,
                tote_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'picking',
                completed_at TEXT,
                UNIQUE(session_id, cust_order_id),
                FOREIGN KEY(session_id) REFERENCES pick_sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                cust_order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT,
                operator TEXT,
                from_operator TEXT,
                to_operator TEXT,
                created_at TEXT NOT NULL,
                details_json TEXT,
                FOREIGN KEY(session_id) REFERENCES pick_sessions(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(conn, "pick_sessions", "source_runs_json", "TEXT")
        _ensure_column(conn, "pick_sessions", "workflow_mode", "TEXT NOT NULL DEFAULT 'legacy'")
        _ensure_column(conn, "pick_scans", "checked_upc", "TEXT")
        _ensure_column(conn, "pick_lines", "item_type", "TEXT NOT NULL DEFAULT 'guns'")
        _ensure_column(conn, "pick_lines", "upc", "TEXT")
        _ensure_column(conn, "pick_scans", "target_order", "TEXT")
        _ensure_column(conn, "pick_scans", "request_id", "TEXT")
        _ensure_column(conn, "pick_sessions", "closed_by", "TEXT")
        _ensure_column(conn, "pick_sessions", "closed_reason", "TEXT")
        _ensure_column(conn, "pick_scans", "scanned_tote", "TEXT")
        _ensure_column(conn, "pick_scans", "scanned_location", "TEXT")
        _ensure_column(conn, "pick_orders", "packlist_id", "TEXT")
        _ensure_column(conn, "pick_orders", "packed_at", "TEXT")
        _ensure_column(conn, "pick_orders", "assigned_operator", "TEXT")
        _ensure_column(conn, "pick_orders", "tote_barcode", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_lines_session ON pick_lines(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_scans_session ON pick_scans(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_orders_session ON pick_orders(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_orders_order ON pick_orders(cust_order_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pick_order_events_session ON pick_order_events(session_id, id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pick_scan_request ON pick_scans(session_id, request_id) WHERE request_id IS NOT NULL"
        )
        conn.execute(
            """
            UPDATE pick_orders
            SET assigned_operator = (
                SELECT operator FROM pick_sessions WHERE pick_sessions.id = pick_orders.session_id
            )
            WHERE assigned_operator IS NULL
            """
        )
        conn.execute(
            """
            UPDATE pick_orders
            SET tote_barcode = 'PICK-' || session_id || '-' || tote_code
            WHERE tote_barcode IS NULL
            """
        )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def start_order_session(
    *,
    plan_rows: list[dict],
    selected_orders: list[str],
    source_runs: dict[str, int],
    operator: Optional[str] = None,
    pick_type: Optional[str] = None,
) -> int:
    """Claim one to three orders and snapshot only their pick lines."""
    operator_name = (operator or "").strip()
    if not operator_name:
        raise ValueError("Enter your name or initials before claiming orders.")
    selected = list(dict.fromkeys(_norm(order) for order in selected_orders if _norm(order)))
    if not selected:
        raise ValueError("Select at least one order.")
    if len(selected) > MAX_ORDERS_PER_SESSION:
        raise ValueError(
            f"A picker can work on at most {MAX_ORDERS_PER_SESSION} orders at a time."
        )

    selected_set = set(selected)
    if pick_type not in (None, "guns", "components"):
        raise ValueError("Choose guns or components.")
    if pick_type == "guns" and len(selected) != 1:
        raise ValueError("Gun picking requires exactly one order per cart.")
    customers: dict[str, Optional[str]] = {}
    lines: dict[tuple[str, str, str, str, str], dict] = {}
    for row in plan_rows:
        order = _norm(row.get("Cust Order ID"))
        if order not in selected_set:
            continue
        part = _norm(row.get("Part Id"))
        if not part:
            continue
        customer = str(row.get("Customer ID") or "").strip() or None
        location = _norm(row.get("Location"))
        item_type = str(row.get("_query_type") or "guns").strip().lower()
        if pick_type and item_type != pick_type:
            continue
        upc = _norm(row.get("UPC") or row.get("GTIN") or row.get("Barcode"))
        customers.setdefault(order, customer)
        key = (order, part, location, item_type, upc)
        line = lines.setdefault(
            key,
            {
                "cust_order_id": order,
                "customer_id": customer,
                "part_id": part,
                "location": location or None,
                "item_type": item_type,
                "upc": upc or None,
                "planned_qty": 0,
            },
        )
        try:
            line["planned_qty"] += int(float(row.get("SO Qty") or 0))
        except (TypeError, ValueError):
            pass

    missing = [order for order in selected if order not in customers]
    if missing:
        raise ValueError(f"These orders are no longer available: {', '.join(missing)}")

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active_for_operator = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM pick_orders po
            JOIN pick_sessions ps ON ps.id = po.session_id
            WHERE ps.status = 'active' AND po.status = 'picking'
              AND UPPER(TRIM(COALESCE(po.assigned_operator, ps.operator, ''))) = ?
            """,
            (_norm(operator_name),),
        ).fetchone()["count"]
        if active_for_operator + len(selected) > MAX_ORDERS_PER_SESSION:
            available = max(0, MAX_ORDERS_PER_SESSION - active_for_operator)
            raise ValueError(
                f"You already have {active_for_operator} active order(s); "
                f"you can claim {available} more."
            )
        if pick_type == "guns" and conn.execute(
            """SELECT 1 FROM pick_orders po JOIN pick_sessions ps ON ps.id = po.session_id
               WHERE ps.status = 'active' AND po.status = 'picking'
                 AND ps.query_type IN ('guns', 'mixed')
                 AND UPPER(TRIM(COALESCE(po.assigned_operator, ps.operator, ''))) = ?""",
            (_norm(operator_name),),
        ).fetchone():
            raise ValueError("Resume or finish your active gun order before starting another.")
        placeholders = ",".join("?" for _ in selected)
        claimed = conn.execute(
            f"""
            SELECT DISTINCT po.cust_order_id
            FROM pick_orders po
            JOIN pick_sessions ps ON ps.id = po.session_id
            WHERE po.cust_order_id IN ({placeholders})
              AND (? IS NULL OR ps.query_type IN (?, 'mixed'))
              AND (
                    (ps.status = 'active' AND po.status = 'picking')
                    OR po.status IN ('ready_for_pack', 'packing', 'exception')
                  )
            """,
            [*selected, pick_type, pick_type],
        ).fetchall()
        legacy_claimed = conn.execute(
            f"""
            SELECT DISTINCT pl.cust_order_id
            FROM pick_lines pl
            JOIN pick_sessions ps ON ps.id = pl.session_id
            WHERE pl.cust_order_id IN ({placeholders})
              AND (? IS NULL OR ps.query_type IN (?, 'mixed'))
              AND ps.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM pick_orders po WHERE po.session_id = ps.id
              )
            """,
            [*selected, pick_type, pick_type],
        ).fetchall()
        claimed = [*claimed, *legacy_claimed]
        if claimed:
            raise ValueError(
                "Already claimed by another pick session: "
                + ", ".join(row["cust_order_id"] for row in claimed)
            )

        run_id = max(source_runs.values()) if source_runs else 0
        query_type = pick_type or (next(iter(source_runs)) if len(source_runs) == 1 else "mixed")
        cursor = conn.execute(
            """
            INSERT INTO pick_sessions
                (run_id, query_type, status, operator, started_at, source_runs_json)
            VALUES (?, ?, 'active', ?, ?, ?)
            """,
            (
                run_id,
                query_type,
                operator_name,
                _now_iso(),
                json.dumps(source_runs, sort_keys=True),
            ),
        )
        session_id = int(cursor.lastrowid)
        conn.execute("UPDATE pick_sessions SET workflow_mode = ? WHERE id = ?",
                     ("single_guns" if pick_type == "guns" else "legacy", session_id))
        conn.executemany(
            """
            INSERT INTO pick_orders
                (session_id, cust_order_id, customer_id, tote_code, status,
                 assigned_operator, tote_barcode)
            VALUES (?, ?, ?, ?, 'picking', ?, ?)
            """,
            [
                (
                    session_id,
                    order,
                    customers.get(order),
                    chr(ord("A") + index),
                    operator_name,
                    f"PICK-{session_id}-{chr(ord('A') + index)}",
                )
                for index, order in enumerate(selected)
            ],
        )
        conn.executemany(
            """
            INSERT INTO pick_order_events
                (session_id, cust_order_id, event_type, operator, to_operator,
                 created_at, details_json)
            VALUES (?, ?, 'claimed', ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    order,
                    operator_name,
                    operator_name,
                    _now_iso(),
                    json.dumps({"tote_code": chr(ord("A") + index)}),
                )
                for index, order in enumerate(selected)
            ],
        )
        conn.executemany(
            """
            INSERT INTO pick_lines
                (session_id, cust_order_id, customer_id, part_id, location,
                 planned_qty, item_type, upc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    line["cust_order_id"],
                    line["customer_id"],
                    line["part_id"],
                    line["location"],
                    line["planned_qty"],
                    line["item_type"],
                    line["upc"],
                )
                for line in lines.values()
                if line["planned_qty"] > 0
            ],
        )
    return session_id


def get_orders(session_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT po.*,
                   COALESCE(SUM(pl.planned_qty), 0) AS planned_units,
                   COALESCE(SUM(MIN(pl.picked_qty, pl.planned_qty)), 0) AS picked_units
            FROM pick_orders po
            LEFT JOIN pick_lines pl
              ON pl.session_id = po.session_id
             AND pl.cust_order_id = po.cust_order_id
            WHERE po.session_id = ?
            GROUP BY po.id
            ORDER BY po.tote_code
            """,
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def claimed_orders(pick_type: Optional[str] = None) -> set[str]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT po.cust_order_id
            FROM pick_orders po
            JOIN pick_sessions ps ON ps.id = po.session_id
            WHERE ((ps.status = 'active' AND po.status = 'picking')
               OR po.status IN ('ready_for_pack', 'packing', 'exception'))
               AND (? IS NULL OR ps.query_type IN (?, 'mixed'))
            """, (pick_type, pick_type)
        ).fetchall()
        legacy_rows = conn.execute(
            """
            SELECT DISTINCT pl.cust_order_id
            FROM pick_lines pl
            JOIN pick_sessions ps ON ps.id = pl.session_id
            WHERE ps.status = 'active'
              AND (? IS NULL OR ps.query_type IN (?, 'mixed'))
              AND NOT EXISTS (
                  SELECT 1 FROM pick_orders po WHERE po.session_id = ps.id
              )
            """, (pick_type, pick_type)
        ).fetchall()
    return {
        row["cust_order_id"]
        for row in [*rows, *legacy_rows]
        if row["cust_order_id"]
    }


def ready_for_pack_orders(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT po.*, ps.operator, ps.query_type, ps.id AS pick_session_id
            FROM pick_orders po
            JOIN pick_sessions ps ON ps.id = po.session_id
            WHERE po.status = 'ready_for_pack'
            ORDER BY po.completed_at, po.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_order_events(session_id: int, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM pick_order_events
            WHERE session_id = ? ORDER BY id DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _close_session_if_idle(conn: sqlite3.Connection, session_id: int) -> None:
    active = conn.execute(
        "SELECT COUNT(*) AS count FROM pick_orders WHERE session_id = ? AND status = 'picking'",
        (session_id,),
    ).fetchone()["count"]
    if active == 0:
        conn.execute(
            "UPDATE pick_sessions SET status = 'closed', completed_at = ? WHERE id = ? AND status = 'active'",
            (_now_iso(), session_id),
        )


def order_action(
    session_id: int,
    cust_order_id: str,
    action: str,
    *,
    operator: str,
    reason: Optional[str] = None,
    to_operator: Optional[str] = None,
) -> dict:
    """Release, transfer, or place a claimed order into an exception state."""
    order = _norm(cust_order_id)
    action_name = str(action or "").strip().lower()
    actor = (operator or "").strip()
    reason_text = (reason or "").strip()
    recipient = (to_operator or "").strip()
    if not actor:
        raise ValueError("Operator is required for order actions.")
    if action_name not in {"release", "transfer", "exception", "resume"}:
        raise ValueError("Unsupported order action.")
    if action_name in {"release", "exception"} and not reason_text:
        raise ValueError("Enter a reason for this order action.")
    if action_name == "transfer" and not recipient:
        raise ValueError("Enter the receiving operator's name or initials.")

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM pick_orders WHERE session_id = ? AND cust_order_id = ?",
            (session_id, order),
        ).fetchone()
        if not row:
            raise ValueError("Order is not in this pick session.")
        if action_name == "resume":
            if row["status"] != "exception":
                raise ValueError(f"{order} is not in an exception state.")
        elif row["status"] != "picking":
            raise ValueError(f"{order} is not currently being picked.")
        picked = conn.execute(
            """
            SELECT COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) AS picked
            FROM pick_lines WHERE session_id = ? AND cust_order_id = ?
            """,
            (session_id, order),
        ).fetchone()["picked"]

        from_operator = row["assigned_operator"]
        workflow = conn.execute("SELECT workflow_mode FROM pick_sessions WHERE id = ?", (session_id,)).fetchone()
        if workflow["workflow_mode"] == "single_guns" and action_name in {"transfer", "resume"}:
            receiver = recipient if action_name == "transfer" else actor
            if conn.execute(
                """SELECT 1 FROM pick_orders po JOIN pick_sessions ps ON ps.id = po.session_id
                   WHERE po.id != ? AND po.status = 'picking' AND ps.status = 'active'
                     AND ps.query_type IN ('guns', 'mixed') AND UPPER(TRIM(po.assigned_operator)) = ?""",
                (row["id"], _norm(receiver)),
            ).fetchone():
                raise ValueError(f"{receiver} already has an active gun order.")
        if action_name == "release":
            if picked:
                raise ValueError(
                    "An order with picked units cannot be released. Use Short/issue instead."
                )
            conn.execute(
                "UPDATE pick_orders SET status = 'released' WHERE id = ?",
                (row["id"],),
            )
            _close_session_if_idle(conn, session_id)
        elif action_name == "exception":
            conn.execute(
                "UPDATE pick_orders SET status = 'exception' WHERE id = ?",
                (row["id"],),
            )
        elif action_name == "resume":
            resume_operator = recipient or from_operator or actor
            active_for_recipient = conn.execute(
                """
                SELECT COUNT(*) AS count FROM pick_orders po
                JOIN pick_sessions ps ON ps.id = po.session_id
                WHERE ps.status = 'active' AND po.status = 'picking'
                  AND UPPER(TRIM(COALESCE(po.assigned_operator, ''))) = ?
                """,
                (_norm(resume_operator),),
            ).fetchone()["count"]
            if active_for_recipient >= MAX_ORDERS_PER_SESSION:
                raise ValueError(
                    f"{resume_operator} already has {MAX_ORDERS_PER_SESSION} active orders."
                )
            recipient = resume_operator
            conn.execute(
                "UPDATE pick_orders SET status = 'picking', assigned_operator = ? WHERE id = ?",
                (resume_operator, row["id"]),
            )
            conn.execute(
                "UPDATE pick_sessions SET status = 'active', completed_at = NULL WHERE id = ?",
                (session_id,),
            )
        else:
            active_for_recipient = conn.execute(
                """
                SELECT COUNT(*) AS count FROM pick_orders po
                JOIN pick_sessions ps ON ps.id = po.session_id
                WHERE ps.status = 'active' AND po.status = 'picking'
                  AND UPPER(TRIM(COALESCE(po.assigned_operator, ''))) = ?
                """,
                (_norm(recipient),),
            ).fetchone()["count"]
            if active_for_recipient >= MAX_ORDERS_PER_SESSION:
                raise ValueError(
                    f"{recipient} already has {MAX_ORDERS_PER_SESSION} active orders."
                )
            conn.execute(
                "UPDATE pick_orders SET assigned_operator = ? WHERE id = ?",
                (recipient, row["id"]),
            )

        conn.execute(
            """
            INSERT INTO pick_order_events
                (session_id, cust_order_id, event_type, reason, operator,
                 from_operator, to_operator, created_at, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                order,
                action_name,
                reason_text or None,
                actor,
                from_operator,
                recipient or None,
                _now_iso(),
                json.dumps({"picked_units": int(picked)}),
            ),
        )
    return {
        "order": order,
        "action": action_name,
        "status": "picking" if action_name in {"transfer", "resume"} else (
            "released" if action_name == "release" else "exception"
        ),
        "assigned_operator": recipient if action_name in {"transfer", "resume"} else from_operator,
    }


def attach_packlist(cust_order_id: str, packlist_id: str, required_types: Optional[set[str]] = None) -> bool:
    """Attach a packlist only after all known picking teams have finished."""
    order, packlist = _norm(cust_order_id), _norm(packlist_id)
    if not order or not packlist:
        return False
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT po.*, ps.query_type FROM pick_orders po
               JOIN pick_sessions ps ON ps.id = po.session_id
               WHERE po.cust_order_id = ? AND po.status IN ('picking', 'exception', 'ready_for_pack', 'packing')""",
            (order,),
        ).fetchall()
        if any(row["status"] in ('picking', 'exception') for row in rows):
            return False
        ready = [row for row in rows if row["status"] == 'ready_for_pack']
        finished_types = {row["query_type"] for row in rows}
        if not ready or (required_types and 'mixed' not in finished_types and not required_types.issubset(finished_types)):
            return False
        now = _now_iso()
        for row in ready:
            conn.execute("UPDATE pick_orders SET status = 'packing', packlist_id = ?, packed_at = ? WHERE id = ?",
                         (packlist, now, row["id"]))
            conn.execute(
                """INSERT INTO pick_order_events
                   (session_id, cust_order_id, event_type, operator, created_at, details_json)
                   VALUES (?, ?, 'packlist_attached', ?, ?, ?)""",
                (row["session_id"], order, row["assigned_operator"], now, json.dumps({"packlist_id": packlist})),
            )
    return True


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
                   (SELECT COUNT(*) FROM pick_orders WHERE session_id = s.id) AS order_count,
                   (SELECT COALESCE(SUM(planned_qty), 0) FROM pick_lines WHERE session_id = s.id) AS planned_units,
                   (SELECT COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) FROM pick_lines WHERE session_id = s.id) AS picked_units,
                   (SELECT COUNT(*) FROM pick_scans
                     WHERE session_id = s.id
                       AND result IN ('wrong_order', 'wrong_item', 'wrong_location', 'wrong_tote', 'wrong_operator', 'overpick', 'unknown')) AS problem_scans
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
            ORDER BY location, cust_order_id, item_type, part_id
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
                COALESCE(SUM(CASE WHEN result IN ('wrong_order', 'wrong_item', 'wrong_location', 'wrong_tote', 'wrong_operator', 'overpick', 'unknown') THEN 1 ELSE 0 END), 0) AS problem_scans
            FROM pick_scans
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    counts = {**dict(line_row), **dict(scan_row)}
    counts["remaining_units"] = max(0, counts["planned_units"] - counts["picked_units"])
    return counts


def complete_order(session_id: int, cust_order_id: str) -> dict:
    """Move one fully picked order to the packing queue."""
    order = _norm(cust_order_id)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        order_row = conn.execute(
            "SELECT * FROM pick_orders WHERE session_id = ? AND cust_order_id = ?",
            (session_id, order),
        ).fetchone()
        if not order_row:
            raise ValueError("Order is not in this pick session.")
        remaining = conn.execute(
            """
            SELECT COALESCE(SUM(MAX(planned_qty - picked_qty, 0)), 0) AS remaining
            FROM pick_lines WHERE session_id = ? AND cust_order_id = ?
            """,
            (session_id, order),
        ).fetchone()["remaining"]
        if remaining:
            raise ValueError(f"{order} still has {remaining} unit(s) to pick.")
        now = _now_iso()
        conn.execute(
            "UPDATE pick_orders SET status = 'ready_for_pack', completed_at = ? WHERE id = ?",
            (now, order_row["id"]),
        )
        conn.execute(
            """
            INSERT INTO pick_order_events
                (session_id, cust_order_id, event_type, operator, created_at, details_json)
            VALUES (?, ?, 'ready_for_pack', ?, ?, ?)
            """,
            (
                session_id,
                order,
                order_row["assigned_operator"],
                now,
                json.dumps({}),
            ),
        )
        open_orders = conn.execute(
            "SELECT COUNT(*) AS count FROM pick_orders WHERE session_id = ? AND status = 'picking'",
            (session_id,),
        ).fetchone()["count"]
        if open_orders == 0:
            conn.execute(
                "UPDATE pick_sessions SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, session_id),
            )
    return {
        "order": order,
        "status": "ready_for_pack",
        "session_complete": open_orders == 0,
    }


def complete_session(session_id: int) -> dict:
    """Complete a legacy or single-order session; never allow short completion."""
    orders = get_orders(session_id)
    if orders:
        if len(orders) != 1:
            raise ValueError("Complete each order separately.")
        complete_order(session_id, orders[0]["cust_order_id"])
    else:
        counts = compute_counts(session_id)
        if counts["remaining_units"]:
            raise ValueError(
                f"This pick still has {counts['remaining_units']} unit(s) remaining."
            )
        with _conn() as conn:
            conn.execute(
                "UPDATE pick_sessions SET status = 'completed', completed_at = ? WHERE id = ?",
                (_now_iso(), session_id),
            )
    session = get_session(session_id) or {}
    session["counts"] = compute_counts(session_id)
    return session


def abandon_session(session_id: int, *, operator: str, reason: str) -> dict:
    """Close an in-progress session short.

    Every order still being picked (or parked in exception) is set to
    ``abandoned`` so it can be claimed again, serials scanned in this session
    stop counting as picked, and the scan history stays for the audit trail.
    Orders already marked ready for pack are left alone: they are done.
    """
    actor = (operator or "").strip()
    reason_text = (reason or "").strip()
    if not actor:
        raise ValueError("Operator is required to close a pick session.")
    if not reason_text:
        raise ValueError("Enter a reason for closing this pick session.")
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM pick_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise ValueError("Pick session not found.")
        if session["status"] in ("completed", "abandoned"):
            raise ValueError(f"Pick session #{session_id} is already {session['status']}.")
        rows = conn.execute(
            "SELECT * FROM pick_orders WHERE session_id = ? AND status IN ('picking', 'exception')",
            (session_id,),
        ).fetchall()
        now = _now_iso()
        released: list[dict] = []
        total_picked = 0
        for row in rows:
            picked = conn.execute(
                """
                SELECT COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) AS picked
                FROM pick_lines WHERE session_id = ? AND cust_order_id = ?
                """,
                (session_id, row["cust_order_id"]),
            ).fetchone()["picked"]
            total_picked += int(picked)
            conn.execute(
                "UPDATE pick_orders SET status = 'abandoned' WHERE id = ?", (row["id"],)
            )
            conn.execute(
                """
                INSERT INTO pick_order_events
                    (session_id, cust_order_id, event_type, reason, operator,
                     from_operator, to_operator, created_at, details_json)
                VALUES (?, ?, 'abandoned', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    session_id, row["cust_order_id"], reason_text, actor,
                    row["assigned_operator"], now,
                    json.dumps({"picked_units": int(picked), "previous_status": row["status"]}),
                ),
            )
            released.append({
                "cust_order_id": row["cust_order_id"],
                "picked_units": int(picked),
                "previous_status": row["status"],
            })
        if not rows:
            # Legacy (whole-picklist) session: count what was pulled so the
            # operator knows what to put back.
            total_picked = int(conn.execute(
                "SELECT COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) AS picked FROM pick_lines WHERE session_id = ?",
                (session_id,),
            ).fetchone()["picked"])
        conn.execute(
            """
            UPDATE pick_sessions
            SET status = 'abandoned', completed_at = ?, closed_by = ?, closed_reason = ?
            WHERE id = ?
            """,
            (now, actor, reason_text, session_id),
        )
    return {
        "session_id": session_id,
        "status": "abandoned",
        "orders": released,
        "picked_units": total_picked,
        "closed_by": actor,
        "reason": reason_text,
    }


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


def _legacy_record_scan(
    session_id: int,
    scan_value: str,
    *,
    target_order: Optional[str] = None,
    serial: Optional[str] = None,
    part_candidates: Optional[list[dict]] = None,
    operator: Optional[str] = None,
    unknown: bool = False,
) -> dict:
    """Legacy run-wide scan behavior retained for migration reference.

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


# Order-centric scan implementation. Kept below the legacy implementation so
# existing imports continue to resolve the upgraded behavior after migration.
def _global_serial_pick(
    conn: sqlite3.Connection, serial: str
) -> Optional[sqlite3.Row]:
    # A serial picked in a session that was later closed short is back on the
    # shelf (or should be), so it must not block the next pick.
    return conn.execute(
        """
        SELECT sc.session_id, sc.target_order
        FROM pick_scans sc
        JOIN pick_sessions ps ON ps.id = sc.session_id
        WHERE sc.serial = ? AND sc.result = 'ok' AND ps.status != 'abandoned'
        LIMIT 1
        """,
        (serial,),
    ).fetchone()


def _counts_for_conn(conn: sqlite3.Connection, session_id: int) -> dict:
    line_row = conn.execute(
        """
        SELECT COALESCE(SUM(planned_qty), 0) AS planned_units,
               COALESCE(SUM(MIN(picked_qty, planned_qty)), 0) AS picked_units,
               COALESCE(SUM(CASE WHEN picked_qty >= planned_qty THEN 1 ELSE 0 END), 0) AS lines_complete,
               COUNT(*) AS lines_total
        FROM pick_lines WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    scan_row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN result = 'ok' THEN 1 ELSE 0 END), 0) AS ok_scans,
               COALESCE(SUM(CASE WHEN result = 'duplicate' THEN 1 ELSE 0 END), 0) AS duplicate_scans,
               COALESCE(SUM(CASE WHEN result IN ('wrong_order', 'wrong_item', 'wrong_location', 'wrong_tote', 'wrong_operator', 'overpick', 'unknown') THEN 1 ELSE 0 END), 0) AS problem_scans
        FROM pick_scans WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    counts = {**dict(line_row), **dict(scan_row)}
    counts["remaining_units"] = max(
        0, counts["planned_units"] - counts["picked_units"]
    )
    return counts


def record_scan(
    session_id: int,
    scan_value: str,
    *,
    target_order: Optional[str] = None,
    serial: Optional[str] = None,
    part_candidates: Optional[list[dict]] = None,
    operator: Optional[str] = None,
    unknown: bool = False,
    request_id: Optional[str] = None,
    scanned_tote: Optional[str] = None,
    scanned_location: Optional[str] = None,
    checked_upc: Optional[str] = None,
    upc_parts: Optional[list[str]] = None,
) -> dict:
    """Verify a serial/UPC against one claimed order and record the event."""
    scan_value = _norm(scan_value)
    serial = _norm(serial) or None
    target = _norm(target_order) or None
    candidates = part_candidates or []
    operator = (operator or "").strip() or None
    request_token = str(request_id or "").strip() or None
    tote = _norm(scanned_tote) or None
    location_context = _norm(scanned_location) or None

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if request_token:
            replay = conn.execute(
                "SELECT * FROM pick_scans WHERE session_id = ? AND request_id = ?",
                (session_id, request_token),
            ).fetchone()
            if replay:
                replay_line = None
                if replay["line_id"] is not None:
                    line_row = conn.execute(
                        "SELECT * FROM pick_lines WHERE id = ?", (replay["line_id"],)
                    ).fetchone()
                    replay_line = dict(line_row) if line_row else None
                return {
                    "result": replay["result"],
                    "message": replay["message"],
                    "line": replay_line,
                    "counts": _counts_for_conn(conn, session_id),
                    "idempotent_replay": True,
                }
        lines = conn.execute(
            """
            SELECT id, cust_order_id, part_id, location, planned_qty, picked_qty,
                   item_type, upc
            FROM pick_lines WHERE session_id = ?
            ORDER BY location, cust_order_id, id
            """,
            (session_id,),
        ).fetchall()
        workflow = conn.execute("SELECT workflow_mode FROM pick_sessions WHERE id = ?", (session_id,)).fetchone()
        single_guns = workflow and workflow["workflow_mode"] == "single_guns"
        candidate_parts = {
            _norm(candidate.get("part_id"))
            for candidate in candidates
            if candidate.get("part_id")
        }
        part_lines = [
            line for line in lines if _norm(line["part_id"]) in candidate_parts
        ]
        duplicate = _global_serial_pick(conn, serial) if serial else None
        order_row = (
            conn.execute(
                "SELECT * FROM pick_orders WHERE session_id = ? AND cust_order_id = ?",
                (session_id, target),
            ).fetchone()
            if target
            else None
        )
        has_claims = conn.execute(
            "SELECT 1 FROM pick_orders WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        expected_totes = set()
        if order_row:
            expected_totes = {
                _norm(order_row["tote_code"]),
                _norm(order_row["tote_barcode"]),
                f"TOTE-{_norm(order_row['tote_code'])}",
            }
        valid_locations = {
            _norm(line["location"])
            for line in lines
            if _norm(line["cust_order_id"]) == target and _norm(line["location"])
        }

        result = RESULT_UNKNOWN
        message = (
            f"{scan_value} did not match a firearm serial, component UPC, or part ID."
        )
        line_id: Optional[int] = None
        matched_part: Optional[str] = None

        if not target:
            result = RESULT_WRONG_ORDER
            message = "Select or scan the sales order before scanning an item."
        elif has_claims and not order_row:
            result = RESULT_WRONG_ORDER
            message = f"{target} is not in this pick wave."
        elif order_row and order_row["status"] != "picking":
            result = RESULT_OVERPICK
            message = f"{target} is not currently available for picking."
        elif order_row and _norm(operator) != _norm(order_row["assigned_operator"]):
            result = RESULT_WRONG_OPERATOR
            message = (
                f"{target} is assigned to {order_row['assigned_operator']}; "
                "transfer it before scanning."
            )
        elif order_row and not single_guns and tote not in expected_totes:
            result = RESULT_WRONG_TOTE
            message = (
                f"Scan tote {order_row['tote_barcode']} for {target} before the item."
            )
        elif valid_locations and location_context not in valid_locations:
            result = RESULT_WRONG_LOCATION
            message = (
                f"{location_context or 'No location'} is not a planned location for "
                f"{target}; expected {', '.join(sorted(valid_locations))}."
            )
        elif single_guns and (not serial or not checked_upc or not upc_parts
                              or not candidate_parts.intersection({_norm(p) for p in upc_parts})):
            result = RESULT_WRONG_ITEM
            message = "Scan the item UPC, then a matching firearm serial."
        elif unknown or not candidates:
            pass
        elif duplicate:
            result = RESULT_DUPLICATE
            message = (
                f"{serial} was already picked for "
                f"{duplicate['target_order'] or 'another order'} in session "
                f"#{duplicate['session_id']} — not counted twice."
            )
        elif not target:
            result = RESULT_WRONG_ORDER
            message = "Select or scan the sales order before scanning an item."
        elif has_claims and not order_row:
            result = RESULT_WRONG_ORDER
            message = f"{target} is not in this pick wave."
        elif order_row and order_row["status"] != "picking":
            result = RESULT_OVERPICK
            message = f"{target} is already ready for packing."
        elif not any(_norm(line["cust_order_id"]) == target for line in lines):
            result = RESULT_WRONG_ORDER
            message = f"{target} is not on this picklist."
        else:
            order_lines = [
                line
                for line in part_lines
                if _norm(line["cust_order_id"]) == target
                and (
                    not location_context
                    or not _norm(line["location"])
                    or _norm(line["location"]) == location_context
                )
            ]
            open_lines = [
                line
                for line in order_lines
                if line["picked_qty"] < line["planned_qty"]
            ]
            if single_guns:
                open_lines = [line for line in open_lines if _norm(line["part_id"]) in {_norm(p) for p in upc_parts or []}]
            if serial and open_lines:
                locations_by_part = {
                    _norm(candidate.get("part_id")): {
                        _norm(location)
                        for location in candidate.get("locations", [])
                        if _norm(location)
                    }
                    for candidate in candidates
                }
                matching_locations = [
                    line
                    for line in open_lines
                    if not locations_by_part.get(_norm(line["part_id"]))
                    or _norm(line["location"])
                    in locations_by_part[_norm(line["part_id"])]
                ]
                if not matching_locations:
                    expected = ", ".join(
                        sorted(
                            {
                                _norm(line["location"])
                                for line in open_lines
                                if line["location"]
                            }
                        )
                    )
                    actual = ", ".join(
                        sorted(
                            {
                                location
                                for values in locations_by_part.values()
                                for location in values
                            }
                        )
                    )
                    result = RESULT_WRONG_LOCATION
                    message = (
                        f"{serial} is in {actual or 'an unknown location'}; "
                        f"this order calls for {expected or 'the planned bin'}."
                    )
                    open_lines = []
                else:
                    open_lines = matching_locations

            if open_lines:
                chosen = open_lines[0]
                conn.execute(
                    "UPDATE pick_lines SET picked_qty = picked_qty + 1 WHERE id = ?",
                    (chosen["id"],),
                )
                line_id = chosen["id"]
                matched_part = chosen["part_id"]
                result = RESULT_OK
                identifier = "serial" if serial else "UPC/part"
                location = (
                    f" from {chosen['location']}" if chosen["location"] else ""
                )
                message = (
                    f"{matched_part} {identifier} accepted for "
                    f"{chosen['cust_order_id']}{location} — "
                    f"{chosen['picked_qty'] + 1} of {chosen['planned_qty']}."
                )
            elif result == RESULT_WRONG_LOCATION:
                pass
            elif order_lines:
                result = RESULT_OVERPICK
                message = (
                    f"{target} already has all required units of "
                    f"{order_lines[0]['part_id']}."
                )
            elif part_lines:
                belongs = sorted(
                    {
                        _norm(line["cust_order_id"])
                        for line in part_lines
                        if line["picked_qty"] < line["planned_qty"]
                    }
                )
                result = RESULT_WRONG_ORDER
                message = (
                    f"{target} does not need {part_lines[0]['part_id']}; it belongs "
                    f"to {', '.join(belongs[:3]) or 'another completed order'}."
                )
            else:
                result = RESULT_WRONG_ITEM
                shown = candidates[0].get("part_id") or scan_value
                message = f"{shown} is not in this pick wave."

        conn.execute(
            """
            INSERT INTO pick_scans
                (session_id, line_id, scan_value, serial, part_id, target_order,
                 result, message, operator, scanned_at, request_id, scanned_tote,
                 scanned_location, checked_upc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                line_id,
                scan_value,
                serial,
                matched_part
                or (candidates[0].get("part_id") if candidates else None),
                target,
                result,
                message,
                operator,
                _now_iso(),
                request_token,
                tote,
                location_context,
                checked_upc,
            ),
        )

    line = None
    if line_id is not None:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM pick_lines WHERE id = ?", (line_id,)
            ).fetchone()
        line = dict(row) if row else None
    return {
        "result": result,
        "message": message,
        "line": line,
        "counts": compute_counts(session_id),
    }
