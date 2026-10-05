"""Shipping reports: stage aging, shortages, release gate, scorecard, excess, recon, verify daily."""
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from picklist.config import (
    EXCESS_CACHE_MINUTES,
    EXCESS_PACKLISTS_FILE,
    logger,
    PACKLIST_DAILY_FILE,
    QUERY_FILES,
    RECON_SHIPMENTS_FILE,
    RELEASE_CANDIDATES_FILE,
    RELEASE_COMPONENT_CODES_TOKEN,
    RELEASE_EXCLUDED_CUSTOMERS_TOKEN,
    RELEASE_GATE_CACHE_SECONDS,
    RELEASE_LOOKAHEAD_TOKEN,
    RELEASE_SERIAL_PART_FILTER_TOKEN,
    RELEASE_SERIAL_SUPPLY_FILE,
    RELEASE_SHIPTO_HISTORY_FILE,
    SHIPPING_METRICS_CACHE_MINUTES,
    SHIPPING_METRICS_FILE,
    SHORTAGE_CACHE_MINUTES,
    SHORTAGE_LOOKAHEAD_DAYS,
    SHORTAGE_PRODUCT_CODES,
    SHORTAGE_PRODUCT_CODES_TOKEN,
    SHORTAGE_QUERY_FILE,
    STAGE_AGING_TARGET_HOURS,
    STAGE_LOCATION_TERM,
    VERIFY_DAILY_CACHE_SECONDS,
)
from picklist.domain import excess, recon, release_gate, shipping_metrics, shortage
from picklist.erp import get_erp_engine, run_erp_query_file
from picklist.services.audit_service import _dwell_age_display, _fetch_dwell_raw
from picklist.services.query_options import get_default_guns_query_options
from picklist.services.run_history import (
    get_latest_successful_run,
    get_latest_successful_run_summary,
    get_plan_snapshots,
    list_plan_dates,
)
from picklist.services.settings_service import (
    ensure_release_gate_policy_version,
    get_excess_packlist_cost,
    get_release_gate_min_ship_to_cooldown_days,
)
from picklist.stores import pick_store, request_store, shipping_store, verify_store
from picklist.timeutil import (
    _audit_dt_display,
    _erp_local_time_display,
    _today_local,
    format_run_timestamp,
    resolve_timezone,
)
from picklist.util import sql_quote_literal


def _active_manual_holds() -> list[dict]:
    """Manual holds from approved set-aside / exception requests."""
    try:
        return request_store.active_manual_holds()
    except Exception:  # noqa: BLE001 - never let the hold ledger break a page
        logger.exception("Manual hold read failed")
        return []


def invalidate_release_gate_cache() -> None:
    _release_gate_cache.update({"payload": None, "fetched_at": None, "signature": None})


