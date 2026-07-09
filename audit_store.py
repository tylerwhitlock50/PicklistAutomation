"""Postgres-backed store for the serialized inventory audit feature.

Separate from the SQLite picklist history: audit state (auditable locations +
cadence, per-session expected snapshots, scan events, accuracy metrics) lives in
the managed Postgres database identified by ``DATABASE_URL``.

The whole feature degrades gracefully: if ``DATABASE_URL`` is unset, the driver
is missing, or Postgres is unreachable at startup, ``initialize()`` disables the
audit feature (``is_available()`` returns ``False``) and the rest of the app —
the picklist — keeps working untouched.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("picklist-app.audit")

# The auditable serialized-inventory locations, matching SCOPE values emitted by
# sql/audit_serialized_inventory.sql. Seeded into audit_locations on first boot.
AUDIT_SCOPES: list[dict[str, Any]] = [
    {"scope": "C2", "label": "Cage 2 (C2)", "warehouse_id": "MAIN", "sort_order": 10},
    {"scope": "C2-SERIALIZED", "label": "Cage 2 - Serialized", "warehouse_id": "MAIN", "sort_order": 20},
    {"scope": "SHIPPING-RACKS", "label": "Shipping Racks (R01-R09)", "warehouse_id": "SHIPPING", "sort_order": 30},
    {"scope": "TIED-WO", "label": "Tied Work Orders (built, unshipped)", "warehouse_id": None, "sort_order": 40},
]
VALID_SCOPES = {s["scope"] for s in AUDIT_SCOPES}
DEFAULT_CADENCE_DAYS = 7

_engine: Optional[Engine] = None
_available = False


# ---------------------------------------------------------------------------
# Engine + schema bootstrap
# ---------------------------------------------------------------------------
def _normalize_pg_url(url: str) -> str:
    """Force the psycopg (v3) SQLAlchemy driver.

    A bare ``postgresql://`` / ``postgres://`` URL makes SQLAlchemy default to
    psycopg2, which has no prebuilt wheels on newer Pythons. We ship psycopg v3,
    so rewrite the scheme to ``postgresql+psycopg://`` unless a driver is already
    pinned.
    """
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def get_database_url() -> Optional[str]:
    url = os.getenv("DATABASE_URL")
    if not url or not url.strip():
        return None
    return _normalize_pg_url(url.strip())


def mask_database_url(url: str) -> str:
    """Hide the password in a postgresql URL for safe logging."""
    try:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    except ValueError:
        pass
    return "***"


def is_available() -> bool:
    return _available


def get_engine() -> Optional[Engine]:
    return _engine


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS audit_locations (
        id           SERIAL PRIMARY KEY,
        scope        TEXT UNIQUE NOT NULL,
        label        TEXT NOT NULL,
        warehouse_id TEXT,
        cadence_days INTEGER NOT NULL DEFAULT 7,
        active       BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order   INTEGER NOT NULL DEFAULT 100
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_sessions (
        id               SERIAL PRIMARY KEY,
        scope            TEXT,
        status           TEXT NOT NULL DEFAULT 'in_progress',
        operator         TEXT,
        started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at     TIMESTAMPTZ,
        expected_count   INTEGER NOT NULL DEFAULT 0,
        verified_count   INTEGER NOT NULL DEFAULT 0,
        misplaced_count  INTEGER NOT NULL DEFAULT 0,
        missing_count    INTEGER NOT NULL DEFAULT 0,
        unexpected_count INTEGER NOT NULL DEFAULT 0,
        accuracy_pct     NUMERIC(5,2)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_expected (
        id                 SERIAL PRIMARY KEY,
        session_id         INTEGER NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,
        serial             TEXT NOT NULL,
        part_id            TEXT,
        part_description   TEXT,
        product_code       TEXT,
        expected_warehouse TEXT,
        expected_location  TEXT,
        scope              TEXT,
        tied_wo            BOOLEAN NOT NULL DEFAULT FALSE,
        cust_order_id      TEXT,
        customer_id        TEXT,
        status             TEXT NOT NULL DEFAULT 'pending',
        scanned_location   TEXT,
        scanned_at         TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_audit_expected_session ON audit_expected(session_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_expected_serial ON audit_expected(session_id, serial)",
    """
    CREATE TABLE IF NOT EXISTS audit_scans (
        id               SERIAL PRIMARY KEY,
        session_id       INTEGER NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,
        scanned_serial   TEXT NOT NULL,
        scanned_location TEXT,
        result           TEXT NOT NULL,
        expected_id      INTEGER REFERENCES audit_expected(id) ON DELETE SET NULL,
        operator         TEXT,
        scanned_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_audit_scans_session ON audit_scans(session_id)",
]


