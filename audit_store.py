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
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("picklist-app.audit")

# Auditable units are individual warehouse/location bins, discovered from the
# ERP via sql/audit_locations_sync.sql and upserted into audit_locations. The
# one non-bin unit is the tied-work-order group (built for an open order, not
# shipped, not resident in an audited bin).
TIED_WO_SCOPE = "TIED-WO"
TIED_WO_LABEL = "Tied Work Orders (built, unshipped)"
DEFAULT_CADENCE_DAYS = 7


def location_key(warehouse_id: str, loc_id: str) -> str:
    return f"{warehouse_id}/{loc_id}"

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
    "ALTER TABLE audit_locations ADD COLUMN IF NOT EXISTS location_id TEXT",
    "ALTER TABLE audit_locations ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE audit_locations ADD COLUMN IF NOT EXISTS serial_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE audit_locations ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ",
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
    "ALTER TABLE audit_sessions ADD COLUMN IF NOT EXISTS target_warehouse TEXT",
    "ALTER TABLE audit_sessions ADD COLUMN IF NOT EXISTS target_location TEXT",
    "ALTER TABLE audit_sessions ADD COLUMN IF NOT EXISTS label TEXT",
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
    "ALTER TABLE audit_expected ADD COLUMN IF NOT EXISTS in_scope BOOLEAN NOT NULL DEFAULT TRUE",
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
            # Migration from the original scope-group model ('C2', 'C2-SERIALIZED',
            # 'SHIPPING'/'SHIPPING-RACKS' rows without a location_id): drop the
            # group rows — sync_locations() repopulates at bin grain — and stamp
            # target columns onto old sessions so coverage matching keeps working.
            conn.execute(
                text(
                    "DELETE FROM audit_locations "
                    "WHERE location_id IS NULL AND scope <> :tied"
                ),
                {"tied": TIED_WO_SCOPE},
            )
            conn.execute(
                text(
                    """
                    UPDATE audit_sessions SET
                        target_warehouse = CASE
                            WHEN scope IN ('C2', 'C2-SERIALIZED') THEN 'MAIN'
                            WHEN scope = 'SHIPPING-RACKS' THEN 'SHIPPING'
                            ELSE target_warehouse END,
                        target_location = CASE
                            WHEN scope IN ('C2', 'C2-SERIALIZED') THEN scope
                            ELSE target_location END
                    WHERE target_warehouse IS NULL
                      AND scope IN ('C2', 'C2-SERIALIZED', 'SHIPPING-RACKS')
                    """
                )
            )
            # Legacy SHIPPING-RACKS sessions audited only racks R01-R09, a
            # subset the bin-grain target model can't express. A NULL
            # target_location would read as "whole SHIPPING warehouse" and
            # wrongly credit never-audited bins (STAGE, INTERNATIONAL), so pin
            # them to a sentinel location no real bin matches: they credit
            # nothing and the racks simply come due again. Runs unconditionally
            # so databases stamped by an earlier buggy migration get corrected.
            conn.execute(
                text(
                    """
                    UPDATE audit_sessions
                    SET target_location = scope
                    WHERE scope = 'SHIPPING-RACKS' AND target_location IS NULL
                    """
                )
            )
            # Seed the one static auditable unit.
            conn.execute(
                text(
                    """
                    INSERT INTO audit_locations (scope, label, warehouse_id, location_id, cadence_days, sort_order)
                    VALUES (:scope, :label, NULL, NULL, :cadence_days, 10000)
                    ON CONFLICT (scope) DO UPDATE SET label = EXCLUDED.label
                    """
                ),
                {
                    "scope": TIED_WO_SCOPE,
                    "label": TIED_WO_LABEL,
                    "cadence_days": DEFAULT_CADENCE_DAYS,
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
# Dashboard read cache
# ---------------------------------------------------------------------------
# The Postgres store is a remote managed DB, so every query is a network
# round-trip (~250ms each from the office). The dashboard reads
# (list_location_status / recent_sessions / last_synced_at) are cached briefly
# and invalidated by the writes that change them (sync_locations,
# start_session, complete_session), so a same-process write is visible
# immediately. Under multi-worker gunicorn other workers may serve results up
# to the TTL stale — acceptable for this dashboard. Set
# AUDIT_DASHBOARD_CACHE_SECONDS=0 to disable.
_CACHE_TTL_SECONDS = float(os.getenv("AUDIT_DASHBOARD_CACHE_SECONDS", "30") or 0)
_cache_lock = threading.Lock()
_read_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, loader: Callable[[], Any]) -> Any:
    if _CACHE_TTL_SECONDS <= 0:
        return loader()
    with _cache_lock:
        hit = _read_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    value = loader()
    with _cache_lock:
        _read_cache[key] = (time.monotonic(), value)
    return value


def invalidate_read_cache() -> None:
    with _cache_lock:
        _read_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _rows(result) -> list[dict[str, Any]]:
    return [dict(m) for m in result.mappings().all()]


# ---------------------------------------------------------------------------
# Location sync + dashboard
# ---------------------------------------------------------------------------
def sync_locations(location_rows: list[dict[str, Any]]) -> int:
    """Upsert the auditable-location list from the ERP.

    location_rows come from sql/audit_locations_sync.sql:
    WAREHOUSE_ID, LOCATION_ID, DESCRIPTION, SERIAL_COUNT. Locations that no
    longer hold serials are deactivated (history preserved), not deleted.
    """
    if not _available:
        raise RuntimeError("Audit store is not available.")
    keys = []
    params = []
    for row in location_rows:
        warehouse_id = (row.get("WAREHOUSE_ID") or "").strip()
        loc_id = (row.get("LOCATION_ID") or "").strip()
        if not warehouse_id or not loc_id:
            continue
        key = location_key(warehouse_id, loc_id)
        keys.append(key)
        params.append(
            {
                "scope": key,
                "label": loc_id,
                "warehouse_id": warehouse_id,
                "location_id": loc_id,
                "description": (row.get("DESCRIPTION") or "").strip() or None,
                "serial_count": int(row.get("SERIAL_COUNT") or 0),
                "cadence_days": DEFAULT_CADENCE_DAYS,
                "sort_order": 100,
            }
        )
    if not keys:
        # A sync that produces nothing is far more likely an ERP hiccup than a
        # warehouse that genuinely emptied out — deactivating every bin on it
        # would blank the dashboard, so keep the stored list untouched.
        logger.warning("Location sync returned no usable rows; keeping the stored location list.")
        return 0
    with _engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO audit_locations
                    (scope, label, warehouse_id, location_id, description,
                     serial_count, cadence_days, active, sort_order, synced_at)
                VALUES
                    (:scope, :label, :warehouse_id, :location_id, :description,
                     :serial_count, :cadence_days, TRUE, :sort_order, now())
                ON CONFLICT (scope) DO UPDATE SET
                    label = EXCLUDED.label,
                    description = EXCLUDED.description,
                    serial_count = EXCLUDED.serial_count,
                    active = TRUE,
                    synced_at = now()
                """
            ),
            params,
        )
        result = conn.execute(
            text(
                """
                UPDATE audit_locations
                SET active = FALSE, serial_count = 0, synced_at = now()
                WHERE location_id IS NOT NULL
                  AND active = TRUE
                  AND NOT (scope = ANY(:keys))
                """
            ),
            {"keys": keys},
        )
        deactivated = result.rowcount or 0
    invalidate_read_cache()
    logger.info(
        "Synced %d auditable locations from ERP (%d deactivated).",
        len(keys),
        deactivated,
    )
    return len(keys)


def _fetch_last_synced_at() -> Optional[datetime]:
    with _engine.connect() as conn:
        return conn.execute(
            text("SELECT MAX(synced_at) FROM audit_locations WHERE location_id IS NOT NULL")
        ).scalar()


def last_synced_at() -> Optional[datetime]:
    if not _available:
        return None
    return _cached("last_synced_at", _fetch_last_synced_at)


def needs_sync(max_age_minutes: int = 15) -> bool:
    last = last_synced_at()
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() > max_age_minutes * 60


def list_location_status() -> list[dict[str, Any]]:
    """Per auditable bin (+ the tied-WO group): last-inventoried, days since, due.

    A completed session covers a location when the session's target includes it:
    full audits (no target) cover everything; warehouse audits cover that
    warehouse's bins; single-location audits cover that bin. Tied-WO sessions
    cover only the tied-WO row (and full audits cover it too).
    """
    if not _available:
        return []
    rows = _cached("location_status", _fetch_location_status_rows)
    # Copy before returning: callers annotate the dicts, and days_since/due are
    # a function of "now", so neither may be baked into the cached rows.
    rows = [dict(row) for row in rows]
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


def _fetch_location_status_rows() -> list[dict[str, Any]]:
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT l.scope, l.label, l.warehouse_id, l.location_id, l.description,
                           l.serial_count, l.cadence_days, l.active, l.sort_order, l.synced_at,
                           s.completed_at AS last_inventoried,
                           s.accuracy_pct AS last_accuracy,
                           s.id           AS last_session_id
                    FROM audit_locations l
                    LEFT JOIN LATERAL (
                        SELECT id, completed_at, accuracy_pct
                        FROM audit_sessions
                        WHERE status = 'completed'
                          AND (
                            (l.scope = :tied AND (scope = :tied OR (target_warehouse IS NULL AND scope IS DISTINCT FROM :tied AND target_location IS NULL)))
                            OR
                            (l.scope <> :tied
                             AND scope IS DISTINCT FROM :tied
                             AND (target_warehouse IS NULL OR target_warehouse = l.warehouse_id)
                             AND (target_location IS NULL OR target_location = l.location_id))
                          )
                        ORDER BY completed_at DESC
                        LIMIT 1
                    ) s ON TRUE
                    WHERE l.active = TRUE
                    ORDER BY l.warehouse_id NULLS LAST, l.location_id, l.scope
                    """
                ),
                {"tied": TIED_WO_SCOPE},
            )
        )


