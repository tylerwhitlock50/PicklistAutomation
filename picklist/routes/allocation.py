"""Allocation lookup and promise-date editing."""
from datetime import date, datetime
from typing import Optional

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import text

from picklist.config import ALLOC_PART_SEARCH_MIN_CHARS, ALLOC_PARTS_FILE, logger
from picklist.domain import allocation
from picklist.erp import get_erp_write_engine, run_erp_query_file
from picklist.security import require_csrf, require_trusted_client
from picklist.services.allocation_service import (
    _allocation_options,
    _AllocationSaveConflict,
    _build_allocation_payload,
    _clean_part_id,
    _fetch_allocation_inputs,
    _parse_iso_date_field,
    ALLOC_SELECT_LINE_SQL,
    ALLOC_UPDATE_SQL,
)
from picklist.stores import allocation_store

bp = Blueprint("allocation", __name__)


@bp.get("/allocation")
@require_trusted_client
def allocation_page():
    prefill = _clean_part_id(request.args.get("part"))
    return render_template("allocation.html", prefill_part=prefill)


@bp.get("/api/allocation/parts")
@require_trusted_client
def api_allocation_parts():
    term = (request.args.get("q") or "").strip().upper()
    if len(term) < ALLOC_PART_SEARCH_MIN_CHARS:
        return jsonify({"parts": []}), 200
    try:
        df = run_erp_query_file(
            ALLOC_PARTS_FILE,
            {"pattern": f"%{term}%", "prefix": f"{term}%"},
            f"allocation part search '{term}'",
        )
    except Exception:
        logger.exception("Allocation part search failed for %s", term)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    parts = [
        {
            "part_id": str(row.get("PART_ID") or ""),
            "description": row.get("DESCRIPTION") if row.get("DESCRIPTION") == row.get("DESCRIPTION") else None,
            "on_hand": int(row.get("ON_HAND") or 0),
            "open_demand": int(row.get("OPEN_DEMAND") or 0),
        }
        for row in df.to_dict(orient="records")
    ]
    return jsonify({"parts": parts}), 200


@bp.get("/api/allocation/history")
@require_trusted_client
def api_allocation_history():
    part_id = _clean_part_id(request.args.get("part_id")) or None
    so = (request.args.get("so") or "").strip() or None
    if not part_id and not so:
        return jsonify({"error": "invalid", "message": "part_id or so is required."}), 400
    changes = allocation_store.recent_changes(part_id=part_id, cust_order_id=so)
    return jsonify({"changes": changes}), 200


@bp.get("/api/allocation/<part_id>")
@require_trusted_client
def api_allocation_detail(part_id: str):
    part_id = _clean_part_id(part_id)
    if not part_id:
        return jsonify({"error": "invalid", "message": "A part number is required."}), 400
    try:
        payload = _build_allocation_payload(part_id)
    except Exception:
        logger.exception("Allocation lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    if (
        not payload["supply"]["events"]
        and not payload["supply"]["informational"]
        and not payload["demand"]["lines"]
    ):
        return (
            jsonify(
                {
                    "error": "part_not_found",
                    "message": f"No open demand, stock, or production found for {part_id}.",
                }
            ),
            404,
        )
    return jsonify(payload), 200


