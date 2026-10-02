"""Box verification sessions."""
import io
from typing import Optional

import pandas as pd
from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for

from picklist.config import (
    logger,
    PACKLIST_SERIALS_FILE,
    SERIAL_HISTORY_SHIPMENTS_FILE,
    SERIAL_MAX_LENGTH,
)
from picklist.erp import run_erp_query_file
from picklist.security import require_csrf, require_trusted_client
from picklist.services.shipping_service import build_verify_daily_payload
from picklist.stores import pick_store, verify_store
from picklist.timeutil import _audit_dt_display
from picklist.util import _audit_json_safe

bp = Blueprint("verify", __name__)


def _normalize_packlist_input(raw: str) -> str:
    """Uppercase/trim; a bare number is assumed to be a PL- packlist."""
    value = (raw or "").strip().upper()
    if value.isdigit():
        return f"PL-{value}"
    return value


def _lookup_serial_shipment(scan: str, current_packlist: str) -> tuple[Optional[dict], bool]:
    """(live shipment row on a different packlist, erp_checked) for a scan
    that matched nothing in the session snapshot."""
    try:
        df = run_erp_query_file(
            SERIAL_HISTORY_SHIPMENTS_FILE, {"serial": scan}, "verify serial lookup"
        )
    except Exception:  # noqa: BLE001 — enrichment only; the scan still records
        logger.exception("Verify serial reverse lookup failed for %s", scan)
        return None, False
    other = None
    for row in df.to_dict(orient="records"):
        status = str(row.get("SHIPPER_STATUS") or "").strip().upper()
        packlist = str(row.get("PACKLIST_ID") or "").strip().upper()
        if status in ("X", "V") or not packlist or packlist == current_packlist:
            continue
        other = row  # keep the last (most recent SHIPPED_DATE) live shipment
    return other, True


@bp.post("/verify/session/start")
@require_trusted_client
@require_csrf
def verify_session_start():
    packlist_id = _normalize_packlist_input(request.form.get("packlist_id") or "")
    operator = (request.form.get("operator") or "").strip() or None
    if not packlist_id:
        flash("Scan or type a packlist number to verify.", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))

    try:
        df = run_erp_query_file(
            PACKLIST_SERIALS_FILE, {"packlist": packlist_id}, "packlist serials"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Packlist serials query failed for %s", packlist_id)
        flash(f"Could not load {packlist_id} from the ERP: {exc}", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))

    rows = df.to_dict(orient="records")
    if not rows:
        flash(f"{packlist_id} was not found in the ERP.", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))

    header = rows[0]
    shipper_status = str(header.get("SHIPPER_STATUS") or "").strip().upper()
    if shipper_status in ("X", "V"):
        flash(f"{packlist_id} is voided in the ERP — nothing to verify.", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))
    pick_attached = pick_store.attach_packlist(
        header.get("CUST_ORDER_ID"), packlist_id
    )
    if not any(str(r.get("TRACE_ID") or "").strip() for r in rows):
        if pick_attached:
            flash(
                f"{packlist_id} was attached to the picked order; it has no serialized items to scan-verify.",
                "success",
            )
            return redirect(url_for("shipping.shipping_page", view="verify"))
        flash(
            f"{packlist_id} has no serialized items — nothing to scan-verify.",
            "error",
        )
        return redirect(url_for("shipping.shipping_page", view="verify"))

    session_id = verify_store.start_session(packlist_id, header, rows, operator=operator)
    if pick_attached:
        logger.info(
            "Attached %s to ready picked order %s.",
            packlist_id,
            header.get("CUST_ORDER_ID"),
        )
    logger.info(
        "Started verify session #%s for %s (%d rows).",
        session_id,
        packlist_id,
        len(rows),
    )
    return redirect(url_for("verify.verify_session_page", session_id=session_id))


@bp.get("/verify/session/<int:session_id>")
@require_trusted_client
def verify_session_page(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        flash(f"Verification session #{session_id} was not found.", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))

    expected = verify_store.get_expected(session_id)
    scans = verify_store.get_scans(session_id, limit=100)
    for scan in scans:
        scan["scanned_display"] = _audit_dt_display(scan.get("scanned_at"))

    return render_template(
        "verify_session.html",
        session=session_row,
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        expected=[row for row in expected if row["status"] != "info"],
        info_rows=[row for row in expected if row["status"] == "info"],
        scans=scans,
        counts=verify_store.compute_counts(session_id),
    )


@bp.post("/api/verify/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_verify_scan(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Verification session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify(
            {"error": "completed", "message": "This verification session is already completed."}
        ), 409

    payload = request.get_json(silent=True) or {}
    scan = (payload.get("scan") or "").strip().upper()
    operator = (payload.get("operator") or "").strip() or None
    if not scan:
        return jsonify({"error": "invalid", "message": "A scanned value is required."}), 400
    if len(scan) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Scanned value is too long."}), 400

    # Only a scan that matches nothing in the snapshot needs the ERP, and its
    # failure never blocks the scan from recording (unlike pick confirm).
    other_row = None
    erp_checked = True
    if not any(
        row["status"] != "info" and scan in (row.get("serial"), row.get("serial_alt"))
        for row in verify_store.get_expected(session_id)
    ):
        other_row, erp_checked = _lookup_serial_shipment(scan, session_row["packlist_id"])

    try:
        result = verify_store.record_scan(
            session_id,
            scan,
            other_packlist_id=(other_row or {}).get("PACKLIST_ID"),
            other_customer=(other_row or {}).get("CUSTOMER_NAME")
            or (other_row or {}).get("CUSTOMER_ID"),
            erp_checked=erp_checked,
            operator=operator,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record verify scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    result["scan"] = scan
    return jsonify(result), 200


@bp.post("/api/verify/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_verify_complete(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Verification session not found."}), 404

    completed = verify_store.complete_session(session_id)
    logger.info(
        "Completed verify session #%s for %s (%s).",
        session_id,
        session_row.get("packlist_id"),
        completed.get("outcome"),
    )
    return jsonify(
        {
            "status": "completed",
            "outcome": completed.get("outcome"),
            "counts": completed.get("counts"),
            "redirect": url_for("verify.verify_session_page", session_id=session_id),
        }
    ), 200


@bp.get("/api/verify/daily")
@require_trusted_client
def api_verify_daily():
    force = request.args.get("refresh") == "1"
    payload = build_verify_daily_payload(request.args.get("date"), force=force)
    return jsonify(_audit_json_safe(payload)), 200


@bp.get("/verify/session/<int:session_id>/export")
@require_trusted_client
def verify_session_export(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        flash(f"Verification session #{session_id} was not found.", "error")
        return redirect(url_for("shipping.shipping_page", view="verify"))

    expected_df = pd.DataFrame(verify_store.get_expected(session_id))
    scans_df = pd.DataFrame(verify_store.get_scans(session_id, limit=10000))
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (expected_df if not expected_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Expected"
        )
        (scans_df if not scans_df.empty else pd.DataFrame(columns=["scan_value"])).to_excel(
            writer, index=False, sheet_name="Scans"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"verify_session{session_id}_{session_row['packlist_id']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