def initialize() -> bool:
    """Create the engine + schema and seed scopes. Returns availability.

    Never raises: any failure logs a warning and leaves the feature disabled.
    """
    global _engine, _available

    url = get_database_url()
    if not url:
        logger.warning("DATABASE_URL is not set; serialized audit feature is disabled.")
        _available = False
        return False

    try:
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800, future=True)
        with _engine.begin() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(text(statement))
            for scope in AUDIT_SCOPES:
                conn.execute(
                    text(
                        """
                        INSERT INTO audit_locations (scope, label, warehouse_id, cadence_days, sort_order)
                        VALUES (:scope, :label, :warehouse_id, :cadence_days, :sort_order)
                        ON CONFLICT (scope) DO UPDATE SET
                            label = EXCLUDED.label,
                            warehouse_id = EXCLUDED.warehouse_id,
                            sort_order = EXCLUDED.sort_order
                        """
                    ),
                    {
                        "scope": scope["scope"],
                        "label": scope["label"],
                        "warehouse_id": scope["warehouse_id"],
                        "cadence_days": DEFAULT_CADENCE_DAYS,
                        "sort_order": scope["sort_order"],
                    },
                )
        _available = True
        logger.info("Serialized audit store ready on %s", mask_database_url(url))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Serialized audit feature disabled: could not initialize Postgres store (%s).",
            exc,
        )
        _engine = None
        _available = False
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _rows(result) -> list[dict[str, Any]]:
    return [dict(m) for m in result.mappings().all()]


# ---------------------------------------------------------------------------
# Locations dashboard
# ---------------------------------------------------------------------------
def list_scope_status() -> list[dict[str, Any]]:
    """Per auditable location: cadence, last-inventoried time, days since, due flag."""
    if not _available:
        return []
    with _engine.connect() as conn:
        rows = _rows(
            conn.execute(
                text(
                    """
                    SELECT l.scope, l.label, l.warehouse_id, l.cadence_days, l.active, l.sort_order,
                           s.completed_at AS last_inventoried,
                           s.accuracy_pct AS last_accuracy,
                           s.id           AS last_session_id
                    FROM audit_locations l
                    LEFT JOIN LATERAL (
                        SELECT id, completed_at, accuracy_pct
                        FROM audit_sessions
                        WHERE status = 'completed'
                          AND (scope = l.scope OR scope IS NULL)
                        ORDER BY completed_at DESC
                        LIMIT 1
                    ) s ON TRUE
                    ORDER BY l.sort_order, l.scope
                    """
                )
            )
        )
    now = datetime.now(timezone.utc)
    for row in rows:
        last = row.get("last_inventoried")
        if last is not None:
            days = (now - last).total_seconds() / 86400.0
            row["days_since"] = round(days, 1)
            row["due"] = days >= row["cadence_days"]
        else:
            row["days_since"] = None
            row["due"] = True  # never audited
    return rows


