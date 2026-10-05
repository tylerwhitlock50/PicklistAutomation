"""Order readiness pages and APIs."""
import os
from datetime import timedelta
from typing import Any

from flask import Blueprint, g, jsonify, render_template, request

from picklist.config import logger
from picklist.domain import identity, readiness, shipments
from picklist.security import require_csrf, require_trusted_client
from picklist.services import readiness_service
from picklist.services.orders_service import _order_documents_view, fetch_order_shipment_rows
from picklist.services.settings_service import get_ffl_doc_config
from picklist.stores import request_store
from picklist.timeutil import _today_local
from picklist.util import _audit_json_safe

bp = Blueprint("orders", __name__)


READINESS_WINDOWS = {
    "7": "Due in the next 7 days",
    "14": "Due in the next 14 days",
    "30": "Due in the next 30 days",
    "overdue": "Overdue only",
    "all": "Everything in the window",
}


READINESS_STALE_DAYS = int(os.getenv("READINESS_STALE_DAYS", "60"))


def _readiness_filters() -> dict[str, Any]:
    operator = identity.current_operator()
    if "owner" in request.args:
        owner = (request.args.get("owner") or "").strip().lower()
    else:
        owner = operator.team if operator and operator.team in ("sales", "finance", "shipping") else ""
    window = (request.args.get("window") or "14").strip().lower()
    if window not in READINESS_WINDOWS:
        window = "14"
    today = _today_local()
    due_before = due_after = None
    if window == "overdue":
        due_before = (today - timedelta(days=1)).isoformat()
    elif window != "all":
        due_before = (today + timedelta(days=int(window))).isoformat()
        due_after = (today - timedelta(days=READINESS_STALE_DAYS)).isoformat()
    return {
        "owner": owner,
        "state": (request.args.get("state") or "").strip().upper(),
        "reason": (request.args.get("reason") or "").strip().lower(),
        "customer": (request.args.get("customer") or "").strip(),
        "q": (request.args.get("q") or "").strip(),
        "firearms": request.args.get("firearms") == "1",
        "blocking": request.args.get("blocking") == "1",
        # Stock holds are noise for Sales/Finance/Shipping; show them only on request,
        # when Production is the selected owner, or when a stock reason is filtered.
        "stock": (
            request.args.get("stock") == "1"
            or owner == readiness.OWNER_PRODUCTION
            or (request.args.get("reason") or "").strip().lower() in readiness.STOCK_REASONS
        ),
        # RMA / warranty orders never go through the picklist; hidden unless asked for.
        "rma": request.args.get("rma") == "1" or (request.args.get("reason") or "").strip().lower() == "rma_excluded",
        "window": window,
        "due_before": due_before,
        "due_after": due_after,
    }


def _readiness_sorted(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"BLOCKED": 0, "ATTENTION": 1, "READY": 2}
    return sorted(
        orders,
        key=lambda o: (rank.get(o.get("state"), 3), o.get("due") or "9999-12-31", o.get("order_id") or ""),
    )


@bp.get("/orders")
@require_trusted_client
def orders_page():
    payload = _audit_json_safe(readiness_service.current_payload())
    filters = _readiness_filters()
    payload = readiness_service.orders_view(payload, hide_stock=not filters["stock"], hide_rma=not filters["rma"])
    orders = _readiness_sorted(
        readiness_service.filter_orders(
            payload.get("orders") or [],
            owner=filters["owner"] or None,
            state=filters["state"] or None,
            reason=filters["reason"] or None,
            customer=filters["customer"] or None,
            query=filters["q"] or None,
            firearms_only=filters["firearms"],
            due_before=filters["due_before"],
            due_after=filters["due_after"],
            blocking_only=filters["blocking"],
        )
    )
    return render_template(
        "orders.html",
        windows=READINESS_WINDOWS,
        payload=payload,
        orders=orders,
        filters=filters,
        hold_reasons=readiness_service.reason_options(),
        owner_labels=readiness.OWNER_LABELS,
        states=readiness.ORDER_STATES,
        operator=identity.current_operator(),
    )


