"""Pick sessions."""
import io
import json
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for

from picklist.config import logger, PICK_SERIAL_LOOKUP_FILE, PICK_UPC_LOOKUP_FILE, SERIAL_MAX_LENGTH
from picklist.domain import identity
from picklist.domain.pick_path import pick_line_sort_key
from picklist.erp import run_erp_query_file
from picklist.security import require_csrf, require_trusted_client
from picklist.services.query_options import get_query_type
from picklist.services.run_history import get_latest_successful_run, get_run_by_id
from picklist.services.shipping_service import build_pick_order_queue
from picklist.stores import pick_store, readiness_store
from picklist.timeutil import _audit_dt_display

bp = Blueprint("pick", __name__)


def _session_warnings(session: dict, orders: list[dict]) -> list[str]:
    """Advisory saved data only; failures must not prevent picking."""
    warnings = []
    try:
        sources = json.loads(session.get("source_runs_json") or "{}")
        if not sources and session.get("run_id"):
            sources = {session.get("query_type"): session["run_id"]}
        for kind, run_id in sources.items():
            run = get_run_by_id(run_id, kind)
            if not run:
                warnings.append(f"Source {kind} run #{run_id} is no longer available.")
                continue
            timestamp = datetime.fromisoformat(str(run["run_timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = max(0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600)
            warnings.append(f"{'Stale saved plan' if age >= 24 else 'Saved plan'}: {kind} run #{run_id}, {_audit_dt_display(run['run_timestamp'])}, {age:.1f} hours old. Current stock may differ.")
    except Exception:
        logger.warning("Session source warnings unavailable", exc_info=True)
        warnings.append("Saved-plan age is unavailable. Picking remains available.")
    try:
        snapshot = readiness_store.latest_snapshot()
        if not snapshot or snapshot.get("error"):
            warnings.append("Readiness information is unavailable; it does not prevent picking.")
        else:
            order_ids = {str(row.get("cust_order_id") or "").upper() for row in orders}
            concerns = [row for row in snapshot.get("orders", []) if str(row.get("order_id") or "").upper() in order_ids and row.get("state") in ("BLOCKED", "ATTENTION")]
            if concerns:
                warnings.append("Readiness advice from " + _audit_dt_display(snapshot.get("evaluated_at")) + ": " + ", ".join(f"{row['order_id']} ({row['state'].lower()})" for row in concerns) + ". These warnings do not prevent picking.")
    except Exception:
        logger.warning("Session readiness warnings unavailable", exc_info=True)
        warnings.append("Readiness information is unavailable; it does not prevent picking.")
    return warnings


@bp.post("/pick/session/start")
@require_trusted_client
@require_csrf
def pick_session_start():
    operator = (request.form.get("operator") or "").strip() or None
    selected_orders = request.form.getlist("orders")
    pick_type = request.form.get("pick_type", "guns")
    if pick_type not in ("guns", "components"):
        return jsonify({"message": "Choose guns or components."}), 400
    queue = build_pick_order_queue(pick_type)
    try:
        session_id = pick_store.start_order_session(
            plan_rows=queue["plan_rows"],
            selected_orders=selected_orders,
            source_runs=queue["source_runs"],
            operator=operator,
            pick_type=pick_type,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("shipping.shipping_page", view="pick", pick_type=pick_type))
    logger.info(
        "Started order pick session #%s for %s.",
        session_id,
        ", ".join(selected_orders),
    )
    return redirect(url_for("pick.pick_session_page", session_id=session_id))


def _legacy_pick_session_start():
    query_type = get_query_type(request.form.get("query_type"))
    operator = (request.form.get("operator") or "").strip() or None

    run, rows = get_latest_successful_run(query_type=query_type)
    if not run:
        flash(
            f"No successful {query_type} picklist run to pick against. Run the picklist first.",
            "error",
        )
        return redirect(url_for("shipping.shipping_page", view="pick"))
    if not rows:
        flash(
            f"The latest {query_type} run has no rows — nothing to pick.",
            "error",
        )
        return redirect(url_for("shipping.shipping_page", view="pick"))

    active_session = next(
        (
            session
            for session in pick_store.recent_sessions(limit=100)
            if session.get("status") == "active"
            and session.get("query_type") == query_type
            and int(session.get("run_id") or 0) == int(run["id"])
        ),
        None,
    )
    if active_session:
        flash(
            f"Resuming active {query_type} pick session #{active_session['id']}.",
            "success",
        )
        return redirect(url_for("pick.pick_session_page", session_id=active_session["id"]))

    session_id = pick_store.start_session(
        run_id=run["id"],
        query_type=query_type,
        plan_rows=rows,
        operator=operator,
    )
    logger.info(
        "Started pick session #%s from %s run %s (%d rows).",
        session_id,
        query_type,
        run["id"],
        len(rows),
    )
    return redirect(url_for("pick.pick_session_page", session_id=session_id))


@bp.get("/pick/session/<int:session_id>")
@require_trusted_client
def pick_session_page(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        flash(f"Pick session #{session_id} was not found.", "error")
        return redirect(url_for("shipping.shipping_page", view="pick"))

    lines = sorted(pick_store.get_lines(session_id), key=pick_line_sort_key)
    orders = pick_store.get_orders(session_id)
    order_context: dict[str, dict[str, Any]] = {}
    for order in orders:
        order_id = str(order.get("cust_order_id") or "").strip().upper()
        order_context[order_id] = {
            "tote": order.get("tote_barcode") or order.get("tote_code"),
            "locations": sorted(
                {
                    str(line.get("location") or "").strip().upper()
                    for line in lines
                    if str(line.get("cust_order_id") or "").strip().upper() == order_id
                    and str(line.get("location") or "").strip()
                }
            ),
            "status": order.get("status"),
            "assigned_operator": order.get("assigned_operator"),
        }
    scans = pick_store.get_scans(session_id, limit=100)
    order_events = pick_store.get_order_events(session_id, limit=200)
    for scan in scans:
        scan["scanned_display"] = _audit_dt_display(scan.get("scanned_at"))
    for event in order_events:
        event["created_display"] = _audit_dt_display(event.get("created_at"))

    return render_template(
        "pick_session.html",
        session=session_row,
        pick_warnings=_session_warnings(session_row, orders),
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        lines=lines,
        orders=orders,
        order_context=order_context,
        scans=scans,
        order_events=order_events,
        counts=pick_store.compute_counts(session_id),
    )


def _resolve_pick_candidates(scan: str, lines: list[dict]) -> tuple[Optional[str], list[dict], bool]:
    """(serial, part_candidates, erp_failed) for one scanned value.

    A scan matching a picklist part ID directly is a part-barcode pick
    (components); anything else is treated as a serial and resolved in the
    ERP to the part(s) it represents plus where it currently sits.
    """
    line_parts = {str(l["part_id"] or "").strip().upper() for l in lines}
    if scan in line_parts:
        return None, [{"part_id": scan, "locations": []}], False
    upc_parts = {
        str(line.get("upc") or "").strip().upper(): str(line["part_id"] or "").strip()
        for line in lines
        if str(line.get("upc") or "").strip()
    }
    if scan in upc_parts:
        return None, [{"part_id": upc_parts[scan], "locations": []}], False

    try:
        df = run_erp_query_file(
            PICK_SERIAL_LOOKUP_FILE, {"serial": scan}, "pick serial lookup"
        )
    except Exception:  # noqa: BLE001
        logger.exception("Pick serial lookup failed for %s", scan)
        return scan, [], True
    if df.empty and any(line.get("item_type") == "components" for line in lines):
        try:
            upc_df = run_erp_query_file(
                PICK_UPC_LOOKUP_FILE, {"upc": scan}, "pick UPC lookup"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Pick UPC lookup failed for %s", scan)
            return None, [], True
        if not upc_df.empty:
            upc_candidates = [
                {"part_id": str(row.get("PART_ID") or "").strip(), "locations": []}
                for row in upc_df.to_dict(orient="records")
                if str(row.get("PART_ID") or "").strip()
            ]
            return None, upc_candidates, False
    if df.empty:
        return scan, [], False

    rows = df.to_dict(orient="records")
    on_hand = [r for r in rows if r.get("NET_QTY") is not None and pd.notna(r.get("NET_QTY")) and r["NET_QTY"] > 0]
    pool = on_hand or rows
    by_part: dict[str, list[str]] = {}
    for row in pool:
        part = str(row.get("PART_ID") or "").strip()
        if not part:
            continue
        locations = by_part.setdefault(part, [])
        loc = str(row.get("LOCATION_ID") or "").strip()
        if loc and loc not in locations:
            locations.append(loc)
    candidates = [{"part_id": part, "locations": locs} for part, locs in by_part.items()]
    return scan, candidates, False


@bp.post("/api/pick/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_pick_scan(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify({"error": "completed", "message": "This pick session is already completed."}), 409

    payload = request.get_json(silent=True) or {}
    scan = (payload.get("scan") or "").strip().upper()
    target_order = (payload.get("order") or "").strip().upper() or None
    operator = (payload.get("operator") or session_row.get("operator") or "").strip() or None
    request_id = (payload.get("request_id") or "").strip() or None
    scanned_tote = (payload.get("tote") or "").strip().upper() or None
    scanned_location = (payload.get("location") or "").strip().upper() or None
    if not scan:
        return jsonify({"error": "invalid", "message": "A scanned value is required."}), 400
    if len(scan) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Scanned value is too long."}), 400
    if not request_id or len(request_id) > 100:
        return jsonify({"error": "invalid", "message": "A valid scan request ID is required."}), 400

    lines = pick_store.get_lines(session_id)
    serial, candidates, erp_failed = _resolve_pick_candidates(scan, lines)
    if erp_failed:
        return (
            jsonify(
                {
                    "error": "erp_failed",
                    "message": "The ERP serial lookup failed — scan not recorded. Try again.",
                }
            ),
            502,
        )

    checked_upc = str(payload.get("item") or payload.get("upc") or "").strip().upper() or None
    upc_parts = []
    if session_row.get("workflow_mode") == "single_guns":
        if not checked_upc or len(checked_upc) > SERIAL_MAX_LENGTH:
            return jsonify({"message": "Scan the part number or UPC before its serial."}), 400
        upc_parts = [line["part_id"] for line in lines
                     if checked_upc in (str(line["part_id"]).upper(), str(line.get("upc") or "").upper())]
        if not upc_parts:
            try:
                upc_df = run_erp_query_file(PICK_UPC_LOOKUP_FILE, {"upc": checked_upc}, "gun UPC lookup")
                upc_parts = [str(row.get("PART_ID") or "") for row in upc_df.to_dict(orient="records")]
            except Exception:
                logger.exception("Gun UPC lookup failed")
                return jsonify({"message": "Item barcode lookup failed. Retry this item or scan its part number."}), 502
    line_id = payload.get("line_id")
    if line_id is not None:
        if not isinstance(line_id, int) or isinstance(line_id, bool):
            return jsonify({"message": "Invalid pick line."}), 400
    try:
        result = pick_store.record_scan(
            session_id,
            scan,
            target_order=target_order,
            serial=serial,
            part_candidates=candidates,
            operator=operator,
            unknown=(serial is not None and not candidates),
            request_id=request_id,
            scanned_tote=scanned_tote,
            scanned_location=scanned_location,
            checked_upc=checked_upc,
            upc_parts=upc_parts,
            target_line_id=line_id,
        )
    except ValueError as exc:
        return jsonify({"error": "closed", "message": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record pick scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    result["scan"] = scan
    return jsonify(result), 200


@bp.post("/api/pick/session/<int:session_id>/order/<path:order_id>/action")
@require_trusted_client
@require_csrf
def api_pick_order_action(session_id: int, order_id: str):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404
    payload = request.get_json(silent=True) or {}
    try:
        result = pick_store.order_action(
            session_id,
            order_id,
            str(payload.get("action") or ""),
            operator=str(payload.get("operator") or session_row.get("operator") or ""),
            reason=payload.get("reason"),
            to_operator=payload.get("to_operator"),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_action", "message": str(exc)}), 409
    result["redirect"] = url_for("pick.pick_session_page", session_id=session_id)
    return jsonify(result), 200


@bp.post("/api/pick/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_pick_complete(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404

    payload = request.get_json(silent=True) or {}
    order_id = (payload.get("order") or "").strip().upper()
    try:
        if order_id:
            completed = pick_store.complete_order(session_id, order_id)
            counts = pick_store.compute_counts(session_id)
            logger.info("Order %s is ready for packing from pick session #%s.", order_id, session_id)
        else:
            completed = pick_store.complete_session(session_id)
            counts = completed.get("counts")
    except ValueError as exc:
        return jsonify({"error": "incomplete", "message": str(exc)}), 409
    return jsonify(
        {
            "status": completed.get("status", "completed"),
            "counts": counts,
            "redirect": url_for("pick.pick_session_page", session_id=session_id),
        }
    ), 200


def _abandon_pick_session(session_id: int, operator: str, reason: str) -> dict:
    session_row = pick_store.get_session(session_id)
    if not session_row:
        raise LookupError("Pick session not found.")
    current = identity.current_operator()
    actor = (operator or "").strip() or (current.name if current else "") or (session_row.get("operator") or "")
    result = pick_store.abandon_session(session_id, operator=actor, reason=reason)
    logger.info(
        "Pick session #%s closed short by %s (%s): %d order(s) released, %d picked unit(s) to put back.",
        session_id, actor, reason, len(result["orders"]), result["picked_units"],
    )
    return result


@bp.post("/api/pick/session/<int:session_id>/abandon")
@require_trusted_client
@require_csrf
def api_pick_abandon(session_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        result = _abandon_pick_session(
            session_id, str(payload.get("operator") or ""), str(payload.get("reason") or "")
        )
    except LookupError as exc:
        return jsonify({"error": "not_found", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": "invalid_action", "message": str(exc)}), 409
    result["redirect"] = url_for("pick.pick_session_page", session_id=session_id)
    return jsonify(result), 200


@bp.post("/pick/session/<int:session_id>/abandon")
@require_trusted_client
@require_csrf
def pick_session_abandon(session_id: int):
    try:
        result = _abandon_pick_session(
            session_id, request.form.get("operator") or "", request.form.get("reason") or ""
        )
    except LookupError as exc:
        flash(str(exc), "error")
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        note = f" {result['picked_units']} picked unit(s) need to go back on the shelf." if result["picked_units"] else ""
        flash(f"Pick session #{session_id} closed; {len(result['orders'])} order(s) released.{note}", "success")
    if request.form.get("return_to") == "work":
        return redirect(url_for("shipping.work_page"))
    if request.form.get("return_to") == "verify":
        return redirect(url_for("shipping.shipping_page", view="verify"))
    return redirect(url_for("shipping.shipping_page", view="pick"))


@bp.get("/pick/session/<int:session_id>/export")
@require_trusted_client
def pick_session_export(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        flash(f"Pick session #{session_id} was not found.", "error")
        return redirect(url_for("shipping.shipping_page", view="pick"))

    lines_df = pd.DataFrame(pick_store.get_lines(session_id))
    scans_df = pd.DataFrame(pick_store.get_scans(session_id, limit=10000))
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["part_id"])).to_excel(
            writer, index=False, sheet_name="Lines"
        )
        (scans_df if not scans_df.empty else pd.DataFrame(columns=["scan_value"])).to_excel(
            writer, index=False, sheet_name="Scans"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pick_session{session_id}_{session_row['query_type']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
