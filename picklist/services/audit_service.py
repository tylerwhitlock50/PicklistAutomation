"""Serialized inventory audit: ERP fetches, location sync, dwell and analytics."""
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from flask import flash, jsonify, render_template, request
from sqlalchemy import text

from picklist.config import (
    AUDIT_DWELL_QUERY_FILE,
    AUDIT_LOCATION_SYNC_MAX_AGE_MINUTES,
    AUDIT_LOCATIONS_SYNC_FILE,
    AUDIT_QUERY_FILE,
    STAGING_TARGET_HOURS,
    logger,
)
from picklist.domain import audit_universe
from picklist.erp import get_erp_engine, run_audit_sql_file
from picklist.stores import audit_store


def fetch_audit_expected() -> pd.DataFrame:
    """Full serialized expected-inventory universe (all locations + tied WOs)."""
    return run_audit_sql_file(AUDIT_QUERY_FILE, "serialized audit expected query")


def fetch_serial_onhand_locations(serials: list[str]) -> dict[str, list[dict]]:
    """Live ERP on-hand balances for a set of serials.

    Returns {SERIAL: [{warehouse, location, qty, last_txn_at}, ...]} keeping
    only positive net balances — the same inference the expected-list query
    uses, but at request time instead of snapshot time.
    """
    serials = sorted({(s or "").strip().upper() for s in serials if (s or "").strip()})
    if not serials:
        return {}
    binds = {f"s{i}": s for i, s in enumerate(serials)}
    placeholders = ", ".join(f":{k}" for k in binds)
    query = text(
        f"""
        SELECT UPPER(tit.TRACE_ID) AS SERIAL_NO,
               it.WAREHOUSE_ID, it.LOCATION_ID,
               SUM(tit.QTY) AS NET_QTY,
               MAX(it.CREATE_DATE) AS LAST_TXN_AT
        FROM dbo.TRACE_INV_TRANS tit
        INNER JOIN dbo.INVENTORY_TRANS it
            ON it.TRANSACTION_ID = tit.TRANSACTION_ID
           AND it.PART_ID        = tit.PART_ID
        WHERE UPPER(tit.TRACE_ID) IN ({placeholders})
        GROUP BY UPPER(tit.TRACE_ID), it.WAREHOUSE_ID, it.LOCATION_ID
        HAVING SUM(tit.QTY) > 0
        """
    )
    engine = get_erp_engine()
    result: dict[str, list[dict]] = {}
    with engine.connect() as connection:
        for row in connection.execute(query, binds).mappings():
            result.setdefault(row["SERIAL_NO"], []).append(
                {
                    "warehouse": row["WAREHOUSE_ID"],
                    "location": row["LOCATION_ID"],
                    "qty": float(row["NET_QTY"]),
                    "last_txn_at": row["LAST_TXN_AT"],
                }
            )
    return result


def recheck_unexpected_scans(session_id: int) -> list[dict]:
    """Re-query the ERP for a session's unexpected serials.

    The snapshot is point-in-time: a WO receipt posted after the session
    started makes a perfectly-placed gun scan as "unexpected". For each
    unexpected serial now on hand in the scanned location, record an automatic
    'erp_synced' resolution. Returns one summary row per unexpected serial.
    """
    unexpected = audit_store.get_unexpected_scans(session_id)
    if not unexpected:
        return []
    onhand = fetch_serial_onhand_locations([r["scanned_serial"] for r in unexpected])
    summary = []
    for row in unexpected:
        serial = (row.get("scanned_serial") or "").strip().upper()
        scanned_loc = (row.get("scanned_location") or "").strip().upper()
        balances = onhand.get(serial, [])
        matched = next(
            (b for b in balances if (b["location"] or "").strip().upper() == scanned_loc),
            None,
        )
        auto_resolved = False
        if matched:
            posted = matched["last_txn_at"]
            posted_str = posted.strftime("%Y-%m-%d %H:%M") if hasattr(posted, "strftime") else str(posted)
            note = (
                f"ERP re-check: now on hand in {matched['warehouse']}/{matched['location']} "
                f"(last transaction {posted_str} ERP time) — receipt posted after the audit snapshot."
            )
            auto_resolved = audit_store.auto_resolve_unexpected(session_id, serial, note)
        summary.append(
            {
                "serial": serial,
                "scanned_location": scanned_loc,
                "erp_locations": [
                    f"{b['warehouse']}/{b['location']}" for b in balances
                ],
                "now_in_scanned_location": matched is not None,
                "auto_resolved": auto_resolved,
            }
        )
    return summary