def build_stage_aging() -> dict[str, Any]:
    """How long inventory has been sitting in the SHIPPING stage bins.

    Same live-ERP snapshot the audit dwell metric uses (one cached query),
    but scoped to every SHIPPING location whose ID contains
    STAGE_LOCATION_TERM — staged goods are sold and boxed, so anything aging
    here is an order that has not actually left.
    """
    target = STAGE_AGING_TARGET_HOURS
    result: dict[str, Any] = {
        "target_hours": target,
        "term": STAGE_LOCATION_TERM,
        "error": None,
    }
    try:
        raw = _fetch_dwell_raw()
    except Exception as exc:  # ERP down/misconfigured — degrade, don't 500
        logger.exception("Stage aging query failed")
        result["error"] = str(exc)
        return result

    mask = (
        raw["WAREHOUSE_ID"].astype(str).str.upper().eq("SHIPPING")
        & raw["LOCATION_ID"].astype(str).str.upper().str.contains(
            STAGE_LOCATION_TERM, regex=False
        )
    )
    df = raw[mask].copy()
    result["as_of"] = (
        raw["ERP_NOW"].max().to_pydatetime() if len(raw) else datetime.now()
    )

    total = int(len(df))
    over = df[df["dwell_hours"] > target].sort_values("ARRIVED_AT")
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
    if total:
        for location_id, sub in df.groupby(df["LOCATION_ID"].astype(str).str.upper()):
            loc_over = int((sub["dwell_hours"] > target).sum())
            oldest_hours = round(float(sub["dwell_hours"].max()), 1)
            by_location.append(
                {
                    "location": f"SHIPPING/{location_id}",
                    "on_hand": int(len(sub)),
                    "over_target": loc_over,
                    "oldest_dwell_hours": oldest_hours,
                    "oldest_display": _dwell_age_display(oldest_hours),
                }
            )
        by_location.sort(key=lambda row: row["location"])
    result["by_location"] = by_location

    result["aged_serials"] = [
        {
            "serial": row["SERIAL_NO"],
            "part_id": row["PART_ID"],
            "part_description": row["PART_DESCRIPTION"],
            "location": f"{row['WAREHOUSE_ID']}/{row['LOCATION_ID']}",
            "arrived_at": row["ARRIVED_AT"].to_pydatetime(),
            "arrived_display": row["ARRIVED_AT"].strftime("%Y-%m-%d %H:%M"),
            "dwell_hours": round(float(row["dwell_hours"]), 1),
            "age_display": _dwell_age_display(float(row["dwell_hours"])),
        }
        for _, row in over.iterrows()
    ]
    return result


_shortage_cache: dict[str, Any] = {"payload": None, "fetched_at": None}


def _fetch_shortage_rows() -> list[dict]:
    """Line-level shortage demand from the ERP.

    The product-code list is rendered into the SQL as quoted literals (same
    token pattern as the guns excluded-customers list) because an IN-list
    cannot be a bind parameter; the lookahead is a normal bound value.
    """
    if not SHORTAGE_QUERY_FILE.exists():
        raise FileNotFoundError(f"Shortage query file not found at: {SHORTAGE_QUERY_FILE}")
    template = SHORTAGE_QUERY_FILE.read_text(encoding="utf-8")
    product_code_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(code)} AS PRODUCT_CODE"
        for code in SHORTAGE_PRODUCT_CODES
    )
    query = template.replace(SHORTAGE_PRODUCT_CODES_TOKEN, product_code_rows)
    engine = get_erp_engine()
    logger.info("Running component shortage query")
    with engine.connect() as connection:
        df = pd.read_sql_query(
            text(query), connection, params={"lookahead_days": SHORTAGE_LOOKAHEAD_DAYS}
        )
        logger.info("Component shortage query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def build_shortage_payload(force: bool = False) -> dict[str, Any]:
    """Cached shortage payload; degrades to an error message when ERP is down."""
    cached_at = _shortage_cache["fetched_at"]
    if (
        not force
        and _shortage_cache["payload"] is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=SHORTAGE_CACHE_MINUTES)
    ):
        return _shortage_cache["payload"]
    try:
        rows = _fetch_shortage_rows()
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Component shortage query failed")
        return {
            "error": str(exc),
            "summary": None,
            "lines": [],
            "transfers": [],
            "lookahead_days": SHORTAGE_LOOKAHEAD_DAYS,
        }
    payload = shortage.build_shortage(rows, SHORTAGE_LOOKAHEAD_DAYS)
    payload["error"] = None
    payload["lookahead_days"] = SHORTAGE_LOOKAHEAD_DAYS
    payload["as_of"] = datetime.now(timezone.utc)
    _shortage_cache["payload"] = payload
    _shortage_cache["fetched_at"] = datetime.now()
    return payload