def recent_sessions(limit: int = 10) -> list[dict[str, Any]]:
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT id, scope, status, operator, started_at, completed_at,
                           expected_count, verified_count, misplaced_count,
                           missing_count, unexpected_count, accuracy_pct
                    FROM audit_sessions
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def start_session(scope: Optional[str], expected_rows: list[dict[str, Any]], operator: Optional[str]) -> int:
    """Create an in-progress session and snapshot the expected serials into it."""
    if not _available:
        raise RuntimeError("Audit store is not available.")
    with _engine.begin() as conn:
        session_id = conn.execute(
            text(
                """
                INSERT INTO audit_sessions (scope, status, operator, expected_count)
                VALUES (:scope, 'in_progress', :operator, :expected_count)
                RETURNING id
                """
            ),
            {"scope": scope, "operator": operator, "expected_count": len(expected_rows)},
        ).scalar_one()

        if expected_rows:
            params = [
                {
                    "session_id": session_id,
                    "serial": (r.get("SERIAL_NO") or "").strip(),
                    "part_id": r.get("PART_ID"),
                    "part_description": r.get("PART_DESCRIPTION"),
                    "product_code": r.get("PRODUCT_CODE"),
                    "expected_warehouse": r.get("EXPECTED_WAREHOUSE"),
                    "expected_location": r.get("EXPECTED_LOCATION"),
                    "scope": r.get("SCOPE"),
                    "tied_wo": bool(r.get("TIED_WO")),
                    "cust_order_id": r.get("CUST_ORDER_ID"),
                    "customer_id": r.get("CUSTOMER_ID"),
                }
                for r in expected_rows
            ]
            conn.execute(
                text(
                    """
                    INSERT INTO audit_expected (
                        session_id, serial, part_id, part_description, product_code,
                        expected_warehouse, expected_location, scope, tied_wo,
                        cust_order_id, customer_id
                    ) VALUES (
                        :session_id, :serial, :part_id, :part_description, :product_code,
                        :expected_warehouse, :expected_location, :scope, :tied_wo,
                        :cust_order_id, :customer_id
                    )
                    """
                ),
                params,
            )
    return session_id


def get_session(session_id: int) -> Optional[dict[str, Any]]:
    if not _available:
        return None
    with _engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM audit_sessions WHERE id = :id"), {"id": session_id}
        ).mappings().first()
    return dict(row) if row else None


def get_expected_items(session_id: int) -> list[dict[str, Any]]:
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT id, serial, part_id, part_description, product_code,
                           expected_warehouse, expected_location, scope, tied_wo,
                           cust_order_id, customer_id, status, scanned_location, scanned_at
                    FROM audit_expected
                    WHERE session_id = :id
                    ORDER BY scope, expected_location, serial
                    """
                ),
                {"id": session_id},
            )
        )


def get_unexpected_scans(session_id: int) -> list[dict[str, Any]]:
    """Distinct serials scanned that were not in the expected snapshot."""
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT scanned_serial, scanned_location,
                           MIN(scanned_at) AS first_scanned_at, COUNT(*) AS scan_count
                    FROM audit_scans
                    WHERE session_id = :id AND expected_id IS NULL
                    GROUP BY scanned_serial, scanned_location
                    ORDER BY first_scanned_at
                    """
                ),
                {"id": session_id},
            )
        )


def compute_counts(session_id: int) -> dict[str, int]:
    """Live counts derived from expected statuses + unexpected scans."""
    if not _available:
        return {}
    with _engine.connect() as conn:
        status_rows = conn.execute(
            text(
                "SELECT status, COUNT(*) AS n FROM audit_expected WHERE session_id = :id GROUP BY status"
            ),
            {"id": session_id},
        ).all()
        by_status = {r[0]: r[1] for r in status_rows}
        expected_total = sum(by_status.values())
        unexpected = conn.execute(
            text(
                "SELECT COUNT(DISTINCT scanned_serial) FROM audit_scans WHERE session_id = :id AND expected_id IS NULL"
            ),
            {"id": session_id},
        ).scalar_one()
    return {
        "expected": expected_total,
        "verified": by_status.get("verified", 0),
        "misplaced": by_status.get("misplaced", 0),
        "pending": by_status.get("pending", 0),
        "missing": by_status.get("missing", 0),
        "unexpected": int(unexpected),
    }