# Guards against overlapping background syncs when several dashboard requests
# cross the TTL at once.
_audit_sync_running = threading.Lock()


def _run_audit_location_sync() -> None:
    df = run_audit_sql_file(AUDIT_LOCATIONS_SYNC_FILE, "audit location sync query")
    audit_store.sync_locations(df.to_dict(orient="records"))


def _background_audit_location_sync() -> None:
    try:
        _run_audit_location_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Background audit location sync failed; keeping stored locations. (%s)", exc)
    finally:
        _audit_sync_running.release()


def sync_audit_locations_from_erp(force: bool = False) -> Optional[str]:
    """Refresh the auditable-location list from the ERP if stale.

    The routine TTL refresh runs in a background thread so no page render
    blocks on the ERP aggregation — the triggering request serves the stored
    list. Two cases run synchronously and return an error message on failure:
    force=True (the dashboard's "Refresh now" link) and a store that has never
    synced (there is nothing stored to show yet).
    """
    if not audit_store.is_available():
        return None
    never_synced = audit_store.last_synced_at() is None
    if force or never_synced:
        try:
            _run_audit_location_sync()
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Audit location sync failed; showing stored locations. (%s)", exc)
            return f"Could not refresh locations from the ERP: {exc}"
    if not audit_store.needs_sync(AUDIT_LOCATION_SYNC_MAX_AGE_MINUTES):
        return None
    if _audit_sync_running.acquire(blocking=False):
        threading.Thread(
            target=_background_audit_location_sync,
            name="audit-location-sync",
            daemon=True,
        ).start()
    return None


def check_audit_universe_sql() -> None:
    """Warn when an audit SQL file has drifted from the canonical bin universe.

    The filter is hardcoded in each file (they must stay runnable in SSMS);
    this catches the add-a-cage-but-miss-a-file mistake at boot instead of as
    silently wrong audit results.
    """
    for sql_file in (AUDIT_QUERY_FILE, AUDIT_LOCATIONS_SYNC_FILE, AUDIT_DWELL_QUERY_FILE):
        try:
            missing = audit_universe.missing_terms(sql_file.read_text(encoding="utf-8"))
        except OSError:
            continue  # missing file surfaces later with a clear error of its own
        if missing:
            logger.warning(
                "%s is missing audited-universe terms (%s) — update it to match audit_universe.py.",
                sql_file.name,
                ", ".join(missing),
            )


def _audit_unavailable_response():
    """Consistent handling when the Postgres audit store is not configured."""
    if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503
    flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
    return render_template(
        "audit.html",
        audit_available=False,
        warehouses=[],
        tied_row=None,
        recent_sessions=[],
        due_locations=[],
        sync_error=None,
        last_synced_display=None,
    )


AUDIT_UNAVAILABLE_MESSAGE = (
    "The serialized audit feature is unavailable because the Postgres store "
    "(DATABASE_URL) is not configured or could not be reached."
)


# Accuracy every completed audit is held against on the analytics page.
AUDIT_ACCURACY_TARGET_PCT = float(os.getenv("AUDIT_ACCURACY_TARGET_PCT", "99"))


AUDIT_ANALYTICS_DEFAULT_DAYS = 30


# Dwell time: guns should clear the staging bins within this many hours.
AUDIT_DWELL_TARGET_HOURS = STAGING_TARGET_HOURS


