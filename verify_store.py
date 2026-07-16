"""SQLite-backed packlist verification sessions.

A verification session snapshots the serials the ERP says shipped on one
packlist (SHIPPER_LINE → TRACE_INV_TRANS → TRACE) and then checks each gun
the operator scans out of the physical box against that snapshot. Lives in
the same SQLite database as run history so verification works even when the
Postgres audit store is not configured — this is a ship-blocking floor
workflow.

Classification is snapshot-local: a scan is verified/duplicate purely from
the snapshot. Only when a scan matches nothing does the caller (app.py)
consult the ERP to say which packlist the gun actually shipped on
(wrong_packlist) or that it never shipped at all (unexpected) — so scanning
never blocks on ERP availability.

  - verified       the scan matches a pending expected serial on this packlist.
  - duplicate      the serial was already verified in this session — logged,
                   never double-counted.
  - wrong_packlist the gun shipped on a DIFFERENT packlist; the message names
                   it so the operator knows which box it belongs in.
  - unexpected     the ERP has no shipment for this serial (or the lookup was
                   unavailable — the message says which).
  - missing        expected but never scanned; assigned when the session is
                   completed.

Non-serialized packlist lines (accessories, components with no TRACE rows)
are stored as qty-only 'info' rows: shown for a visual check, excluded from
serial verification and counts.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Set by initialize(); returns a sqlite3.Connection with row_factory=Row.
_get_conn: Optional[Callable[[], sqlite3.Connection]] = None

RESULT_VERIFIED = "verified"
RESULT_DUPLICATE = "duplicate"
RESULT_WRONG_PACKLIST = "wrong_packlist"
RESULT_UNEXPECTED = "unexpected"

PROBLEM_RESULTS = ("wrong_packlist", "unexpected")

OUTCOME_CLEAN = "clean"
OUTCOME_ISSUES = "issues"


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                packlist_id TEXT NOT NULL,
                packlist_date TEXT,
                shipper_status TEXT,
                cust_order_id TEXT,
                customer_id TEXT,
                customer_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                operator TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                expected_count INTEGER NOT NULL DEFAULT 0,
                verified_count INTEGER,
                missing_count INTEGER,
                unexpected_count INTEGER,
                duplicate_count INTEGER,
                outcome TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_expected (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                line_no INTEGER,
                part_id TEXT,
                serial TEXT,
                serial_alt TEXT,
                qty REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                scanned_at TEXT,
                FOREIGN KEY(session_id) REFERENCES verify_sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                expected_id INTEGER,
                scan_value TEXT NOT NULL,
                result TEXT NOT NULL,
                other_packlist_id TEXT,
                message TEXT,
                operator TEXT,
                scanned_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES verify_sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verify_sessions_packlist"
            " ON verify_sessions(packlist_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verify_sessions_date"
            " ON verify_sessions(packlist_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verify_expected_session"
            " ON verify_expected(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verify_scans_session"
            " ON verify_scans(session_id)"
        )


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("verify_store.initialize() has not been called")
    return _get_conn()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def start_session(
    packlist_id: str,
    header: dict,
    expected_rows: list[dict],
    operator: Optional[str] = None,
) -> int:
    """Create a session from packlist_serials.sql rows.

    header: one row's worth of packlist context (status, order, customer,
    CREATE_DATE). expected_rows: one dict per query row — rows with a serial
    become pending expected serials; rows without become 'info' rows.
    """
    serial_rows: list[tuple] = []
    info_rows: list[tuple] = []
    seen_serials: set[str] = set()
    for row in expected_rows:
        serial = _norm(row.get("TRACE_ID"))
        serial_alt = _norm(row.get("SERIAL_ID")) or None
        line_no = row.get("LINE_NO")
        part_id = str(row.get("PART_ID") or "").strip() or None
        qty = row.get("SHIPPED_QTY")
        if serial:
            if serial in seen_serials:
                continue
            seen_serials.add(serial)
            serial_rows.append((line_no, part_id, serial, serial_alt, qty, "pending"))
        else:
            info_rows.append((line_no, part_id, None, None, qty, "info"))

    packlist_date = None
    create_date = header.get("CREATE_DATE")
    if create_date is not None:
        packlist_date = str(create_date)[:10] or None

    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO verify_sessions
                (packlist_id, packlist_date, shipper_status, cust_order_id,
                 customer_id, customer_name, status, operator, started_at,
                 expected_count)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                _norm(packlist_id),
                packlist_date,
                str(header.get("SHIPPER_STATUS") or "").strip() or None,
                str(header.get("CUST_ORDER_ID") or "").strip() or None,
                str(header.get("CUSTOMER_ID") or "").strip() or None,
                str(header.get("CUSTOMER_NAME") or "").strip() or None,
                (operator or "").strip() or None,
                _now_iso(),
                len(serial_rows),
            ),
        )
        session_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO verify_expected
                (session_id, line_no, part_id, serial, serial_alt, qty, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(session_id, *row) for row in serial_rows + info_rows],
        )
    return session_id


def get_session(session_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM verify_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def get_expected(session_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM verify_expected
            WHERE session_id = ?
            ORDER BY CASE WHEN status = 'info' THEN 1 ELSE 0 END, line_no, serial
            """,
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scans(session_id: int, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT sc.*, ex.serial AS expected_serial, ex.part_id AS expected_part_id
            FROM verify_scans sc
            LEFT JOIN verify_expected ex ON ex.id = sc.expected_id
            WHERE sc.session_id = ?
            ORDER BY sc.id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_counts(session_id: int) -> dict:
    with _conn() as conn:
        expected_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status != 'info' THEN 1 ELSE 0 END), 0) AS expected_serials,
                COALESCE(SUM(CASE WHEN status = 'verified' THEN 1 ELSE 0 END), 0) AS verified,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END), 0) AS missing,
                COALESCE(SUM(CASE WHEN status = 'info' THEN 1 ELSE 0 END), 0) AS info_rows
            FROM verify_expected
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        scan_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN result = 'duplicate' THEN 1 ELSE 0 END), 0) AS duplicates,
                COALESCE(SUM(CASE WHEN result = 'wrong_packlist' THEN 1 ELSE 0 END), 0) AS wrong_packlist,
                COALESCE(SUM(CASE WHEN result = 'unexpected' THEN 1 ELSE 0 END), 0) AS unexpected,
                COUNT(*) AS total_scans
            FROM verify_scans
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    counts = {**dict(expected_row), **dict(scan_row)}
    counts["remaining"] = counts["pending"]
    counts["problem_scans"] = counts["wrong_packlist"] + counts["unexpected"]
    return counts


