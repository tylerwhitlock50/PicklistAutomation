"""Work / Reports pages, shipping APIs, release exceptions, exports and digest."""
import io
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for

from picklist.config import logger, QUERY_FILES
from picklist.domain import readiness
from picklist.features import get_feature_flags
from picklist.security import require_csrf, require_trusted_client
from picklist.services import readiness_service
from picklist.services.digest_service import build_shipped_digest_payload, send_shipped_digest
from picklist.services.run_service import get_run_state_snapshot
from picklist.services.unfinished_work import build_unfinished_work
from picklist.services.shipping_service import (
    _latest_success_by_type,
    _recent_sessions_for_display,
    _release_gate_cache,
    build_excess_packlist_payload,
    build_pick_order_queue,
    build_recon_payload,
    build_release_gate_payload,
    build_shipping_scorecard_payload,
    build_shortage_payload,
    build_stage_aging,
    build_verify_daily_payload,
    parse_scorecard_days,
)
from picklist.stores import pick_store, readiness_store, request_store, shipping_store, verify_store
from picklist.timeutil import _audit_dt_display, _today_local
from picklist.util import _audit_json_safe

bp = Blueprint("shipping", __name__)


# /shipping?view=... serves two kinds of page. WORK_VIEWS are the scan-and-do
# screens that sit under the Work tab; REPORT_VIEWS are read-only and sit
# under Reports. The nav in _topnav.html groups them accordingly.
WORK_VIEWS = {
    "pick": "Pick orders",
    "verify": "Verify boxes",
}


REPORT_VIEWS = {
    "scorecard": "Scorecard",
    "shortages": "Shortages",
    "recon": "Reconciliation",
    "excess": "Excess packlists",
    "stage": "Staged shipments",
}


SHIPPING_VIEWS = {**WORK_VIEWS, **REPORT_VIEWS}


# Sub-views that used to live here and now have their own page. Old links and
# bookmarks land on the replacement.
RETIRED_SHIPPING_VIEWS = {
    "work": "shipping.work_page",
    "holds": "orders.orders_page",
    "requests": "requests.requests_page",
}


