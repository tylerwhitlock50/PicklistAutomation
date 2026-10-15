"""Serialized inventory audit pages and APIs."""
import csv
import io
from datetime import date, datetime

import pandas as pd
from flask import Blueprint, g, jsonify, redirect, render_template, request, send_file, url_for

from fulfillment.audit.runtime import logger, UI_REFRESH_INTERVAL_SECONDS, resolve_timezone, _audit_dt_display, _audit_json_safe, flash
from fulfillment.audit.runtime import require_csrf, require_trusted_client
from fulfillment.audit.service import (
    _audit_analytics_window,
    _audit_unavailable_response,
    AUDIT_UNAVAILABLE_MESSAGE,
    build_audit_analytics,
    fetch_audit_expected,
    recheck_unexpected_scans,
    sync_audit_locations_from_erp,
)
from fulfillment.audit import store as audit_store

bp = Blueprint("audit", __name__, template_folder="templates", static_folder="static", static_url_path="/audit/assets")


@bp.get("/api/audit/dashboard")
@require_trusted_client
def api_dashboard():
    error = sync_audit_locations_from_erp()
    return jsonify(_audit_json_safe({"locations": audit_store.list_location_status(),
        "active_sessions": audit_store.unfinished_sessions(), "recent_sessions": audit_store.recent_sessions(), "source_error": error}))


@bp.get("/api/audit/session/<int:session_id>")
@require_trusted_client
def api_session(session_id):
    from fulfillment.domain import require
    session = audit_store.get_session(session_id)
    require(session is not None, "Audit session not found.", "not_found", 404)
    return jsonify(_audit_json_safe({"session": session, "items": audit_store.get_expected_items(session_id),
        "unexpected": audit_store.get_unexpected_scans(session_id), "counts": audit_store.compute_counts(session_id),
        "resolutions": audit_store.get_resolutions(session_id)}))


@bp.route("/audit/settings", methods=["GET", "POST"])
@require_trusted_client
def audit_settings():
    from sqlalchemy import text
    from fulfillment import model as m
    from fulfillment.domain import require
    from fulfillment.audit.runtime import settings
    require(g.actor["role"] == "admin", "Administrator access required.", "role", 403)
    config = settings()
    if request.method == "POST":
        def number(name, low, high):
            try:
                value = int(request.form.get(name, ""))
            except ValueError:
                require(False, f"{name} must be an integer.", "invalid", 400)
            require(low <= value <= high, f"{name} must be between {low} and {high}.", "invalid", 400)
            return value
        require(number("version", 1, 100000000) == config["version"], "Audit settings changed. Reload before saving.", "revision", 409)
        reason = request.form.get("reason", "").strip()
        require(3 <= len(reason) <= 1000, "Enter a reason (3–1000 characters).", "invalid", 400)
        locations = sorted(set(x.strip().upper() for x in request.form.get("dwell_locations", "").split(",") if x.strip()))
        require(len(locations) <= 100 and all(x.count("/") == 1 and all(x.split("/")) for x in locations), "Enter WAREHOUSE/LOCATION pairs.", "invalid", 400)
        updated = {"version": config["version"] + 1, "accuracy_target_pct": number("accuracy_target_pct", 0, 100),
            "dwell_target_hours": number("dwell_target_hours", 1, 8760), "sync_minutes": number("sync_minutes", 1, 1440), "dwell_locations": locations}
        cadence = number("cadence_days", 1, 365)
        scope = request.form.get("scope", "")
        if scope:
            result = g.audit_conn.execute(text("UPDATE dispatch_audit_locations SET cadence_days=:days WHERE scope=:scope"), {"days": cadence, "scope": scope})
            require(result.rowcount == 1, "Audit location no longer exists.", "not_found", 404)
        m.put_setting(g.audit_conn, "audit_configuration", updated)
        from fulfillment.service import event
        event(g.audit_conn, g.actor["id"], "admin:audit-settings", {"reason": reason, "before": config, "after": updated, "scope": scope, "cadence_days": cadence})
        return redirect(url_for("audit.audit_settings"))
    return render_template("audit_settings.html", config=config, locations=audit_store.list_location_status())


