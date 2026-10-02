"""Serial history lookup."""
from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from picklist.config import (
    logger,
    SERIAL_HISTORY_SHIPMENTS_FILE,
    SERIAL_HISTORY_TRACE_FILE,
    SERIAL_HISTORY_TRANSACTIONS_FILE,
    SERIAL_MAX_LENGTH,
)
from picklist.domain import serial_history
from picklist.erp import run_erp_query_file
from picklist.security import require_trusted_client

bp = Blueprint("serial", __name__)


def _clean_serial_input(raw: Optional[str]) -> str:
    return (raw or "").strip().upper()


@bp.get("/serial-history")
@require_trusted_client
def serial_history_page():
    prefill = _clean_serial_input(request.args.get("serial"))[:SERIAL_MAX_LENGTH]
    return render_template("serial_history.html", prefill_serial=prefill)


@bp.get("/api/serial-history")
@require_trusted_client
def api_serial_history():
    serial = _clean_serial_input(request.args.get("serial"))
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400
    if len(serial) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Serial number is too long."}), 400

    try:
        trace_df = run_erp_query_file(
            SERIAL_HISTORY_TRACE_FILE, {"serial": serial}, "serial history trace lookup"
        )
        if trace_df.empty:
            txns_df = trace_df
            shipments_df = trace_df
        else:
            txns_df = run_erp_query_file(
                SERIAL_HISTORY_TRANSACTIONS_FILE, {"serial": serial}, "serial history transactions"
            )
            shipments_df = run_erp_query_file(
                SERIAL_HISTORY_SHIPMENTS_FILE, {"serial": serial}, "serial history shipments"
            )
    except Exception:
        logger.exception("Serial history lookup failed for %s", serial)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )

    payload = serial_history.build_serial_history(serial, trace_df, txns_df, shipments_df)
    return jsonify(payload), 200