def recent_sessions(limit: int = 10) -> list[dict[str, Any]]:
    if not _available:
        return []
    rows = _cached(f"recent_sessions:{limit}", lambda: _fetch_recent_sessions(limit))
    return [dict(row) for row in rows]


def _fetch_recent_sessions(limit: int) -> list[dict[str, Any]]:
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT id, scope, label, status, operator, started_at, completed_at,
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
# Analytics
# ---------------------------------------------------------------------------
# Rows a completed session actually learned something about: everything the
# session was auditing (in_scope) plus out-of-scope serials that turned up on
# the audited shelf (status flipped off 'pending' by a scan).
_OBSERVED_ROW = "(e.in_scope OR e.status <> 'pending')"


def completed_sessions_since(days: int) -> list[dict[str, Any]]:
    """Completed sessions in the window, oldest first (trend order)."""
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT id, scope, label, operator, started_at, completed_at,
                           expected_count, verified_count, misplaced_count,
                           missing_count, unexpected_count, accuracy_pct
                    FROM audit_sessions
                    WHERE status = 'completed'
                      AND completed_at >= now() - make_interval(days => :days)
                    ORDER BY completed_at
                    """
                ),
                {"days": days},
            )
        )


def location_error_breakdown(days: int, limit: int = 25) -> list[dict[str, Any]]:
    """Locations (bin grain; tied WOs as one group) with at least one
    misplaced/missing result across completed sessions in the window, worst first."""
    if not _available:
        return []
    with _engine.connect() as conn:
        rows = _rows(
            conn.execute(
                text(
                    f"""
                    SELECT CASE
                               WHEN e.scope = '{TIED_WO_SCOPE}' THEN 'Tied work orders'
                               ELSE COALESCE(e.expected_warehouse, '?') || ' / ' ||
                                    COALESCE(e.expected_location, 'Unassigned')
                           END AS scope,
                           COUNT(*) FILTER (WHERE e.in_scope) AS audited,
                           COUNT(*) FILTER (WHERE e.in_scope AND e.status = 'verified') AS verified,
                           COUNT(*) FILTER (WHERE e.status = 'misplaced') AS misplaced,
                           COUNT(*) FILTER (WHERE e.status = 'missing')   AS missing,
                           MAX(s.completed_at) AS last_audited_at
                    FROM audit_expected e
                    JOIN audit_sessions s ON s.id = e.session_id
                    WHERE s.status = 'completed'
                      AND s.completed_at >= now() - make_interval(days => :days)
                      AND {_OBSERVED_ROW}
                    GROUP BY 1
                    HAVING COUNT(*) FILTER (WHERE e.status IN ('misplaced', 'missing')) > 0
                    ORDER BY COUNT(*) FILTER (WHERE e.status IN ('misplaced', 'missing')) DESC,
                             MAX(s.completed_at) DESC
                    LIMIT :limit
                    """
                ),
                {"days": days, "limit": limit},
            )
        )
    for row in rows:
        audited = row.get("audited") or 0
        errors = (row.get("misplaced") or 0) + (row.get("missing") or 0)
        row["errors"] = errors
        row["error_rate_pct"] = round(100.0 * errors / audited, 1) if audited else None
    return rows


def repeat_offender_serials(days: int, limit: int = 50) -> list[dict[str, Any]]:
    """Serials that came up misplaced or missing in the window, with how often,
    plus where the most recent completed session left them."""
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    f"""
                    WITH hist AS (
                        SELECT e.serial, e.part_id, e.part_description, e.status,
                               e.in_scope, e.tied_wo, e.cust_order_id,
                               e.expected_warehouse, e.expected_location, e.scanned_location,
                               s.id AS session_id, s.completed_at
                        FROM audit_expected e
                        JOIN audit_sessions s ON s.id = e.session_id
                        WHERE s.status = 'completed'
                          AND s.completed_at >= now() - make_interval(days => :days)
                          AND {_OBSERVED_ROW}
                    ),
                    agg AS (
                        SELECT UPPER(serial) AS serial_key,
                               COUNT(*) FILTER (WHERE in_scope) AS audits,
                               COUNT(*) FILTER (WHERE status = 'missing')   AS missing_times,
                               COUNT(*) FILTER (WHERE status = 'misplaced') AS misplaced_times
                        FROM hist
                        GROUP BY UPPER(serial)
                    ),
                    latest AS (
                        SELECT DISTINCT ON (UPPER(serial)) UPPER(serial) AS serial_key, hist.*
                        FROM hist
                        ORDER BY UPPER(serial), completed_at DESC
                    )
                    SELECT l.serial, l.part_id, l.part_description, l.tied_wo, l.cust_order_id,
                           l.expected_warehouse, l.expected_location,
                           l.scanned_location AS last_scanned_location,
                           l.status AS last_status,
                           l.session_id AS last_session_id,
                           l.completed_at AS last_audited_at,
                           a.audits, a.missing_times, a.misplaced_times,
                           a.missing_times + a.misplaced_times AS error_times
                    FROM agg a
                    JOIN latest l ON l.serial_key = a.serial_key
                    WHERE a.missing_times > 0 OR a.misplaced_times > 0
                    ORDER BY a.missing_times + a.misplaced_times DESC, l.completed_at DESC
                    LIMIT :limit
                    """
                ),
                {"days": days, "limit": limit},
            )
        )


def current_exceptions(limit: int = 200) -> list[dict[str, Any]]:
    """Serials whose LATEST completed-audit result is missing or misplaced.

    This is the open punch list: a serial missing in an old audit that verified
    in a newer one drops off automatically.
    """
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (UPPER(e.serial))
                               e.serial, e.part_id, e.part_description, e.status,
                               e.tied_wo, e.cust_order_id,
                               e.expected_warehouse, e.expected_location, e.scanned_location,
                               s.id AS session_id, s.label AS session_label, s.completed_at
                        FROM audit_expected e
                        JOIN audit_sessions s ON s.id = e.session_id
                        WHERE s.status = 'completed' AND {_OBSERVED_ROW}
                        ORDER BY UPPER(e.serial), s.completed_at DESC
                    )
                    SELECT * FROM latest
                    WHERE status IN ('missing', 'misplaced')
                    ORDER BY CASE status WHEN 'missing' THEN 0 ELSE 1 END,
                             expected_warehouse NULLS LAST, expected_location, serial
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )


def unexpected_serials_since(days: int, limit: int = 50) -> list[dict[str, Any]]:
    """Distinct serials scanned during window sessions that were not in any
    snapshot — guns the system did not know about, most recent first."""
    if not _available:
        return []
    with _engine.connect() as conn:
        return _rows(
            conn.execute(
                text(
                    """
                    SELECT UPPER(sc.scanned_serial) AS serial,
                           MAX(sc.scanned_location) AS last_scanned_location,
                           COUNT(*)                 AS scan_count,
                           MAX(sc.scanned_at)       AS last_scanned_at,
                           MAX(sc.session_id)       AS last_session_id
                    FROM audit_scans sc
                    JOIN audit_sessions s ON s.id = sc.session_id
                    WHERE sc.expected_id IS NULL
                      AND sc.scanned_at >= now() - make_interval(days => :days)
                    GROUP BY UPPER(sc.scanned_serial)
                    ORDER BY MAX(sc.scanned_at) DESC
                    LIMIT :limit
                    """
                ),
                {"days": days, "limit": limit},
            )
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def build_target(kind: str, warehouse: Optional[str] = None, location: Optional[str] = None) -> dict[str, Any]:
    """Normalize a session target.

    kind: 'all' (full audit), 'tied' (tied-WO group), 'warehouse', 'location'.
    Returns {kind, warehouse, location, scope, label}.
    """
    kind = (kind or "all").strip().lower()
    warehouse = (warehouse or "").strip() or None
    location = (location or "").strip() or None
    if kind == "tied":
        return {"kind": "tied", "warehouse": None, "location": None,
                "scope": TIED_WO_SCOPE, "label": TIED_WO_LABEL}
    if kind == "warehouse":
        if not warehouse:
            raise ValueError("A warehouse is required for a warehouse audit.")
        return {"kind": "warehouse", "warehouse": warehouse, "location": None,
                "scope": warehouse, "label": f"{warehouse} warehouse (all locations)"}
    if kind == "location":
        if not warehouse or not location:
            raise ValueError("A warehouse and location are required for a location audit.")
        return {"kind": "location", "warehouse": warehouse, "location": location,
                "scope": location_key(warehouse, location),
                "label": f"{warehouse} / {location}"}
    if kind == "all":
        return {"kind": "all", "warehouse": None, "location": None,
                "scope": None, "label": "Full audit (all locations)"}
    raise ValueError(f"Unknown audit target type: {kind!r}.")


def target_exists(target: dict[str, Any]) -> bool:
    """True when the target's warehouse/location is a known, active audit location.

    Full and tied-WO audits need no lookup. Guards against stale dashboard
    forms racing the ERP sync: a just-deactivated bin should refuse to start
    an (empty) session rather than silently audit nothing.
    """
    if target["kind"] in ("all", "tied"):
        return True
    if not _available:
        return False
    with _engine.connect() as conn:
        if target["kind"] == "warehouse":
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM audit_locations "
                        "WHERE warehouse_id = :wh AND active LIMIT 1"
                    ),
                    {"wh": target["warehouse"]},
                ).first()
            )
        return bool(
            conn.execute(
                text("SELECT 1 FROM audit_locations WHERE scope = :scope AND active LIMIT 1"),
                {"scope": location_key(target["warehouse"], target["location"])},
            ).first()
        )


def list_active_location_ids() -> list[str]:
    """Location ids of every active auditable bin (for barcode recognition)."""
    if not _available:
        return []
    with _engine.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT DISTINCT location_id FROM audit_locations "
                    "WHERE location_id IS NOT NULL AND active"
                )
            )
        ]


def _row_in_scope(row: dict[str, Any], target: dict[str, Any]) -> bool:
    scope = (row.get("SCOPE") or "").strip()
    if target["kind"] == "all":
        return True
    if target["kind"] == "tied":
        return scope == TIED_WO_SCOPE
    if scope == TIED_WO_SCOPE:
        return False
    if target["kind"] == "warehouse":
        return (row.get("EXPECTED_WAREHOUSE") or "").strip() == target["warehouse"]
    return (
        (row.get("EXPECTED_WAREHOUSE") or "").strip() == target["warehouse"]
        and (row.get("EXPECTED_LOCATION") or "").strip() == target["location"]
    )


def start_session(target: dict[str, Any], expected_rows: list[dict[str, Any]], operator: Optional[str]) -> int:
    """Create an in-progress session and snapshot the expected serials into it.

    The FULL serial universe is snapshotted; rows matching the target are
    flagged in_scope. Out-of-scope rows let a scan of a stray gun report its
    true home ("misplaced — belongs in R07S03") instead of just "unexpected".
    Counts and accuracy consider only in-scope rows.
    """
    if not _available:
        raise RuntimeError("Audit store is not available.")
    in_scope_count = sum(1 for r in expected_rows if _row_in_scope(r, target))
    with _engine.begin() as conn:
        session_id = conn.execute(
            text(
                """
                INSERT INTO audit_sessions
                    (scope, label, target_warehouse, target_location, status, operator, expected_count)
                VALUES
                    (:scope, :label, :target_warehouse, :target_location, 'in_progress', :operator, :expected_count)
                RETURNING id
                """
            ),
            {
                "scope": target["scope"],
                "label": target["label"],
                "target_warehouse": target["warehouse"],
                "target_location": target["location"],
                "operator": operator,
                "expected_count": in_scope_count,
            },
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
                    "in_scope": _row_in_scope(r, target),
                }
                for r in expected_rows
            ]
            conn.execute(
                text(
                    """
                    INSERT INTO audit_expected (
                        session_id, serial, part_id, part_description, product_code,
                        expected_warehouse, expected_location, scope, tied_wo,
                        cust_order_id, customer_id, in_scope
                    ) VALUES (
                        :session_id, :serial, :part_id, :part_description, :product_code,
                        :expected_warehouse, :expected_location, :scope, :tied_wo,
                        :cust_order_id, :customer_id, :in_scope
                    )
                    """
                ),
                params,
            )
    invalidate_read_cache()
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
                           cust_order_id, customer_id, status, scanned_location,
                           scanned_at, in_scope
                    FROM audit_expected
                    WHERE session_id = :id
                      AND (in_scope OR status <> 'pending')
                    ORDER BY in_scope DESC, scope, expected_location, serial
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
    """Live counts derived from expected statuses + unexpected scans.

    expected/verified/pending/missing consider only in-scope serials (the ones
    this session is auditing). misplaced counts every snapshot row scanned in
    the wrong place — including out-of-scope guns found on the audited shelf.
    """
    if not _available:
        return {}
    with _engine.connect() as conn:
        status_rows = conn.execute(
            text(
                """
                SELECT status, in_scope, COUNT(*) AS n
                FROM audit_expected
                WHERE session_id = :id
                GROUP BY status, in_scope
                """
            ),
            {"id": session_id},
        ).all()
        in_scope_by_status: dict[str, int] = {}
        misplaced_total = 0
        for status, in_scope, n in status_rows:
            if status == "misplaced":
                misplaced_total += n
            if in_scope:
                in_scope_by_status[status] = in_scope_by_status.get(status, 0) + n
        expected_total = sum(in_scope_by_status.values())
        unexpected = conn.execute(
            text(
                "SELECT COUNT(DISTINCT scanned_serial) FROM audit_scans WHERE session_id = :id AND expected_id IS NULL"
            ),
            {"id": session_id},
        ).scalar_one()
    return {
        "expected": expected_total,
        "verified": in_scope_by_status.get("verified", 0),
        "misplaced": misplaced_total,
        "pending": in_scope_by_status.get("pending", 0),
        "missing": in_scope_by_status.get("missing", 0),
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
                SELECT id, serial, expected_location, scope, tied_wo, status,
                       part_description, in_scope
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
            # Tied-WO rows carry their sales order id as the expected location
            # (units are staged by SO), so scanning the SO barcode then the
            # serial validates the allocation. A row with no expected location
            # at all (rare) verifies anywhere — presence is all we can check.
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
                "UPDATE audit_expected SET status = 'missing' "
                "WHERE session_id = :id AND status = 'pending' AND in_scope"
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
    invalidate_read_cache()
    return get_session(session_id)