@bp.get("/audit")
@require_trusted_client
def audit_dashboard():
    if not audit_store.is_available():
        return _audit_unavailable_response()

    sync_error = sync_audit_locations_from_erp(force=request.args.get("sync") == "1")

    locations = audit_store.list_location_status()
    tied_row = None
    by_warehouse: dict[str, list[dict]] = {}
    for loc in locations:
        loc["last_inventoried_display"] = _audit_dt_display(loc.get("last_inventoried"))
        if loc["scope"] == audit_store.TIED_WO_SCOPE:
            tied_row = loc
        else:
            by_warehouse.setdefault(loc["warehouse_id"], []).append(loc)
    warehouses = [
        {"warehouse_id": wh, "locations": locs, "serial_total": sum(l["serial_count"] for l in locs)}
        for wh, locs in sorted(by_warehouse.items())
    ]

    recent = audit_store.recent_sessions(limit=10)
    for row in recent:
        row["started_display"] = _audit_dt_display(row.get("started_at"))
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))

    due_locations = [loc for loc in locations if loc.get("due")]
    return render_template(
        "audit.html",
        audit_available=True,
        warehouses=warehouses,
        tied_row=tied_row,
        recent_sessions=recent,
        active_sessions=audit_store.unfinished_sessions(),
        due_locations=due_locations,
        sync_error=sync_error,
        last_synced_display=_audit_dt_display(audit_store.last_synced_at()),
        today_iso=datetime.now(resolve_timezone()).date().isoformat(),
    )


