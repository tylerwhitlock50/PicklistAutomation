"""Executes a picklist run end to end (fetch, export, notify)."""
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from picklist.config import ESTIMATED_MINUTES_SAVED_PER_RUN, logger, QUERY_FILES
from picklist.erp import get_erp_engine
from picklist.features import feature_enabled
from picklist.services import readiness_service
from picklist.services.notification_service import (
    send_email_notification,
    send_telegram_notification,
)
from picklist.services.orders_service import _apply_manual_hold_exclusions
from picklist.services.query_options import (
    apply_release_gate_filter,
    get_default_guns_query_options,
    get_query_type,
    load_query,
)
from picklist.services.run_history import (
    generate_export,
    get_run_budget,
    record_reporting_event,
    save_plan_snapshot,
    save_run,
)
from picklist.services.settings_service import get_release_gate_mode
from picklist.services.shipping_service import build_release_gate_payload, build_shortage_payload
from picklist.stores import shipping_store
from picklist.timeutil import _today_local, format_datetime_for_display


RUN_STATE_LOCK = threading.Lock()


RUN_STATE: dict[str, dict[str, Optional[datetime] | bool]] = {
    query_type: {"running": False, "started_at": None}
    for query_type in QUERY_FILES
}


def try_mark_run_started(query_type: str) -> bool:
    started_at = datetime.now(timezone.utc)
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        if bool(state["running"]):
            return False
        state["running"] = True
        state["started_at"] = started_at
    return True


def mark_run_finished(query_type: str) -> None:
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        state["running"] = False
        state["started_at"] = None


def is_run_active(query_type: str) -> bool:
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        return bool(state["running"])


def any_run_active() -> bool:
    with RUN_STATE_LOCK:
        return any(bool(state["running"]) for state in RUN_STATE.values())


def get_run_state_snapshot() -> dict[str, dict[str, Optional[str] | bool]]:
    snapshot: dict[str, dict[str, Optional[str] | bool]] = {}
    with RUN_STATE_LOCK:
        for query_type in QUERY_FILES:
            state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
            started_at_value = state["started_at"]
            started_at_iso = None
            started_at_display = None
            if isinstance(started_at_value, datetime):
                started_at_iso = started_at_value.isoformat()
                started_at_display = format_datetime_for_display(started_at_value)
            snapshot[query_type] = {
                "running": bool(state["running"]),
                "started_at": started_at_iso,
                "started_at_display": started_at_display,
            }
    return snapshot