@bp.get("/work")
@require_trusted_client
def work_page():
    """Today: the one screen that starts or resumes the day's shipping work."""
    flags = get_feature_flags()
    pick_sessions: list[dict[str, Any]] = []
    verify_sessions: list[dict[str, Any]] = []
    if flags["shipping"]:
        pick_sessions = _recent_sessions_for_display(pick_store)
        verify_sessions = _recent_sessions_for_display(verify_store)
    shipping_requests: list[dict[str, Any]] = []
    if flags["requests"]:
        try:
            shipping_requests = request_store.list_requests(owner_team="shipping", open_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Open request list failed: %s", exc)
    return render_template(
        "work.html",
        pick_sessions=pick_sessions,
        verify_sessions=verify_sessions,
        latest_success_by_type=_latest_success_by_type(),
        run_state_by_type=get_run_state_snapshot(),
        query_options=list(QUERY_FILES.keys()),
        shipping_requests=_audit_json_safe(shipping_requests),
        unfinished_work=build_unfinished_work() if flags["shipping"] else [],
    )


@bp.get("/shipping")
@require_trusted_client
def shipping_page():
    view = request.args.get("view") or "scorecard"
    if view in RETIRED_SHIPPING_VIEWS:
        return redirect(url_for(RETIRED_SHIPPING_VIEWS[view]))
    if view not in SHIPPING_VIEWS:
        view = "scorecard"

    recon_payload = None
    stage = None
    shortages = None
    excess_payload = None
    verify_daily = None
    pick_sessions: list[dict[str, Any]] = []
    latest_success_by_type: dict[str, Any] = {}
    pick_orders: list[dict[str, Any]] = []
    ready_for_pack: list[dict[str, Any]] = []
    scorecard = None
    release_gate_payload = None
    hold_stats = None
    request_stats = None
    scorecard_days = parse_scorecard_days(request.args.get("days"))

    if view == "scorecard":
        scorecard = _audit_json_safe(
            build_shipping_scorecard_payload(scorecard_days)
        )
        release_gate_payload = _audit_json_safe(build_release_gate_payload())
        try:
            hold_stats = readiness_store.hold_durations(scorecard_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hold duration stats failed: %s", exc)
        try:
            request_stats = request_store.queue_summary(scorecard_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Request queue stats failed: %s", exc)
    elif view == "recon":
        recon_payload = _audit_json_safe(build_recon_payload(request.args.get("date")))
    elif view == "shortages":
        shortages = _audit_json_safe(build_shortage_payload())
    elif view == "excess":
        excess_payload = _audit_json_safe(build_excess_packlist_payload())
    elif view == "stage":
        stage = _audit_json_safe(build_stage_aging())
    elif view == "verify":
        verify_daily = _audit_json_safe(
            build_verify_daily_payload(request.args.get("date"))
        )
    elif view == "pick":
        pick_sessions = _recent_sessions_for_display(pick_store)
        latest_success_by_type = _latest_success_by_type()
        pick_type = request.args.get("pick_type", "guns")
        if pick_type not in ("guns", "components"):
            pick_type = "guns"
        pick_sessions = [session for session in pick_sessions if session.get("query_type") in (pick_type, "mixed")]
        pick_orders = build_pick_order_queue(pick_type)["orders"]
        ready_for_pack = pick_store.ready_for_pack_orders(limit=100)
        ready_for_pack = [order for order in ready_for_pack if order.get("query_type") in (pick_type, "mixed")]
        for row in ready_for_pack:
            row["completed_display"] = _audit_dt_display(row.get("completed_at"))

    return render_template(
        "shipping.html",
        unfinished_work=build_unfinished_work() if view in WORK_VIEWS else [],
        view=view,
        view_options=SHIPPING_VIEWS,
        recon=recon_payload,
        stage=stage,
        shortages=shortages,
        excess=excess_payload,
        verify_daily=verify_daily,
        pick_sessions=pick_sessions,
        latest_success_by_type=latest_success_by_type,
        query_options=list(QUERY_FILES.keys()),
        pick_orders=pick_orders,
        pick_type=request.args.get("pick_type", "guns") if request.args.get("pick_type", "guns") in ("guns", "components") else "guns",
        ready_for_pack=ready_for_pack,
        max_pick_orders=pick_store.MAX_ORDERS_PER_SESSION,
        today_iso=_today_local().isoformat(),
        scorecard=scorecard,
        scorecard_days=scorecard_days,
        release_gate=release_gate_payload,
        hold_stats=hold_stats,
        request_stats=request_stats,
        hold_reason_labels={code: meta["label"] for code, meta in readiness.HOLD_REASONS.items()},
        hold_reasons=readiness_service.reason_options(),
        owner_labels=readiness.OWNER_LABELS,
        exception_kinds=request_store.EXCEPTION_KINDS,
    )


@bp.get("/api/shipping/digest/preview")
@require_trusted_client
def api_shipping_digest_preview():
    day_raw = (request.args.get("date") or "").strip()
    try:
        day = date.fromisoformat(day_raw) if day_raw else _today_local()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        payload = build_shipped_digest_payload(day)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Digest preview failed")
        return jsonify({"error": str(exc)}), 502
    payload.pop("packlists", None)
    return jsonify(_audit_json_safe(payload))


@bp.post("/api/shipping/digest/send")
@require_trusted_client
@require_csrf
def api_shipping_digest_send():
    day_raw = (request.args.get("date") or (request.get_json(silent=True) or {}).get("date") or "").strip()
    try:
        day = date.fromisoformat(day_raw) if day_raw else _today_local()
    except ValueError:
        return jsonify({"ok": False, "message": "date must be YYYY-MM-DD"}), 400
    try:
        result = send_shipped_digest(day, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Digest send failed")
        return jsonify({"ok": False, "message": str(exc)}), 502
    return jsonify({"ok": result["sent"], **result}), (200 if result["sent"] else 400)


@bp.get("/api/shipping/metrics")
@require_trusted_client
def api_shipping_metrics():
    days = parse_scorecard_days(request.args.get("days"))
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_shipping_scorecard_payload(days, force=force))), 200


@bp.get("/api/shipping/release-gate")
@require_trusted_client
def api_shipping_release_gate():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_release_gate_payload(force=force))), 200


@bp.post("/shipping/release-exceptions")
@require_trusted_client
@require_csrf
def shipping_release_exception_add():
    order_id = (request.form.get("cust_order_id") or "").strip().upper()
    reason = (request.form.get("reason") or "").strip()
    operator = (request.form.get("operator") or "").strip()
    try:
        hours = int(request.form.get("expires_hours") or "24")
        if hours < 1 or hours > 168:
            raise ValueError
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        shipping_store.add_exception(
            cust_order_id=order_id,
            reason=reason,
            created_by=operator,
            expires_at=expires.isoformat(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )
        flash(f"Release exception added for {order_id}.", "success")
    return redirect(url_for("shipping.shipping_page", view="scorecard"))


@bp.post("/shipping/release-exceptions/<int:exception_id>/revoke")
@require_trusted_client
@require_csrf
def shipping_release_exception_revoke(exception_id: int):
    operator = (request.form.get("operator") or "").strip()
    try:
        revoked = shipping_store.revoke_exception(exception_id, operator)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )
        flash(
            "Release exception revoked." if revoked else "Release exception was already inactive.",
            "success" if revoked else "error",
        )
    return redirect(url_for("shipping.shipping_page", view="scorecard"))