@bp.get("/orders/<order_id>")
@require_trusted_client
def order_detail_page(order_id: str):
    detail = _audit_json_safe(readiness_service.order_detail(order_id))
    status = 200 if detail.get("found") or detail.get("error") else 404
    so = order_id.strip().upper()
    return (
        render_template(
            "order_detail.html",
            order=detail,
            owner_labels=readiness.OWNER_LABELS,
            bin_labels=readiness.BIN_CLASS_LABELS,
            operator=identity.current_operator(),
            order_requests=request_store.list_requests(cust_order_id=so, limit=50),
            order_holds=request_store.manual_holds_for_order(so),
            order_documents=_order_documents_view(so) if detail.get("found") else [],
            ocr_enabled=get_ffl_doc_config()["enabled"],
            exception_kinds=request_store.EXCEPTION_KINDS,
        ),
        status,
    )


@bp.get("/api/orders")
@require_trusted_client
def api_orders():
    payload = _audit_json_safe(readiness_service.current_payload())
    filters = _readiness_filters()
    orders = _readiness_sorted(
        readiness_service.filter_orders(
            payload.get("orders") or [],
            owner=filters["owner"] or None,
            state=filters["state"] or None,
            reason=filters["reason"] or None,
            customer=filters["customer"] or None,
            query=filters["q"] or None,
            firearms_only=filters["firearms"],
            due_before=filters["due_before"],
            due_after=filters["due_after"],
            blocking_only=filters["blocking"],
        )
    )
    return jsonify(
        {
            "evaluated_at": payload.get("evaluated_at"),
            "summary": payload.get("summary"),
            "error": payload.get("error"),
            "filters": filters,
            "orders": orders,
        }
    )


@bp.get("/api/orders/<order_id>")
@require_trusted_client
def api_order_detail(order_id: str):
    detail = _audit_json_safe(readiness_service.order_detail(order_id))
    if not detail.get("found") and not detail.get("error"):
        return jsonify({"error": f"{order_id} was not found.", **detail}), 404
    return jsonify(detail)


@bp.post("/api/orders/<order_id>/holds/<int:hold_id>/ack")
@require_trusted_client
@require_csrf
@identity.require_operator
def api_order_hold_ack(order_id: str, hold_id: int):
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or request.form.get("note") or "").strip() or None
    try:
        hold = readiness_service.acknowledge(
            hold_id, actor=g.operator.name, actor_team=g.operator.team, note=note
        )
    except LookupError:
        return jsonify({"ok": False, "message": "Hold not found."}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if str(hold.get("cust_order_id") or "").upper() != order_id.strip().upper():
        return jsonify({"ok": False, "message": "Hold does not belong to this order."}), 400
    return jsonify({"ok": True, "hold": _audit_json_safe(hold)})


@bp.post("/api/readiness/refresh")
@require_trusted_client
@require_csrf
def api_readiness_refresh():
    """Start a readiness refresh in the background and return at once.

    The full refresh (ERP query plus per-order document checks) can run for
    minutes, longer than a gunicorn worker may block, so the page polls
    ``/api/readiness/refresh/status`` until the job finishes. Passing
    ``{"wait": true}`` keeps the old synchronous behaviour for tests and tools.
    """
    body = request.get_json(silent=True) or {}
    wait = bool(body.get("wait"))
    status = readiness_service.start_refresh("manual", wait=wait)
    if wait:
        ok = status.get("error") is None
        return jsonify(
            {
                "ok": ok,
                "started": status.get("started"),
                "error": status.get("error"),
                "evaluated_at": status.get("evaluated_at"),
                "summary": status.get("summary"),
                "reconcile": status.get("reconcile"),
            }
        ), (200 if ok else 502)
    return jsonify({"ok": True, "started": status.get("started"), "status": status}), 202


@bp.get("/api/readiness/refresh/status")
@require_trusted_client
def api_readiness_refresh_status():
    return jsonify(readiness_service.refresh_status())


@bp.get("/api/orders/<order_id>/shipments")
@require_trusted_client
def api_order_shipments(order_id: str):
    try:
        packlists = shipments.group_packlists(fetch_order_shipment_rows(order_id.strip().upper()))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Order shipments lookup failed for %s", order_id)
        return jsonify({"error": str(exc), "order_id": order_id}), 502
    return jsonify(_audit_json_safe({
        "order_id": order_id.strip().upper(),
        "packlists": packlists,
        "summary": shipments.summarize(packlists),
    }))