def fetch_picklist_from_mssql(
    query_type: str,
    query_options: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    query = load_query(query_type, query_options=query_options)
    gate_payload = None
    gate_mode = get_release_gate_mode()
    if gate_mode != "off":
        # A generated picklist is the operational boundary: bypass the dashboard cache
        # and revalidate sticky serial reservations against live ERP immediately before
        # released order IDs are injected into the allocation SQL.
        gate_payload = build_release_gate_payload(
            force=True,
            query_options=query_options,
            require_serial_tracking=gate_mode == "enforced",
            persist=True,
        )
        if gate_payload.get("error") and gate_mode == "enforced":
            raise RuntimeError(
                "Release gate is enforced but could not be evaluated: "
                f"{gate_payload.get('error')}"
            )
    query = apply_release_gate_filter(query, gate_payload)
    if query_type == "guns":
        applied_options = get_default_guns_query_options()
        if query_options:
            applied_options = {**applied_options, **query_options}
        logger.info(
            "Using guns query options: lookahead_days=%s excluded_customers=%s",
            applied_options["lookahead_days"],
            ",".join(applied_options["excluded_customers"]),
        )
    engine = get_erp_engine()
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    try:
        df = _apply_manual_hold_exclusions(df)
    except Exception as exc:  # noqa: BLE001 - a hold ledger problem must not fail the run
        logger.exception("Manual hold exclusion failed: %s", exc)
    if gate_payload is not None:
        df.attrs["release_gate_payload"] = gate_payload
        if gate_payload and not gate_payload.get("error") and not df.empty:
            decision_map = {
                str(row["order_id"]).strip().upper(): row
                for row in gate_payload.get("decisions") or []
            }
            order_column = "Cust Order ID"
            if order_column in df.columns:
                df["Release Gate"] = df[order_column].map(
                    lambda value: (decision_map.get(str(value).strip().upper()) or {}).get(
                        "decision", "UNREVIEWED"
                    )
                )
                df["Release Reason"] = df[order_column].map(
                    lambda value: (decision_map.get(str(value).strip().upper()) or {}).get(
                        "label", "No gate decision"
                    )
                )
    logger.info("Picklist query returned %d rows.", len(df.index))
    return df


def get_dummy_picklist_rows() -> list[dict[str, str]]:
    return [
        {
            "Order": "SO-10425",
            "SKU": "LAMP-BASE-01",
            "Description": "Desk Lamp Base",
            "Location": "A1-03",
            "Qty": "8",
            "Priority": "High",
        },
        {
            "Order": "SO-10426",
            "SKU": "SHADE-IVY-08",
            "Description": "Ivy Fabric Shade",
            "Location": "B2-11",
            "Qty": "4",
            "Priority": "Medium",
        },
        {
            "Order": "SO-10427",
            "SKU": "BULB-WARM-60",
            "Description": "Warm White Bulb 60W",
            "Location": "C1-07",
            "Qty": "12",
            "Priority": "High",
        },
    ]


def _execute_picklist_run_core(
    query_type: str,
    query_options: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    started_at = time.perf_counter()
    run_timestamp = datetime.utcnow()

    try:
        df = fetch_picklist_from_mssql(
            query_type,
            query_options=query_options,
        )
        run_id = save_run(
            df=df,
            status="success",
            query_type=query_type,
            run_timestamp=run_timestamp,
        )
        try:
            gate_payload = df.attrs.get("release_gate_payload")
            if gate_payload and not gate_payload.get("error"):
                released = [
                    row
                    for row in gate_payload.get("decisions") or []
                    if row.get("decision") == "RELEASE"
                ]
                shipping_store.record_released_ship_tos(
                    run_id=run_id,
                    released_decisions=released,
                    released_date=_today_local(),
                )
        except Exception as exc:  # noqa: BLE001 — ledger write must never fail the run
            logger.exception("Failed to record released ship-tos for run %s: %s", run_id, exc)
        try:
            save_plan_snapshot(
                run_id=run_id,
                query_type=query_type,
                run_timestamp=run_timestamp,
                rows=df.to_dict(orient="records"),
            )
        except Exception as exc:  # noqa: BLE001 — recon snapshot must never fail the run
            logger.exception("Failed to save plan snapshot for run %s: %s", run_id, exc)
        try:
            if feature_enabled("orders"):
                threading.Thread(
                    target=readiness_service.refresh,
                    kwargs={"trigger": "picklist_run", "force": True},
                    name="readiness-after-picklist",
                    daemon=True,
                ).start()
        except Exception as exc:  # noqa: BLE001 — readiness refresh must never fail the run
            logger.exception("Failed to start readiness refresh after run %s: %s", run_id, exc)
        export_path = generate_export(
            df=df,
            run_id=run_id,
            query_type=query_type,
            run_timestamp=run_timestamp,
        )
        reporting_event_counted = record_reporting_event(
            source_run_id=run_id,
            query_type=query_type,
            event_timestamp=run_timestamp,
        )
        elapsed_seconds = time.perf_counter() - started_at

        message = (
            f"✅ Picklist run {run_id} ({query_type}) succeeded with {len(df.index)} rows "
            f"in {elapsed_seconds:.2f}s."
        )
        if reporting_event_counted:
            message += (
                f" Counted toward reporting metrics "
                f"({ESTIMATED_MINUTES_SAVED_PER_RUN} minutes saved estimated)."
            )
        if query_type == "components":
            # The picklist only shows what CAN pick — put the shortfall note
            # in the same notification so transfers get requested while the
            # day is young. Never let this extra ERP call fail the run.
            try:
                shortage_payload = build_shortage_payload()
                shortage_summary = shortage_payload.get("summary")
                if not shortage_payload.get("error") and shortage_summary and (
                    shortage_summary["transfer_units"] or shortage_summary["stockout_units"]
                ):
                    message += (
                        f" Shortage check: {shortage_summary['transfer_units']} open units "
                        f"need a transfer from MAIN and {shortage_summary['stockout_units']} "
                        f"have no stock anywhere — move list on the Shipping page."
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Shortage note for run notification failed: %s", exc)
        exclusions = df.attrs.get("manual_hold_exclusions") or []
        if exclusions:
            message += (
                f" {len(exclusions)} order{'s' if len(exclusions) != 1 else ''} excluded by manual holds: "
                + ", ".join(
                    f"{row['order_id']} ({row.get('hold_kind') or 'hold'} until {row.get('expires_at') or '?'})"
                    for row in exclusions[:10]
                )
                + (" ..." if len(exclusions) > 10 else "")
                + "."
            )
        logger.info(message)
        try:
            send_telegram_notification(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Telegram notification error: %s", exc)
        try:
            send_email_notification(
                subject=f"Picklist run {run_id} succeeded",
                body=message,
                attachment=export_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected SMTP notification error: %s", exc)
        return export_path
    except Exception as exc:  # noqa: BLE001
        elapsed_seconds = time.perf_counter() - started_at
        logger.exception("Picklist run failed: %s", exc)
        run_id = save_run(
            pd.DataFrame(),
            status="failed",
            query_type=query_type,
            run_timestamp=run_timestamp,
            error_message=str(exc),
        )
        message = (
            f"❌ Picklist run {run_id} ({query_type}) failed "
            f"after {elapsed_seconds:.2f}s: {exc}"
        )
        try:
            send_telegram_notification(message)
        except Exception as notify_exc:  # noqa: BLE001
            logger.exception("Unexpected Telegram notification error: %s", notify_exc)
        try:
            send_email_notification(
                subject=f"Picklist run {run_id} failed",
                body=(
                    f"{message}\n\n"
                    "Please review logs/app.log for the full traceback and failure context."
                ),
            )
        except Exception as notify_exc:  # noqa: BLE001
            logger.exception("Unexpected SMTP notification error: %s", notify_exc)
        return None


def execute_picklist_run(
    query_type: Optional[str] = None,
    query_options: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    normalized_query_type = get_query_type(query_type)
    budget = get_run_budget(normalized_query_type)
    if budget["exhausted"]:
        logger.info(
            "Skipped picklist run for %s: daily run limit reached (%d used, resets %s).",
            normalized_query_type,
            budget["used"],
            budget["resets_at_display"],
        )
        return None
    if not try_mark_run_started(normalized_query_type):
        logger.info(
            "Skipped picklist run for %s because another run is already active.",
            normalized_query_type,
        )
        return None

    try:
        return _execute_picklist_run_core(
            normalized_query_type,
            query_options=query_options,
        )
    finally:
        mark_run_finished(normalized_query_type)


def start_picklist_run_async(
    query_type: Optional[str] = None,
    query_options: Optional[dict[str, Any]] = None,
) -> bool:
    normalized_query_type = get_query_type(query_type)
    budget = get_run_budget(normalized_query_type)
    if budget["exhausted"]:
        logger.info(
            "Skipped background picklist run for %s: daily run limit reached (%d used, resets %s).",
            normalized_query_type,
            budget["used"],
            budget["resets_at_display"],
        )
        return False
    if not try_mark_run_started(normalized_query_type):
        logger.info(
            "Skipped background picklist run for %s because another run is already active.",
            normalized_query_type,
        )
        return False

    background_query_options = dict(query_options or {})

    def run_in_background() -> None:
        try:
            _execute_picklist_run_core(
                normalized_query_type,
                query_options=background_query_options,
            )
        finally:
            mark_run_finished(normalized_query_type)

    thread = threading.Thread(
        target=run_in_background,
        name=f"picklist-run-{normalized_query_type}",
        daemon=True,
    )
    thread.start()
    return True