def record_scan(
    session_id: int, serial: str, location: str, operator: Optional[str]
) -> dict[str, Any]:
    """Classify a (serial, location) scan against the session snapshot and persist it.

    Returns {result, is_duplicate, item, counts}. result is one of
    verified | misplaced | unexpected.
    """
    if not _available:
        raise RuntimeError("Audit store is not available.")

    norm_serial = _norm(serial)
    norm_location = _norm(location)

    with _engine.begin() as conn:
        item = conn.execute(
            text(
                """
                SELECT id, serial, expected_location, scope, tied_wo, status, part_description
                FROM audit_expected
                WHERE session_id = :sid AND UPPER(serial) = :serial
                ORDER BY id
                LIMIT 1
                """
            ),
            {"sid": session_id, "serial": norm_serial},
        ).mappings().first()

        prior_scan = conn.execute(
            text(
                "SELECT 1 FROM audit_scans WHERE session_id = :sid AND UPPER(scanned_serial) = :serial LIMIT 1"
            ),
            {"sid": session_id, "serial": norm_serial},
        ).first()
        is_duplicate = prior_scan is not None

        if item is None:
            result = "unexpected"
            expected_id = None
            item_dict: Optional[dict[str, Any]] = None
        else:
            expected_id = item["id"]
            expected_loc = _norm(item["expected_location"])
            # Tied-WO units with no known bin (unassigned): scanning anywhere confirms presence.
            if not expected_loc:
                result = "verified"
            elif norm_location == expected_loc:
                result = "verified"
            else:
                result = "misplaced"
            conn.execute(
                text(
                    """
                    UPDATE audit_expected
                    SET status = :status, scanned_location = :loc, scanned_at = now()
                    WHERE id = :id
                    """
                ),
                {"status": result, "loc": location.strip(), "id": expected_id},
            )
            item_dict = dict(item)
            item_dict["status"] = result
            item_dict["scanned_location"] = location.strip()

        conn.execute(
            text(
                """
                INSERT INTO audit_scans (session_id, scanned_serial, scanned_location, result, expected_id, operator)
                VALUES (:sid, :serial, :loc, :result, :expected_id, :operator)
                """
            ),
            {
                "sid": session_id,
                "serial": serial.strip(),
                "loc": location.strip(),
                "result": "duplicate" if is_duplicate else result,
                "expected_id": expected_id,
                "operator": operator,
            },
        )

    return {
        "result": result,
        "is_duplicate": is_duplicate,
        "item": item_dict,
        "counts": compute_counts(session_id),
    }


def complete_session(session_id: int) -> Optional[dict[str, Any]]:
    """Finalize: unscanned expected -> missing, roll up counts + accuracy, stamp completion."""
    if not _available:
        raise RuntimeError("Audit store is not available.")
    with _engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_expected SET status = 'missing' WHERE session_id = :id AND status = 'pending'"
            ),
            {"id": session_id},
        )
    counts = compute_counts(session_id)
    expected_total = counts.get("expected", 0)
    verified = counts.get("verified", 0)
    accuracy = round(100.0 * verified / expected_total, 2) if expected_total else None
    with _engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE audit_sessions
                SET status = 'completed', completed_at = now(),
                    expected_count = :expected, verified_count = :verified,
                    misplaced_count = :misplaced, missing_count = :missing,
                    unexpected_count = :unexpected, accuracy_pct = :accuracy
                WHERE id = :id
                """
            ),
            {
                "id": session_id,
                "expected": expected_total,
                "verified": verified,
                "misplaced": counts.get("misplaced", 0),
                "missing": counts.get("missing", 0),
                "unexpected": counts.get("unexpected", 0),
                "accuracy": accuracy,
            },
        )
    return get_session(session_id)
