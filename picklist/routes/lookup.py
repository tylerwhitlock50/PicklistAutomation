"""Lookup router, shipments and stock lookups."""
from typing import Any, Optional

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from picklist.config import ALLOC_PART_SEARCH_MIN_CHARS, logger
from picklist.domain import identity, shipments, stock
from picklist.security import require_trusted_client
from picklist.services.allocation_service import _clean_part_id
from picklist.services.audit_service import fetch_serial_onhand_locations
from picklist.services.orders_service import (
    _search_parts,
    _shipment_lookup,
    _shipment_lookup_params,
    build_stock_lookup,
)
from picklist.util import _audit_json_safe
from picklist.routes.orders import safe_return_path

bp = Blueprint("lookup", __name__)


@bp.get("/lookup")
@require_trusted_client
def lookup_page():
    return render_template("lookup.html")


@bp.get("/shipments")
@require_trusted_client
def shipments_page():
    params = _shipment_lookup_params()
    if params["serial"]:
        return redirect(url_for("serial.serial_history_page", serial=params["serial"]))
    result: dict[str, Any] = {"packlists": [], "summary": shipments.summarize([]), "params": params, "error": None}
    searched = bool(params["so"] or params["customer"] or request.args.get("start") or request.args.get("end") or request.args.get("go"))
    if searched:
        try:
            result = _shipment_lookup(params)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Shipment lookup failed")
            result["error"] = str(exc)
    return render_template(
        "shipments.html",
        result=_audit_json_safe(result),
        params=params,
        searched=searched,
        operator=identity.current_operator(),
    )


@bp.get("/api/shipments")
@require_trusted_client
def api_shipments():
    params = _shipment_lookup_params()
    try:
        result = _shipment_lookup(params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Shipment lookup failed")
        return jsonify({"error": str(exc), "params": params}), 502
    return jsonify(_audit_json_safe(result))


@bp.get("/stock")
@require_trusted_client
def stock_page():
    part = _clean_part_id(request.args.get("part")) or ""
    serial = (request.args.get("serial") or "").strip().upper()
    payload: Optional[dict[str, Any]] = None
    serial_payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    if part:
        try:
            payload = build_stock_lookup(part)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stock lookup failed for %s", part)
            error = str(exc)
    elif serial:
        try:
            serial_payload = stock.serial_locations_payload(serial, fetch_serial_onhand_locations([serial]))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Serial stock lookup failed for %s", serial)
            error = str(exc)
    return render_template(
        "stock.html",
        return_to=safe_return_path(request.args.get("return_to")) if request.args.get("return_to") else None,
        part=part,
        serial=serial,
        payload=_audit_json_safe(payload) if payload else None,
        serial_payload=_audit_json_safe(serial_payload) if serial_payload else None,
        error=error,
        operator=identity.current_operator(),
    )


@bp.get("/api/stock")
@require_trusted_client
def api_stock():
    part = _clean_part_id(request.args.get("part")) or ""
    serial = (request.args.get("serial") or "").strip().upper()
    if not part and not serial:
        return jsonify({"error": "part or serial is required"}), 400
    try:
        if part:
            return jsonify(_audit_json_safe(build_stock_lookup(part)))
        return jsonify(_audit_json_safe(stock.serial_locations_payload(serial, fetch_serial_onhand_locations([serial]))))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stock lookup failed")
        return jsonify({"error": str(exc)}), 502


@bp.get("/api/stock/parts")
@require_trusted_client
def api_stock_parts():
    term = (request.args.get("q") or "").strip().upper()
    if len(term) < ALLOC_PART_SEARCH_MIN_CHARS:
        return jsonify({"parts": []}), 200
    try:
        return jsonify({"parts": _search_parts(term)}), 200
    except Exception:  # noqa: BLE001
        logger.exception("Part search failed for %s", term)
        return jsonify({"error": "lookup_failed", "parts": []}), 500