@bp.post("/api/audit/sessions")
@bp.post("/audit/session/start")
@require_trusted_client
@require_csrf
def audit_session_start():
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit.audit_dashboard"))

    source = request.get_json(silent=True) if request.is_json else request.form
    source = source or {}
    try:
        target = audit_store.build_target(
            kind=source.get("target_kind"),
            warehouse=source.get("warehouse"),
            location=source.get("location"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("audit.audit_dashboard"))

    if not audit_store.target_exists(target):
        flash(
            f"Unknown audit location: {target['label']}. "
            "Refresh the dashboard and try again.",
            "error",
        )
        return redirect(url_for("audit.audit_dashboard"))

    operator = g.actor["id"]

    try:
        df = fetch_audit_expected()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load audit expected list: %s", exc)
        flash("Could not load the expected serial list. No audit was started.", "error")
        return redirect(url_for("audit.audit_dashboard"))

    expected_rows = df.astype(object).where(pd.notna(df), None).to_dict(orient="records") if not df.empty else []
    try:
        from fulfillment.audit.runtime import validate_snapshot
        validate_snapshot(expected_rows)
        session_id = audit_store.start_session(target, expected_rows, operator)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start audit session: %s", exc)
        flash("Could not start the audit session. Check the source data and retry.", "error")
        return redirect(url_for("audit.audit_dashboard"))

    logger.info(
        "Started audit session #%s (%s, %d serials snapshotted).",
        session_id,
        target["label"],
        len(expected_rows),
    )
    if request.is_json:
        return jsonify(id=session_id, url=url_for("audit.audit_session_page", session_id=session_id))
    return redirect(url_for("audit.audit_session_page", session_id=session_id))


@bp.get("/audit/session/<int:session_id>")
@require_trusted_client
def audit_session_page(session_id: int):
    if not audit_store.is_available():
        return _audit_unavailable_response()

    session_row = audit_store.get_session(session_id)
    if not session_row:
        flash(f"Audit session #{session_id} was not found.", "error")
        return redirect(url_for("audit.audit_dashboard"))

    items = audit_store.get_expected_items(session_id)
    for item in items:
        item["scanned_at_display"] = _audit_dt_display(item.get("scanned_at"))
    unexpected = audit_store.get_unexpected_scans(session_id)
    for row in unexpected:
        row["first_scanned_display"] = _audit_dt_display(row.get("first_scanned_at"))
        try:
            row["near_matches"] = audit_store.find_near_matches(
                session_id, row.get("scanned_serial") or ""
            )
        except Exception:  # noqa: BLE001 — hints are best-effort
            row["near_matches"] = []
    counts = audit_store.compute_counts(session_id)

    return render_template(
        "audit_session.html",
        session=session_row,
        session_scope_label=session_row.get("label")
        or session_row.get("scope")
        or "Full audit (all locations)",
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        items=items,
        unexpected=unexpected,
        counts=counts,
        resolutions=audit_store.get_resolutions(session_id),
        resolution_codes=audit_store.RESOLUTION_CODES,
        manual_resolution_codes=audit_store.MANUAL_RESOLUTION_CODES,
        open_resolution_codes=audit_store.OPEN_RESOLUTION_CODES,
        known_location_ids=audit_store.list_active_location_ids(),
        ui_refresh_interval_seconds=UI_REFRESH_INTERVAL_SECONDS,
    )


@bp.post("/api/audit/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_audit_scan(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify({"error": "completed", "message": "This audit session is already completed."}), 409

    payload = request.get_json(silent=True) or {}
    serial = (payload.get("serial") or "").strip()
    location = (payload.get("location") or "").strip()
    from fulfillment.audit.runtime import validate_barcode
    validate_barcode(location, "Location")
    operator = g.actor["id"]
    from fulfillment.audit.runtime import validate_barcode
    validate_barcode(serial, "Serial")
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400

    try:
        result = audit_store.record_scan(session_id, serial, location, operator)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record audit scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": "Audit operation failed. Retry the same request."}), 500

    return jsonify(
        {
            "result": result["result"],
            "is_duplicate": result["is_duplicate"],
            "item": result["item"],
            "counts": result["counts"],
            "near_matches": result.get("near_matches") or [],
            "serial": serial,
            "location": location,
        }
    ), 200


@bp.post("/api/audit/session/<int:session_id>/resolve")
@require_trusted_client
@require_csrf
def api_audit_resolve(session_id: int):
    # Allowed on both in-progress and completed sessions: exceptions found by a
    # completed audit are a punch list worked after the fact.
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    payload = request.get_json(silent=True) or {}
    serial = (payload.get("serial") or "").strip()
    resolution = (payload.get("resolution") or "").strip()
    note = (payload.get("note") or "").strip() or None
    operator = g.actor["id"]
    from fulfillment.audit.runtime import validate_barcode
    validate_barcode(serial, "Serial")
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400
    if resolution not in audit_store.MANUAL_RESOLUTION_CODES:
        return jsonify({"error": "invalid", "message": "A valid resolution status is required."}), 400

    try:
        result = audit_store.record_resolution(session_id, serial, resolution, note, operator)
    except ValueError as exc:
        return jsonify({"error": "invalid", "message": "Audit operation failed. Retry the same request."}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record audit resolution: %s", exc)
        return jsonify({"error": "resolve_failed", "message": "Audit operation failed. Retry the same request."}), 500

    return jsonify(
        {
            "resolution": result["resolution"],
            "resolution_label": result["resolution_label"],
            "exception_type": result["exception_type"],
            "item": result["item"],
            "counts": result["counts"],
            "serial": serial,
        }
    ), 200


@bp.post("/api/audit/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_audit_complete(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    # Best-effort: annotate unexpected scans the ERP has since caught up with
    # (e.g. a WO receipt posted after the snapshot). Never blocks completion.
    try:
        recheck = recheck_unexpected_scans(session_id)
        resolved = sum(1 for r in recheck if r["auto_resolved"])
        if resolved:
            logger.info(
                "Audit session #%s: ERP re-check auto-resolved %d unexpected scan(s).",
                session_id,
                resolved,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ERP re-check of unexpected scans failed for session #%s: %s", session_id, exc)

    try:
        completed = audit_store.complete_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to complete audit session: %s", exc)
        return jsonify({"error": "complete_failed", "message": "Audit operation failed. Retry the same request."}), 500

    logger.info("Completed audit session #%s.", session_id)
    return jsonify(
        {
            "status": "completed",
            "session": {
                "id": completed["id"],
                "accuracy_pct": float(completed["accuracy_pct"]) if completed.get("accuracy_pct") is not None else None,
                "expected_count": completed["expected_count"],
                "verified_count": completed["verified_count"],
                "misplaced_count": completed["misplaced_count"],
                "missing_count": completed["missing_count"],
                "unexpected_count": completed["unexpected_count"],
            },
            "redirect": url_for("audit.audit_session_page", session_id=session_id),
        }
    ), 200


@bp.get("/api/audit/session/<int:session_id>/state")
@require_trusted_client
def api_audit_state(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    return jsonify(
        {
            "status": session_row.get("status"),
            "counts": audit_store.compute_counts(session_id),
        }
    ), 200


@bp.post("/api/audit/session/<int:session_id>/recheck")
@require_trusted_client
@require_csrf
def api_audit_recheck(session_id: int):
    """Re-query the ERP for this session's unexpected serials on demand."""
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    try:
        summary = recheck_unexpected_scans(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ERP re-check failed for session #%s: %s", session_id, exc)
        return jsonify({"error": "recheck_failed", "message": "Audit operation failed. Retry the same request."}), 500

    return jsonify(
        {
            "checked": len(summary),
            "auto_resolved": sum(1 for r in summary if r["auto_resolved"]),
            "rows": summary,
        }
    ), 200


@bp.get("/audit/export")
@require_trusted_client
def audit_day_export():
    """One CSV covering every audit session on a local calendar day.

    Answers "what was scanned today and what were the issues in each area"
    without opening each location's session: expected rows (verified /
    misplaced / missing) and unexpected scans, unioned, with resolutions.
    """
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit.audit_dashboard"))

    tz = resolve_timezone()
    day_param = (request.args.get("date") or "").strip()
    if day_param:
        try:
            day = date.fromisoformat(day_param)
        except ValueError:
            flash(f"Invalid date '{day_param}' — use YYYY-MM-DD.", "error")
            return redirect(url_for("audit.audit_dashboard"))
    else:
        day = datetime.now(tz).date()

    try:
        data = audit_store.day_export_rows(day.isoformat(), str(tz))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Audit day export failed: %s", exc)
        flash(f"Could not build the audit export: {exc}", "error")
        return redirect(url_for("audit.audit_dashboard"))

    if not data["sessions"]:
        flash(f"No audit sessions were started on {day.isoformat()}.", "error")
        return redirect(url_for("audit.audit_dashboard"))

    columns = [
        "session_id", "session_label", "operator", "result", "serial",
        "part_id", "part_description", "location_scope", "expected_location",
        "scanned_location", "scanned_at", "tied_wo", "sales_order",
        "resolution", "resolution_note", "resolved_by",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    def resolution_label(code):
        return audit_store.RESOLUTION_CODES.get(code, code) if code else ""

    for row in data["expected"]:
        writer.writerow(
            {
                "session_id": row["session_id"],
                "session_label": row.get("session_label") or "",
                "operator": row.get("operator") or "",
                "result": row.get("status") or "",
                "serial": row.get("serial") or "",
                "part_id": row.get("part_id") or "",
                "part_description": row.get("part_description") or "",
                "location_scope": row.get("scope") or "",
                "expected_location": row.get("expected_location") or "",
                "scanned_location": row.get("scanned_location") or "",
                "scanned_at": _audit_dt_display(row.get("scanned_at")) or "",
                "tied_wo": "yes" if row.get("tied_wo") else "",
                "sales_order": row.get("cust_order_id") or "",
                "resolution": resolution_label(row.get("resolution")),
                "resolution_note": row.get("resolution_note") or "",
                "resolved_by": row.get("resolved_by") or "",
            }
        )
    for row in data["unexpected"]:
        writer.writerow(
            {
                "session_id": row["session_id"],
                "session_label": row.get("session_label") or "",
                "operator": row.get("operator") or "",
                "result": "unexpected",
                "serial": row.get("serial") or "",
                "scanned_location": row.get("scanned_location") or "",
                "scanned_at": _audit_dt_display(row.get("scanned_at")) or "",
                "resolution": resolution_label(row.get("resolution")),
                "resolution_note": row.get("resolution_note") or "",
                "resolved_by": row.get("resolved_by") or "",
            }
        )

    safe = io.StringIO()
    safe_writer = csv.writer(safe)
    for record in csv.reader(io.StringIO(output.getvalue())):
        safe_writer.writerow([("'" + v) if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in record])
    payload = safe.getvalue().encode("utf-8-sig")  # BOM so Excel opens it cleanly
    return send_file(
        io.BytesIO(payload),
        as_attachment=True,
        download_name=f"audit_{day.isoformat()}.csv",
        mimetype="text/csv",
    )


@bp.get("/audit/session/<int:session_id>/export")
@require_trusted_client
def audit_session_export(session_id: int):
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit.audit_dashboard"))

    session_row = audit_store.get_session(session_id)
    if not session_row:
        flash(f"Audit session #{session_id} was not found.", "error")
        return redirect(url_for("audit.audit_dashboard"))

    items = audit_store.get_expected_items(session_id)
    unexpected = audit_store.get_unexpected_scans(session_id)
    resolutions = audit_store.get_resolutions(session_id)

    for item in items:
        res = resolutions.get((item.get("serial") or "").upper())
        item["resolution"] = audit_store.RESOLUTION_CODES.get(res["resolution"]) if res else None
        item["resolution_note"] = res["note"] if res else None
    for row in unexpected:
        res = resolutions.get((row.get("scanned_serial") or "").upper())
        row["resolution"] = audit_store.RESOLUTION_CODES.get(res["resolution"]) if res else None
        row["resolution_note"] = res["note"] if res else None
    resolution_rows = [
        {
            "serial": res["serial"],
            "exception_type": res["exception_type"],
            "resolution": audit_store.RESOLUTION_CODES.get(res["resolution"], res["resolution"]),
            "note": res["note"],
            "resolved_by": res["resolved_by"],
            "resolved_at": _audit_dt_display(res["resolved_at"]),
        }
        for res in resolutions.values()
    ]

    expected_df = pd.DataFrame(_audit_json_safe(items))
    unexpected_df = pd.DataFrame(_audit_json_safe(unexpected))
    resolutions_df = pd.DataFrame(_audit_json_safe(resolution_rows))
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        (expected_df if not expected_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Expected"
        )
        (unexpected_df if not unexpected_df.empty else pd.DataFrame(columns=["scanned_serial"])).to_excel(
            writer, index=False, sheet_name="Unexpected"
        )
        (resolutions_df if not resolutions_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Resolutions"
        )
    from openpyxl import load_workbook
    output.seek(0)
    book = load_workbook(output)
    for sheet in book:
        for cells in sheet:
            for cell in cells:
                if cell.data_type == "f": cell.data_type = "s"
    output = io.BytesIO()
    book.save(output)
    output.seek(0)
    scope_label = (session_row.get("scope") or "ALL").replace("/", "-")
    filename = f"audit_{scope_label}_session{session_id}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/audit/analytics")
@require_trusted_client
def audit_analytics_page():
    if not audit_store.is_available():
        return _audit_unavailable_response()

    days = _audit_analytics_window()
    payload = build_audit_analytics(days)

    # Human-readable timestamps for the tables (charts use the ISO values).
    for row in payload["sessions"]:
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    for row in payload["open_exceptions"]:
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    for row in payload["problem_locations"]:
        row["last_audited_display"] = _audit_dt_display(row.get("last_audited_at"))
    for row in payload["problem_serials"]:
        row["last_audited_display"] = _audit_dt_display(row.get("last_audited_at"))
    for row in payload["unexpected_serials"]:
        row["last_scanned_display"] = _audit_dt_display(row.get("last_scanned_at"))
    # ERP timestamps are company-local naive — format without tz conversion.
    for row in payload["dwell"].get("aged_serials", []):
        row["arrived_display"] = row["arrived_at"].strftime("%Y-%m-%d %H:%M")

    return render_template(
        "audit_analytics.html",
        audit_available=True,
        data=_audit_json_safe(payload),
        window_options=[7, 30, 90, 365],
    )


@bp.get("/api/audit/analytics")
@require_trusted_client
def api_audit_analytics():
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503
    return jsonify(_audit_json_safe(build_audit_analytics(_audit_analytics_window()))), 200