@bp.post("/api/allocation/preview")
@require_trusted_client
@require_csrf
def api_allocation_preview():
    body = request.get_json(silent=True) or {}
    part_id = _clean_part_id(body.get("part_id"))
    so = (body.get("so") or "").strip()
    line_no = body.get("line_no")
    if not part_id or not so or not isinstance(line_no, int):
        return (
            jsonify({"error": "invalid", "message": "part_id, so, and line_no are required."}),
            400,
        )
    new_value, error = _parse_iso_date_field(body, "new_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400

    options = _allocation_options()
    try:
        supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    except Exception:
        logger.exception("Allocation preview lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    preview = allocation.preview_change(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        options["excluded_customers"],
        so,
        line_no,
        new_value,
    )
    preview["result"]["part_id"] = part_id
    return jsonify(preview), 200


@bp.get("/api/allocation/suggest")
@require_trusted_client
def api_allocation_suggest():
    part_id = _clean_part_id(request.args.get("part_id"))
    so = (request.args.get("so") or "").strip()
    try:
        line_no = int(request.args.get("line_no", ""))
        target_position = int(request.args.get("target_position", ""))
    except ValueError:
        return (
            jsonify({"error": "invalid", "message": "line_no and target_position must be numbers."}),
            400,
        )
    if not part_id or not so:
        return jsonify({"error": "invalid", "message": "part_id and so are required."}), 400

    options = _allocation_options()
    try:
        supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    except Exception:
        logger.exception("Allocation suggest lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    suggestion = allocation.suggest_promise_del(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        options["excluded_customers"],
        so,
        line_no,
        target_position,
    )
    status = 400 if suggestion.get("error") else 200
    return jsonify(suggestion), status


@bp.post("/api/allocation/promise-del")
@require_trusted_client
@require_csrf
def api_allocation_save():
    body = request.get_json(silent=True) or {}
    part_id = _clean_part_id(body.get("part_id"))
    so = (body.get("so") or "").strip()
    line_no = body.get("line_no")
    changed_by = (body.get("changed_by") or "").strip()
    reason = (body.get("reason") or "").strip() or None
    if not part_id or not so or not isinstance(line_no, int):
        return (
            jsonify({"error": "invalid", "message": "part_id, so, and line_no are required."}),
            400,
        )
    if not changed_by:
        return (
            jsonify(
                {
                    "error": "invalid",
                    "message": "Set your operator name before saving — the audit trail requires it.",
                }
            ),
            400,
        )
    new_value, error = _parse_iso_date_field(body, "new_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400
    expected_old_value, error = _parse_iso_date_field(body, "expected_old_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400

    # Server-computed baseline: never trust the client's idea of its position.
    try:
        baseline = _build_allocation_payload(part_id)
    except Exception:
        logger.exception("Allocation save baseline failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Nothing was saved.",
                }
            ),
            500,
        )
    position_before = None
    line_found = False
    for line in baseline["demand"]["lines"]:
        if line["so"] == so and line["line_no"] == line_no:
            position_before = line["position"]
            line_found = True
            break
    if not line_found:
        return (
            jsonify(
                {
                    "error": "line_not_found",
                    "message": f"{so} line {line_no} has no open demand for {part_id}.",
                }
            ),
            404,
        )

    audit_id: Optional[int] = None
    erp_updated = False
    old_value_iso: Optional[str] = None
    try:
        engine = get_erp_write_engine()
        with engine.begin() as conn:
            row = (
                conn.execute(text(ALLOC_SELECT_LINE_SQL), {"so": so, "line": line_no})
                .mappings()
                .first()
            )
            if row is None:
                return (
                    jsonify(
                        {
                            "error": "line_not_found",
                            "message": f"{so} line {line_no} was not found in VISUAL.",
                        }
                    ),
                    404,
                )
            row_part = str(row["PART_ID"] or "").strip().upper()
            if row_part != part_id:
                return (
                    jsonify(
                        {
                            "error": "invalid",
                            "message": f"{so} line {line_no} is {row_part}, not {part_id}.",
                        }
                    ),
                    400,
                )
            current = row["PROMISE_DEL_DATE"]
            if isinstance(current, datetime):
                current = current.date()
            old_value_iso = current.isoformat() if current else None

            result = conn.execute(
                text(ALLOC_UPDATE_SQL),
                {
                    "new_value": new_value,
                    "so": so,
                    "line": line_no,
                    "old_value": expected_old_value,
                },
            )
            if result.rowcount == 0:
                raise _AllocationSaveConflict(current)
            erp_updated = True

            # Inside the ERP transaction on purpose: if the audit row cannot
            # be written, the ERP change must not survive.
            audit_id = allocation_store.record_change(
                changed_by=changed_by,
                cust_order_id=so,
                line_no=line_no,
                part_id=part_id,
                old_value=old_value_iso,
                new_value=new_value.isoformat() if new_value else None,
                reason=reason,
                position_before=position_before,
                position_after=None,
            )
    except _AllocationSaveConflict as conflict:
        return (
            jsonify(
                {
                    "error": "conflict",
                    "message": "Promise Del Date changed since this screen was loaded. Reloaded value shown — review and retry.",
                    "current_value": conflict.current_value.isoformat()
                    if conflict.current_value
                    else None,
                }
            ),
            409,
        )
    except Exception:
        if erp_updated and audit_id is None:
            logger.exception(
                "Audit write failed for %s line %s; ERP update rolled back.", so, line_no
            )
            return (
                jsonify(
                    {
                        "error": "audit_failed",
                        "message": "The audit trail could not be written, so the change was rolled back.",
                    }
                ),
                500,
            )
        if audit_id is not None:
            # Commit itself failed after the audit insert: compensate.
            try:
                allocation_store.delete_change(audit_id)
            except Exception:
                logger.exception("Compensating audit delete failed for id %s", audit_id)
        logger.exception("Promise Del save failed for %s line %s", so, line_no)
        return (
            jsonify(
                {
                    "error": "save_failed",
                    "message": "The save failed. Nothing was changed in VISUAL.",
                }
            ),
            500,
        )

    logger.info(
        "Promise Del Date for %s line %s (%s) changed %s -> %s by %s.",
        so,
        line_no,
        part_id,
        old_value_iso or "blank",
        new_value.isoformat() if new_value else "blank",
        changed_by,
    )

    fresh_payload: Optional[dict] = None
    position_after = None
    warning = None
    try:
        fresh_payload = _build_allocation_payload(part_id)
        for line in fresh_payload["demand"]["lines"]:
            if line["so"] == so and line["line_no"] == line_no:
                position_after = line["position"]
                break
        allocation_store.set_position_after(audit_id, position_after)
    except Exception:
        logger.exception("Post-save allocation rebuild failed for %s", part_id)
        warning = "Saved, but the refreshed allocation could not be loaded. Reload the page."

    return (
        jsonify(
            {
                "status": "saved",
                "audit_id": audit_id,
                "old_value": old_value_iso,
                "new_value": new_value.isoformat() if new_value else None,
                "position_before": position_before,
                "position_after": position_after,
                "result": fresh_payload,
                "warning": warning,
            }
        ),
        200,
    )