# Warehouse/location pairs the clearance metric watches.
AUDIT_DWELL_LOCATIONS = [
    tuple(pair.strip().upper().split("/", 1))
    for pair in os.getenv(
        "AUDIT_DWELL_LOCATIONS",
        "MAIN/C2-SERIALIZED,SHIPPING/STAGE,SHIPPING/STAGE1,SHIPPING/STAGE2,SHIPPING/STAGE3",
    ).split(",")
    if "/" in pair
]


AUDIT_DWELL_CACHE_MINUTES = float(os.getenv("AUDIT_DWELL_CACHE_MINUTES", "10"))


_dwell_cache: dict[str, Any] = {"df": None, "fetched_at": None}


def _dwell_age_display(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f} d"
    return f"{hours:.0f} h"


def _fetch_dwell_raw() -> pd.DataFrame:
    """Dwell rows for the whole serialized universe, cached briefly (ERP ~5s).

    The dwell SQL returns every SHIPPING location plus the MAIN cage bins;
    both the audit dwell metric and shipping stage aging filter this one
    cached snapshot. dwell_hours is computed against the ERP server's own
    clock (ERP_NOW), which is on the same clock as CREATE_DATE — the app
    host's timezone never enters into it. Ages drift up to the cache TTL;
    the pages show the snapshot time.
    """
    cached_at = _dwell_cache["fetched_at"]
    if (
        _dwell_cache["df"] is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=AUDIT_DWELL_CACHE_MINUTES)
    ):
        return _dwell_cache["df"]
    df = run_audit_sql_file(AUDIT_DWELL_QUERY_FILE, "serialized dwell-time query")
    df["ARRIVED_AT"] = pd.to_datetime(df["ARRIVED_AT"])
    df["ERP_NOW"] = pd.to_datetime(df["ERP_NOW"])
    df["dwell_hours"] = (
        (df["ERP_NOW"] - df["ARRIVED_AT"]).dt.total_seconds() / 3600.0
    ).clip(lower=0.0)
    _dwell_cache["df"] = df
    _dwell_cache["fetched_at"] = datetime.now()
    return df


def fetch_dwell_df() -> pd.DataFrame:
    """Dwell rows filtered to the audit-watched staging locations."""
    df = _fetch_dwell_raw()
    watched = {f"{wh}/{loc}" for wh, loc in AUDIT_DWELL_LOCATIONS}
    keys = (
        df["WAREHOUSE_ID"].astype(str).str.upper()
        + "/"
        + df["LOCATION_ID"].astype(str).str.upper()
    )
    return df[keys.isin(watched)].copy()


def build_dwell_metrics() -> dict[str, Any]:
    """Snapshot of how long guns have sat in the watched staging locations.

    Unlike the rest of the analytics payload this is a *current* snapshot from
    the ERP, not a windowed aggregate. Failure to reach the ERP degrades to an
    error message so the audit analytics (Postgres-backed) still render.
    """
    target = AUDIT_DWELL_TARGET_HOURS
    result: dict[str, Any] = {
        "target_hours": target,
        "locations": [f"{wh}/{loc}" for wh, loc in AUDIT_DWELL_LOCATIONS],
        "error": None,
    }
    try:
        df = fetch_dwell_df()
    except Exception as exc:  # ERP down/misconfigured — degrade, don't 500
        logger.exception("Dwell-time query failed")
        result["error"] = str(exc)
        return result
    result["as_of"] = (
        df["ERP_NOW"].max().to_pydatetime() if len(df) else datetime.now()
    )

    over = df[df["dwell_hours"] > target].sort_values("ARRIVED_AT")

    total = int(len(df))
    over_count = int(len(over))
    result["summary"] = {
        "on_hand": total,
        "within_target": total - over_count,
        "over_target": over_count,
        "clearance_pct": round(100.0 * (total - over_count) / total, 1) if total else None,
        "median_dwell_hours": round(float(df["dwell_hours"].median()), 1) if total else None,
        "oldest_dwell_hours": round(float(df["dwell_hours"].max()), 1) if total else None,
    }

    by_location = []
    for label in result["locations"]:
        wh, loc = label.split("/", 1)
        sub = df[
            (df["WAREHOUSE_ID"].str.upper() == wh) & (df["LOCATION_ID"].str.upper() == loc)
        ]
        loc_over = int((sub["dwell_hours"] > target).sum())
        oldest_hours = round(float(sub["dwell_hours"].max()), 1) if len(sub) else None
        by_location.append(
            {
                "location": label,
                "on_hand": int(len(sub)),
                "over_target": loc_over,
                "oldest_dwell_hours": oldest_hours,
                "oldest_display": (
                    _dwell_age_display(oldest_hours) if oldest_hours is not None else None
                ),
            }
        )
    result["by_location"] = by_location

    result["aged_serials"] = [
        {
            "serial": row["SERIAL_NO"],
            "part_id": row["PART_ID"],
            "part_description": row["PART_DESCRIPTION"],
            "location": f"{row['WAREHOUSE_ID']}/{row['LOCATION_ID']}",
            "arrived_at": row["ARRIVED_AT"].to_pydatetime(),
            "dwell_hours": round(float(row["dwell_hours"]), 1),
            "age_display": _dwell_age_display(float(row["dwell_hours"])),
        }
        for _, row in over.iterrows()
    ]
    return result