def render_release_candidates_query(
    query_template: str,
    query_options: Optional[dict[str, Any]] = None,
) -> str:
    options = get_default_guns_query_options()
    if query_options:
        options = {**options, **query_options}
    component_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(code)} AS PRODUCT_CODE"
        for code in SHORTAGE_PRODUCT_CODES
    ) or "SELECT '' AS PRODUCT_CODE"
    excluded_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(term)} AS CUSTOMER_TERM"
        for term in options["excluded_customers"]
    ) or "SELECT '' AS CUSTOMER_TERM"
    return (
        query_template.replace(RELEASE_LOOKAHEAD_TOKEN, str(options["lookahead_days"]))
        .replace(RELEASE_COMPONENT_CODES_TOKEN, component_rows)
        .replace(RELEASE_EXCLUDED_CUSTOMERS_TOKEN, excluded_rows)
    )


_release_gate_cache: dict[str, Any] = {
    "payload": None,
    "fetched_at": None,
    "signature": None,
}


def _fetch_release_candidate_rows(
    query_options: Optional[dict[str, Any]] = None,
) -> list[dict]:
    if not RELEASE_CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Release candidate query not found at: {RELEASE_CANDIDATES_FILE}"
        )
    template = RELEASE_CANDIDATES_FILE.read_text(encoding="utf-8")
    query = render_release_candidates_query(template, query_options=query_options)
    engine = get_erp_engine()
    logger.info("Running order release candidate query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Order release candidate query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def _fetch_release_serial_rows(part_ids: Iterable[str]) -> list[dict]:
    if not RELEASE_SERIAL_SUPPLY_FILE.exists():
        raise FileNotFoundError(
            f"Release serial-supply query not found at: {RELEASE_SERIAL_SUPPLY_FILE}"
        )
    parts = sorted({str(part).strip().upper() for part in part_ids if str(part).strip()})
    template = RELEASE_SERIAL_SUPPLY_FILE.read_text(encoding="utf-8")
    if RELEASE_SERIAL_PART_FILTER_TOKEN not in template:
        raise ValueError(
            f"Release serial query must contain {RELEASE_SERIAL_PART_FILTER_TOKEN}."
        )
    if parts:
        literals = ", ".join(sql_quote_literal(part) for part in parts)
        part_filter = f"AND tit.PART_ID IN ({literals})"
    else:
        part_filter = "AND 1 = 0"
    query = template.replace(RELEASE_SERIAL_PART_FILTER_TOKEN, part_filter)
    engine = get_erp_engine()
    logger.info("Running release-gate serial supply query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Release serial-supply query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def _fetch_release_shipto_history_rows() -> list[dict]:
    if not RELEASE_SHIPTO_HISTORY_FILE.exists():
        raise FileNotFoundError(
            f"Release ship-to history query not found at: {RELEASE_SHIPTO_HISTORY_FILE}"
        )
    query = RELEASE_SHIPTO_HISTORY_FILE.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running release-gate ship-to history query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Release ship-to history query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def build_release_gate_payload(
    force: bool = False,
    query_options: Optional[dict[str, Any]] = None,
    require_serial_tracking: bool = False,
    persist: bool = False,
) -> dict[str, Any]:
    # persist=True is reserved for the operational boundary (picklist generation):
    # only then are sticky serial reservations reconciled and an audit evaluation
    # recorded. Dashboard and API reads stay side-effect free.
    policy = ensure_release_gate_policy_version()
    signature = json.dumps(
        {"version": policy["version"], "query_options": query_options or {}},
        sort_keys=True,
        default=str,
    )
    cached_at = _release_gate_cache["fetched_at"]
    if (
        not force
        and _release_gate_cache["payload"] is not None
        and _release_gate_cache["signature"] == signature
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(seconds=RELEASE_GATE_CACHE_SECONDS)
    ):
        return _release_gate_cache["payload"]

    evaluated_at = datetime.now(timezone.utc)
    source_as_of = evaluated_at.isoformat()
    try:
        rows = _fetch_release_candidate_rows(query_options=query_options)
        gun_parts = {
            str(row.get("PART_ID") or "").strip().upper()
            for row in rows
            if str(row.get("ITEM_TYPE") or "guns").strip().lower() == "guns"
            and float(row.get("AVAILABLE_QTY") or 0) > 0
            and str(row.get("PART_ID") or "").strip()
        }
        try:
            serial_rows: Optional[list[dict]] = _fetch_release_serial_rows(gun_parts)
            serial_error = None
        except Exception as serial_exc:  # noqa: BLE001 - advisory may show quantity fallback
            logger.exception("Release-gate serial supply query failed")
            serial_rows = None
            serial_error = str(serial_exc)
            if require_serial_tracking:
                raise RuntimeError(
                    "Live serial reservation validation is required for enforced picklists: "
                    f"{serial_exc}"
                ) from serial_exc
        try:
            ship_to_history: Optional[list[dict]] = _fetch_release_shipto_history_rows()
            ship_to_history_error = None
        except Exception as ship_to_exc:  # noqa: BLE001 - advisory exposes degraded check
            logger.exception("Release-gate ship-to history query failed")
            ship_to_history = None
            ship_to_history_error = str(ship_to_exc)
            if require_serial_tracking:
                raise RuntimeError(
                    "Live ship-to cooldown validation is required for enforced picklists: "
                    f"{ship_to_exc}"
                ) from ship_to_exc
        if ship_to_history is not None:
            # A picklist generated earlier today has no SHIPPED_DATE in VISUAL yet;
            # the local release ledger closes that same-day duplicate window.
            try:
                ship_to_history = list(ship_to_history) + (
                    shipping_store.recent_released_ship_tos()
                )
            except Exception:  # noqa: BLE001 - ledger is supplemental history
                logger.exception("Local release ledger read failed")
        existing_reservations = shipping_store.serial_reservations_for_gate()
        payload = release_gate.evaluate_release_gate(
            rows,
            today=_today_local(),
            mode=policy["mode"],
            due_override_days=policy["due_override_days"],
            customer_policies=policy["customer_policies"],
            exceptions=shipping_store.active_exceptions(),
            serial_inventory=serial_rows,
            reservations=existing_reservations,
            ship_to_history=ship_to_history,
            local_now=evaluated_at.astimezone(resolve_timezone()),
            policy_version=policy["version"],
            evaluated_at=evaluated_at,
            min_ship_to_cooldown_days=get_release_gate_min_ship_to_cooldown_days(),
            manual_holds=_active_manual_holds(),
        )
        if persist and serial_rows is not None:
            payload["reservation_sync"] = shipping_store.sync_serial_reservations(
                desired=payload.get("reservation_updates") or [],
                live_serials=serial_rows,
                evaluated_at=payload["evaluated_at"],
                policy_version=policy["version"],
            )
        else:
            payload["reservation_sync"] = None
        payload["serial_tracking_error"] = serial_error
        payload["ship_to_tracking_available"] = ship_to_history is not None
        payload["ship_to_tracking_error"] = ship_to_history_error
        payload["serial_reservations"] = shipping_store.recent_serial_reservations()
        payload["source_as_of"] = source_as_of
        payload["error"] = None
        payload["active_exceptions"] = shipping_store.active_exceptions()
        payload["evaluation_id"] = (
            shipping_store.record_evaluation(payload, source_as_of=source_as_of)
            if persist
            else None
        )
    except Exception as exc:  # noqa: BLE001 - dashboard degrades; enforced runs fail closed
        logger.exception("Release gate evaluation failed")
        payload = {
            "mode": policy["mode"],
            "policy_version": policy["version"],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "source_as_of": source_as_of,
            "due_override_days": policy["due_override_days"],
            "summary": {
                "orders": 0,
                "release": 0,
                "accumulating": 0,
                "hold": 0,
                "blocked": 0,
                "release_units": 0,
                "protected_units": 0,
                "protected_guns": 0,
                "held_ready_units": 0,
            },
            "released_orders": [],
            "decisions": [],
            "active_exceptions": shipping_store.active_exceptions(),
            "serial_tracking_available": False,
            "serial_tracking_error": str(exc),
            "ship_to_tracking_available": False,
            "ship_to_tracking_error": str(exc),
            "serial_reservations": shipping_store.recent_serial_reservations(),
            "reservation_updates": [],
            "error": str(exc),
        }
    _release_gate_cache.update(
        {"payload": payload, "fetched_at": datetime.now(), "signature": signature}
    )
    return payload


_shipping_scorecard_cache: dict[tuple[int, str], dict[str, Any]] = {}


def parse_scorecard_days(raw: Any) -> int:
    try:
        value = int(str(raw or "30").strip())
    except (TypeError, ValueError):
        return 30
    return value if value in {1, 7, 30, 90} else 30


def _fetch_shipping_metric_rows(start: date, end: date) -> list[dict]:
    span = end - start
    query_start = start - span
    df = run_erp_query_file(
        SHIPPING_METRICS_FILE,
        {"query_start": query_start.isoformat(), "end_date": end.isoformat()},
        "shipping management metrics",
    )
    return df.to_dict(orient="records")


def _empty_scorecard_payload(days: int, error: str) -> dict[str, Any]:
    today = _today_local()
    cards = {
        key: {
            **definition,
            "value": None,
            "prior_value": None,
            "delta": None,
            "numerator": None,
            "denominator": None,
            "companion": {},
        }
        for key, definition in shipping_metrics.METRIC_DEFINITIONS.items()
    }
    return {
        "error": error,
        "stale": False,
        "as_of": None,
        "period": {
            "start": (today - timedelta(days=days - 1)).isoformat(),
            "end_exclusive": (today + timedelta(days=1)).isoformat(),
            "days": days,
            "latest_complete_date": (today - timedelta(days=1)).isoformat(),
            "is_partial": True,
        },
        "cards": cards,
        "trends": [],
        "actions": {"late_orders": [], "single_shipments": []},
        "coverage": {},
        "source": {},
    }


def build_shipping_scorecard_payload(days: int = 30, force: bool = False) -> dict[str, Any]:
    days = parse_scorecard_days(days)
    today = _today_local()
    cache_key = (days, today.isoformat())
    cached = _shipping_scorecard_cache.get(cache_key)
    if (
        not force
        and cached
        and datetime.now() - cached["_cached_at"]
        < timedelta(minutes=SHIPPING_METRICS_CACHE_MINUTES)
    ):
        return cached["payload"]

    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    try:
        rows = _fetch_shipping_metric_rows(start, end)
        payload = shipping_metrics.build_shipping_metrics(
            rows, start, end, as_of=datetime.now(timezone.utc)
        )
        payload["period"]["is_partial"] = True
        payload["period"]["latest_complete_date"] = (
            today - timedelta(days=1)
        ).isoformat()
        payload["error"] = None
        payload["stale"] = False
        try:
            shipping_store.save_metric_snapshot(
                snapshot_date=today,
                period_days=days,
                payload=payload,
                source_as_of=payload.get("as_of"),
            )
        except Exception as exc:  # noqa: BLE001 - history should not hide fresh metrics
            logger.warning("Could not save shipping metric snapshot: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Shipping management metric query failed")
        snapshot = shipping_store.get_metric_snapshot(today, days)
        if snapshot:
            payload = snapshot
            payload["error"] = f"Live refresh failed; showing the latest saved snapshot: {exc}"
            payload["stale"] = True
        else:
            payload = _empty_scorecard_payload(days, str(exc))

    _shipping_scorecard_cache[cache_key] = {
        "payload": payload,
        "_cached_at": datetime.now(),
    }
    return payload


_excess_cache: dict[str, Any] = {"payload": None, "fetched_at": None}


def _fetch_excess_rows(today: date) -> list[dict]:
    """SHIPPER headers shipped since the month containing (today - 34 days),
    plus anything created today that has not shipped yet. Starting at a month
    boundary keeps calendar month-to-date fully inside the window even when
    the rolling 34 days would start mid-month."""
    anchor = today - timedelta(days=34)
    df = run_erp_query_file(
        EXCESS_PACKLISTS_FILE,
        {
            "start_date": anchor.replace(day=1).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "today_start": today.isoformat(),
        },
        "excess packlists",
    )
    return df.to_dict(orient="records")


def build_excess_packlist_payload(force: bool = False) -> dict[str, Any]:
    """Cached excess-packlist payload; degrades to an error message when ERP is down."""
    cost = get_excess_packlist_cost()
    cached = _excess_cache["payload"]
    cached_at = _excess_cache["fetched_at"]
    if (
        not force
        and cached is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=EXCESS_CACHE_MINUTES)
        # A cost change in settings invalidates the cache immediately.
        and (cached.get("summary") or {}).get("cost_per_excess") == round(cost, 2)
    ):
        return cached
    today = _today_local()
    try:
        rows = _fetch_excess_rows(today)
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Excess packlist query failed")
        return {
            "error": str(exc),
            "summary": None,
            "fixable": [],
            "groups": [],
        }
    payload = excess.build_excess(rows, today, cost)
    payload["error"] = None
    payload["as_of"] = datetime.now(timezone.utc)
    _excess_cache["payload"] = payload
    _excess_cache["fetched_at"] = datetime.now()
    return payload


def _resolve_recon_date(requested: Optional[str], available: list[str]) -> Optional[str]:
    """Requested date if we have a plan for it; else the newest date before
    today (the 'how did yesterday go' default); else the newest we have."""
    if requested:
        requested = requested.strip()
        if requested in available:
            return requested
        return requested  # let the caller report "no plan for this date"
    today_iso = _today_local().isoformat()
    for plan_date in available:  # newest first
        if plan_date < today_iso:
            return plan_date
    return available[0] if available else None


def build_recon_payload(requested_date: Optional[str]) -> dict[str, Any]:
    """Reconciliation payload for the shipping page / API."""
    available = list_plan_dates()
    plan_date_iso = _resolve_recon_date(requested_date, available)
    payload: dict[str, Any] = {
        "available_dates": available,
        "plan_date": plan_date_iso,
        "error": None,
    }
    if not plan_date_iso:
        payload["error"] = "no_plans"
        payload["message"] = (
            "No picklist plans have been captured yet. Reconciliation starts "
            "working after the next successful picklist run."
        )
        return payload

    try:
        plan_day = date.fromisoformat(plan_date_iso)
    except ValueError:
        payload["error"] = "bad_date"
        payload["message"] = f"'{plan_date_iso}' is not a valid date."
        return payload

    plans = get_plan_snapshots(plan_date_iso)
    if not plans:
        payload["error"] = "no_plan_for_date"
        payload["message"] = f"No picklist plan was captured on {plan_date_iso}."
        return payload

    end_day = _today_local() + timedelta(days=1)
    try:
        shipments_df = run_erp_query_file(
            RECON_SHIPMENTS_FILE,
            {"start_date": plan_date_iso, "end_date": end_day.isoformat()},
            "shipping reconciliation shipments",
        )
    except Exception as exc:  # noqa: BLE001 — degrade like the dwell metric
        logger.exception("Reconciliation shipments query failed")
        payload["error"] = "erp_failed"
        payload["message"] = f"Could not load shipments from the ERP: {exc}"
        return payload

    result = recon.build_reconciliation(
        plan_day, plans, shipments_df.to_dict(orient="records")
    )
    result.update(payload)
    for qt, plan in result["plans"].items():
        plan["run_timestamp_display"] = (
            format_run_timestamp(plan["run_timestamp"]) if plan.get("run_timestamp") else None
        )
    return result


# Verify dashboard: only the ERP day pull is cached — the local session join
# is recomputed every request so completed scans show up immediately.
_verify_daily_cache: dict[str, Any] = {"date": None, "rows": None, "fetched_at": None}


def _fetch_packlists_created_on(day_iso: str, force: bool = False) -> list[dict]:
    now = datetime.now()
    if (
        not force
        and _verify_daily_cache["rows"] is not None
        and _verify_daily_cache["date"] == day_iso
        and _verify_daily_cache["fetched_at"] is not None
        and now - _verify_daily_cache["fetched_at"]
        < timedelta(seconds=VERIFY_DAILY_CACHE_SECONDS)
    ):
        return _verify_daily_cache["rows"]
    end_iso = (date.fromisoformat(day_iso) + timedelta(days=1)).isoformat()
    df = run_erp_query_file(
        PACKLIST_DAILY_FILE,
        {"start_date": day_iso, "end_date": end_iso},
        "daily packlists",
    )
    rows = df.to_dict(orient="records")
    _verify_daily_cache.update({"date": day_iso, "rows": rows, "fetched_at": now})
    return rows


def _verify_session_display(session_row: dict) -> dict:
    row = dict(session_row)
    row["started_display"] = _audit_dt_display(row.get("started_at"))
    row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    return row


def _session_started_local_date(session_row: dict) -> Optional[str]:
    started = session_row.get("started_at")
    if not started:
        return None
    try:
        parsed = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(resolve_timezone()).date().isoformat()


def build_verify_daily_payload(
    requested_date: Optional[str], force: bool = False
) -> dict[str, Any]:
    """Every packlist created on the requested day lined up against its
    latest verification session, plus that day's sessions for older packlists."""
    day_iso = (requested_date or "").strip() or _today_local().isoformat()
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        day_iso = _today_local().isoformat()

    payload: dict[str, Any] = {
        "date": day_iso,
        "today": _today_local().isoformat(),
        "error": None,
        "packlists": [],
        "other_sessions": [],
        "summary": {},
    }

    try:
        erp_rows = _fetch_packlists_created_on(day_iso, force=force)
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Daily packlist query failed")
        payload["error"] = f"Could not load packlists from the ERP: {exc}"
        erp_rows = []

    packlist_ids = [str(r.get("PACKLIST_ID") or "") for r in erp_rows]
    sessions_by_packlist = verify_store.latest_sessions_for_packlists(packlist_ids)

    summary = {
        "created": 0,
        "verified_clean": 0,
        "verified_issues": 0,
        "in_progress": 0,
        "not_verified": 0,
        "no_serials": 0,
        "voided": 0,
    }
    for row in erp_rows:
        packlist_id = str(row.get("PACKLIST_ID") or "").strip().upper()
        shipper_status = str(row.get("SHIPPER_STATUS") or "").strip().upper()
        serial_count = int(row.get("SERIAL_COUNT") or 0)
        session_row = sessions_by_packlist.get(packlist_id)
        if shipper_status in ("X", "V"):
            status = "voided"
        elif session_row and session_row.get("status") == "active":
            status = "in_progress"
        elif session_row and session_row.get("status") == "completed":
            status = (
                "verified_clean"
                if session_row.get("outcome") == verify_store.OUTCOME_CLEAN
                else "verified_issues"
            )
        elif serial_count == 0:
            status = "no_serials"
        else:
            status = "not_verified"
        summary["created"] += 1
        summary[status] += 1
        payload["packlists"].append(
            {
                "packlist_id": packlist_id,
                "created_display": _erp_local_time_display(row.get("CREATE_DATE")),
                "cust_order_id": row.get("CUST_ORDER_ID"),
                "customer": row.get("CUSTOMER_NAME") or row.get("CUSTOMER_ID"),
                "line_count": int(row.get("LINE_COUNT") or 0),
                "serial_count": serial_count,
                "status": status,
                "session": _verify_session_display(session_row) if session_row else None,
            }
        )
    payload["summary"] = summary

    # Put work that needs an operator first. The ERP query otherwise mixes
    # actionable rows with non-serialized packlists that need no verification.
    verify_status_order = {
        "in_progress": 0,
        "not_verified": 1,
        "verified_issues": 2,
        "verified_clean": 3,
        "no_serials": 4,
        "voided": 5,
    }
    payload["packlists"].sort(
        key=lambda row: (
            verify_status_order.get(str(row.get("status")), 99),
            str(row.get("created_display") or ""),
            str(row.get("packlist_id") or ""),
        )
    )

    # Sessions started this day for packlists created on other days
    # (re-verifying an older box) still deserve a spot on the dashboard.
    day_packlists = {p["packlist_id"] for p in payload["packlists"]}
    for session_row in verify_store.recent_sessions(limit=100):
        if session_row.get("packlist_id") in day_packlists:
            continue
        if _session_started_local_date(session_row) != day_iso:
            continue
        payload["other_sessions"].append(_verify_session_display(session_row))

    return payload


def build_pick_order_queue(pick_type: str | None = None) -> dict[str, Any]:
    """Combine the latest guns/components plans into one order work queue."""
    source_runs: dict[str, int] = {}
    plan_rows: list[dict[str, Any]] = []
    for query_type in QUERY_FILES:
        if pick_type and query_type != pick_type:
            continue
        run, rows = get_latest_successful_run(query_type=query_type)
        if not run:
            continue
        source_runs[query_type] = int(run["id"])
        for row in rows:
            plan_rows.append({**row, "_query_type": query_type})

    claimed = pick_store.claimed_orders(pick_type)
    by_order: dict[str, dict[str, Any]] = {}
    for row in plan_rows:
        order_id = str(row.get("Cust Order ID") or "").strip().upper()
        if not order_id:
            continue
        entry = by_order.setdefault(
            order_id,
            {
                "order_id": order_id,
                "customer_id": str(row.get("Customer ID") or "").strip(),
                "guns": 0,
                "components": 0,
                "units": 0,
                "locations": set(),
                "desired_ship_date": None,
                "claimed": order_id in claimed,
            },
        )
        try:
            quantity = int(float(row.get("SO Qty") or 0))
        except (TypeError, ValueError):
            quantity = 0
        item_type = str(row.get("_query_type") or "").lower()
        entry[item_type] = int(entry.get(item_type) or 0) + quantity
        entry["units"] += quantity
        location = str(row.get("Location") or "").strip()
        if location:
            entry["locations"].add(location)
        desired = str(row.get("Desired Ship Date") or "").strip() or None
        if desired and (
            entry["desired_ship_date"] is None or desired < entry["desired_ship_date"]
        ):
            entry["desired_ship_date"] = desired

    orders = []
    for entry in by_order.values():
        entry["locations"] = sorted(entry["locations"])
        orders.append(entry)
    orders.sort(
        key=lambda row: (
            bool(row["claimed"]),
            str(row.get("desired_ship_date") or "9999-12-31"),
            row["order_id"],
        )
    )
    return {"orders": orders, "plan_rows": plan_rows, "source_runs": source_runs}


def _recent_sessions_for_display(store: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = store.recent_sessions(limit=limit)
    for row in rows:
        row["started_display"] = _audit_dt_display(row.get("started_at"))
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    return rows


def _latest_success_by_type() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for query_type in QUERY_FILES:
        summary = get_latest_successful_run_summary(query_type)
        out[query_type] = (
            {
                "id": summary["id"],
                "row_count": summary["row_count"],
                "run_timestamp_display": format_run_timestamp(summary["run_timestamp"]),
            }
            if summary
            else None
        )
    return out