def complete_session(session_id: int) -> dict:
    """Mark unscanned serials missing, roll up counts, and set the outcome."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE verify_expected SET status = 'missing'
            WHERE session_id = ? AND status = 'pending'
            """,
            (session_id,),
        )
    counts = compute_counts(session_id)
    issues = counts["missing"] + counts["unexpected"] + counts["wrong_packlist"]
    outcome = OUTCOME_ISSUES if issues > 0 else OUTCOME_CLEAN
    with _conn() as conn:
        conn.execute(
            """
            UPDATE verify_sessions
            SET status = 'completed', completed_at = ?,
                verified_count = ?, missing_count = ?,
                unexpected_count = ?, duplicate_count = ?, outcome = ?
            WHERE id = ?
            """,
            (
                _now_iso(),
                counts["verified"],
                counts["missing"],
                counts["unexpected"] + counts["wrong_packlist"],
                counts["duplicates"],
                outcome,
                session_id,
            ),
        )
    session = get_session(session_id) or {}
    session["counts"] = counts
    return session


def recent_sessions(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM verify_scans
                     WHERE session_id = s.id
                       AND result IN ('wrong_packlist', 'unexpected')) AS problem_scans
            FROM verify_sessions s
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_sessions_for_packlists(packlist_ids: list[str]) -> dict[str, dict]:
    """Latest session per packlist — the dashboard shows the most recent verdict."""
    ids = sorted({_norm(p) for p in packlist_ids if _norm(p)})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT s.* FROM verify_sessions s
            WHERE s.packlist_id IN ({placeholders})
              AND s.id = (
                  SELECT MAX(id) FROM verify_sessions
                  WHERE packlist_id = s.packlist_id
              )
            """,
            ids,
        ).fetchall()
    return {row["packlist_id"]: dict(row) for row in rows}


def sessions_started_on(day_iso: str) -> list[dict]:
    """Sessions whose started_at falls on the given UTC date (YYYY-MM-DD)."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM verify_sessions
            WHERE substr(started_at, 1, 10) = ?
            ORDER BY id DESC
            """,
            (day_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Scan verification
# ---------------------------------------------------------------------------
def record_scan(
    session_id: int,
    scan_value: str,
    *,
    other_packlist_id: Optional[str] = None,
    other_customer: Optional[str] = None,
    erp_checked: bool = True,
    operator: Optional[str] = None,
) -> dict:
    """Verify one scan against the session's expected serials and log it.

    The caller only supplies other_packlist_id/other_customer when the scan
    matched nothing here and the ERP reverse lookup found the serial on a
    different live packlist. erp_checked=False means that lookup itself was
    unavailable, which softens the 'unexpected' message.
    """
    scan_value = _norm(scan_value)
    operator = (operator or "").strip() or None

    with _conn() as conn:
        result: str
        message: str
        expected_id: Optional[int] = None
        matched: Optional[dict] = None

        row = conn.execute(
            """
            SELECT * FROM verify_expected
            WHERE session_id = ? AND status != 'info'
              AND (serial = ? OR serial_alt = ?)
            LIMIT 1
            """,
            (session_id, scan_value, scan_value),
        ).fetchone()

        if row is not None:
            expected_id = row["id"]
            if row["status"] == "verified":
                result = RESULT_DUPLICATE
                message = (
                    f"{scan_value} was already scanned in this session — "
                    "not counted twice."
                )
            else:
                conn.execute(
                    "UPDATE verify_expected SET status = 'verified', scanned_at = ?"
                    " WHERE id = ?",
                    (_now_iso(), expected_id),
                )
                result = RESULT_VERIFIED
                part_bit = f" ({row['part_id']})" if row["part_id"] else ""
                message = f"{scan_value}{part_bit} verified — belongs on this packlist."
        elif other_packlist_id:
            result = RESULT_WRONG_PACKLIST
            cust_bit = f" ({other_customer})" if other_customer else ""
            message = (
                f"{scan_value} shipped on {_norm(other_packlist_id)}{cust_bit}"
                " — wrong box."
            )
        else:
            result = RESULT_UNEXPECTED
            if erp_checked:
                message = (
                    f"{scan_value} is not on this packlist and has no shipment"
                    " in the ERP."
                )
            else:
                message = (
                    f"{scan_value} is not on this packlist (ERP lookup"
                    " unavailable to say where it belongs)."
                )

        conn.execute(
            """
            INSERT INTO verify_scans
                (session_id, expected_id, scan_value, result, other_packlist_id,
                 message, operator, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                expected_id,
                scan_value,
                result,
                _norm(other_packlist_id) or None,
                message,
                operator,
                _now_iso(),
            ),
        )

        if expected_id is not None:
            refreshed = conn.execute(
                "SELECT * FROM verify_expected WHERE id = ?", (expected_id,)
            ).fetchone()
            matched = dict(refreshed) if refreshed else None

    return {
        "result": result,
        "message": message,
        "expected_row": matched,
        "counts": compute_counts(session_id),
    }