def _audit_analytics_window() -> int:
    try:
        days = int(request.args.get("days", AUDIT_ANALYTICS_DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = AUDIT_ANALYTICS_DEFAULT_DAYS
    return max(1, min(days, 365))


def build_audit_analytics(days: int) -> dict[str, Any]:
    """Everything the analytics dashboard / endpoint reports for the window."""
    sessions = audit_store.completed_sessions_since(days)
    serials_audited = sum(r.get("expected_count") or 0 for r in sessions)
    verified = sum(r.get("verified_count") or 0 for r in sessions)
    misplaced = sum(r.get("misplaced_count") or 0 for r in sessions)
    missing = sum(r.get("missing_count") or 0 for r in sessions)
    unexpected = sum(r.get("unexpected_count") or 0 for r in sessions)
    weighted_accuracy = (
        round(100.0 * verified / serials_audited, 2) if serials_audited else None
    )
    session_accuracies = [
        float(r["accuracy_pct"]) for r in sessions if r.get("accuracy_pct") is not None
    ]
    avg_session_accuracy = (
        round(sum(session_accuracies) / len(session_accuracies), 2)
        if session_accuracies
        else None
    )

    locations = audit_store.list_location_status()
    locations_active = len(locations)
    locations_due = sum(1 for loc in locations if loc.get("due"))
    cadence_compliance = (
        round(100.0 * (locations_active - locations_due) / locations_active, 1)
        if locations_active
        else None
    )

    open_exceptions = audit_store.current_exceptions()
    open_missing = sum(1 for r in open_exceptions if r.get("status") == "missing")
    open_misplaced = sum(1 for r in open_exceptions if r.get("status") == "misplaced")

    return {
        "window_days": days,
        "target_accuracy_pct": AUDIT_ACCURACY_TARGET_PCT,
        "generated_at": datetime.now(timezone.utc),
        "summary": {
            "sessions_completed": len(sessions),
            "serials_audited": serials_audited,
            "verified": verified,
            "misplaced": misplaced,
            "missing": missing,
            "unexpected": unexpected,
            "weighted_accuracy_pct": weighted_accuracy,
            "avg_session_accuracy_pct": avg_session_accuracy,
            "meets_target": (
                weighted_accuracy is not None
                and weighted_accuracy >= AUDIT_ACCURACY_TARGET_PCT
            ),
            "locations_active": locations_active,
            "locations_due": locations_due,
            "cadence_compliance_pct": cadence_compliance,
            "open_missing": open_missing,
            "open_misplaced": open_misplaced,
        },
        "sessions": sessions,
        "open_exceptions": open_exceptions,
        "problem_locations": audit_store.location_error_breakdown(days),
        "problem_serials": audit_store.repeat_offender_serials(days),
        "unexpected_serials": audit_store.unexpected_serials_since(days),
        "dwell": build_dwell_metrics(),
    }