@bp.get("/api/shipping/shortages")
@require_trusted_client
def api_shipping_shortages():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_shortage_payload(force=force))), 200


@bp.get("/api/shipping/excess-packlists")
@require_trusted_client
def api_shipping_excess_packlists():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_excess_packlist_payload(force=force))), 200


@bp.get("/shipping/shortages/export")
@require_trusted_client
def shipping_shortages_export():
    payload = build_shortage_payload()
    if payload.get("error"):
        flash(f"Shortage data is unavailable: {payload['error']}", "error")
        return redirect(url_for("shipping.shipping_page", view="shortages"))

    summary_df = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in (payload["summary"] or {}).items()]
    )
    transfers_df = pd.DataFrame([
        {
            "Part": t["part_id"],
            "Description": t["part_description"],
            "Product code": t["product_code"],
            "Qty needed": t["qty_needed"],
            "Stock locations": t["stock_locations"],
        }
        for t in payload["transfers"]
    ])
    lines_df = pd.DataFrame([
        {
            "Reason": l["reason"],
            "Past due": "yes" if l["past_due"] else "",
            "Desired ship": l["desired_ship_date"],
            "Customer": l.get("customer_name") or l.get("customer_id") or "",
            "Order": l["cust_order_id"],
            "Line": l["line_no"],
            "Part": l["part_id"],
            "Description": l["part_description"],
            "Open qty": l["open_qty"],
            "Will print": l["will_print_qty"],
            "Short": l["short_qty"],
            "Coverable by transfer": l["transfer_qty"],
            "No stock anywhere": l["stockout_qty"],
            "No ship-to on order": "yes" if l["shipto_missing"] else "",
            "Stock locations": l["stock_locations"],
        }
        for l in payload["lines"]
    ])

    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        (transfers_df if not transfers_df.empty else pd.DataFrame(columns=["Part"])).to_excel(
            writer, index=False, sheet_name="Transfer move list"
        )
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["Reason"])).to_excel(
            writer, index=False, sheet_name="Shortage lines"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"component_shortages_{_today_local().isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/api/shipping/recon")
@require_trusted_client
def api_shipping_recon():
    return jsonify(_audit_json_safe(build_recon_payload(request.args.get("date")))), 200


@bp.get("/shipping/recon/export")
@require_trusted_client
def shipping_recon_export():
    payload = build_recon_payload(request.args.get("date"))
    if payload.get("error"):
        flash(payload.get("message") or "Reconciliation data is unavailable.", "error")
        return redirect(url_for("shipping.shipping_page"))

    summary = {k: v for k, v in payload["summary"].items() if k != "by_type"}
    summary_df = pd.DataFrame([{"metric": k, "value": v} for k, v in summary.items()])
    lines_df = pd.DataFrame([
        {
            "Status": l["status"],
            "Customer": l.get("customer_name") or l.get("customer_id") or "",
            "Order": l["cust_order_id"],
            "Part": l["part_id"],
            "Picklist": ", ".join(l["types"]),
            "Locations": ", ".join(l["locations"]),
            "Planned": l["planned_qty"],
            "Shipped same day": l["shipped_same_day"],
            "Shipped late": l["shipped_late"],
            "Voided qty": l["voided_qty"],
            "Packlists": ", ".join(str(p["packlist_id"]) for p in l["packlists"]),
            "Tracking": ", ".join(l["tracking"]),
        }
        for l in payload["lines"]
    ])
    unplanned_df = pd.DataFrame([
        {
            "Customer": u.get("customer_name") or u.get("customer_id") or "",
            "Order": u["cust_order_id"],
            "Part": u.get("part_id") or "",
            "Qty": u["qty"],
            "Packlists": ", ".join(str(p["packlist_id"]) for p in u["packlists"]),
            "Tracking": ", ".join(u["tracking"]),
        }
        for u in payload["unplanned"]
    ])

    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["Status"])).to_excel(
            writer, index=False, sheet_name="Planned lines"
        )
        (unplanned_df if not unplanned_df.empty else pd.DataFrame(columns=["Order"])).to_excel(
            writer, index=False, sheet_name="Unplanned"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"ship_recon_{payload['plan_date']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
