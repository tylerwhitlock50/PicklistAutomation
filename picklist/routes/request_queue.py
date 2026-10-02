"""Cross-team request queue."""
from typing import Any

from flask import abort, Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from picklist.domain import identity
from picklist.security import require_csrf, require_trusted_client
from picklist.services import request_service
from picklist.services.orders_service import _readiness_blocking_holds
from picklist.services.shipping_service import _active_manual_holds
from picklist.stores import request_store
from picklist.util import _audit_json_safe

bp = Blueprint("requests", __name__)


def _request_options() -> dict[str, Any]:
    return {
        "request_types": request_store.REQUEST_TYPES,
        "exception_kinds": request_store.EXCEPTION_KINDS,
        "problem_kinds": request_store.PROBLEM_KINDS,
        "service_levels": request_store.SERVICE_LEVELS,
        "statuses": request_store.STATUSES,
        "priorities": request_store.PRIORITIES,
        "team_labels": request_service.TEAM_LABELS,
        "transitions": {k: sorted(v) for k, v in request_store.TRANSITIONS.items()},
    }


def _request_list_for_args(operator) -> tuple[list[dict], dict[str, Any]]:
    scope = (request.args.get("scope") or ("mine" if operator else "all")).strip().lower()
    status = (request.args.get("status") or "open").strip().lower()
    rtype = (request.args.get("type") or "").strip()
    so = (request.args.get("so") or "").strip().upper()
    kwargs: dict[str, Any] = {"limit": 300}
    if rtype in request_store.REQUEST_TYPES:
        kwargs["request_type"] = rtype
    if so:
        kwargs["cust_order_id"] = so
    if status == "open":
        kwargs["open_only"] = True
    elif status == "overdue":
        kwargs["overdue_only"] = True
    elif status in request_store.STATUSES:
        kwargs["status"] = status
    if scope == "team" and operator and operator.team:
        rows = request_store.list_requests(owner_team=operator.team, **kwargs)
    elif scope == "mine" and operator:
        seen: dict[int, dict] = {}
        for row in request_store.list_requests(created_by=operator.name, **kwargs):
            seen[row["id"]] = row
        for row in request_store.list_requests(assigned_to=operator.name, **kwargs):
            seen.setdefault(row["id"], row)
        rows = sorted(seen.values(), key=lambda r: (not r["is_open"], r.get("sla_due_at") or r["created_at"]))
    else:
        scope = "all"
        rows = request_store.list_requests(**kwargs)
    return rows, {"scope": scope, "status": status, "type": rtype, "so": so}


@bp.get("/requests")
@require_trusted_client
def requests_page():
    operator = identity.current_operator()
    rows, filters = _request_list_for_args(operator)
    prefill = {
        "type": (request.args.get("new") or request.args.get("type") or "").strip(),
        "so": (request.args.get("so") or "").strip().upper(),
        "wo": (request.args.get("wo") or "").strip().upper(),
        "part": (request.args.get("part") or "").strip().upper(),
        "serial": (request.args.get("serial") or "").strip().upper(),
    }
    return render_template(
        "requests.html",
        rows=_audit_json_safe(rows),
        filters=filters,
        summary=request_store.queue_summary(),
        options=_request_options(),
        prefill=prefill,
        operator=operator,
        open_form=bool(request.args.get("new")),
        manual_holds=_active_manual_holds(),
    )


@bp.post("/requests")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_create():
    form = request.form
    request_type = (form.get("request_type") or "").strip()
    fields = {key: form.get(key) for key in (
        "needed_by", "expedite", "service_level", "ship_complete", "exception_kind", "expires_at",
        "expected_location", "actual_location", "qty", "problem_kind",
    ) if form.get(key) is not None}
    try:
        req = request_service.create(
            request_type=request_type,
            actor=g.operator.name,
            actor_team=g.operator.team,
            title=form.get("title"),
            body=form.get("body") or "",
            cust_order_id=form.get("cust_order_id"),
            work_order_id=form.get("work_order_id"),
            customer_id=form.get("customer_id"),
            part_id=form.get("part_id"),
            serial_no=form.get("serial_no"),
            fields=fields,
            priority=form.get("priority") or "normal",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("requests.requests_page", new=request_type or None, so=form.get("cust_order_id") or None))
    flash(f"Request #{req['id']} sent to {request_service.TEAM_LABELS.get(req['owner_team'], req['owner_team'])}.", "success")
    return redirect(url_for("requests.request_detail_page", request_id=req["id"]))


@bp.get("/requests/<int:request_id>")
@require_trusted_client
def request_detail_page(request_id: int):
    req = request_store.get_request(request_id)
    if req is None:
        abort(404)
    hold = request_store.get_manual_hold(req["linked_manual_hold_id"]) if req.get("linked_manual_hold_id") else None
    blockers = _readiness_blocking_holds(req["cust_order_id"]) if req.get("cust_order_id") else []
    return render_template(
        "request_detail.html",
        req=_audit_json_safe(req),
        hold=hold,
        blockers=[b for b in blockers if b.get("reason_code") != "manual_hold"],
        legal=sorted(request_store.TRANSITIONS.get(req["status"], set())),
        options=_request_options(),
        operator=identity.current_operator(),
    )


@bp.post("/requests/<int:request_id>/transition")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_transition(request_id: int):
    to_status = (request.form.get("to_status") or "").strip().lower()
    try:
        request_service.transition(
            request_id,
            to_status,
            actor=g.operator.name,
            actor_team=g.operator.team,
            note=request.form.get("note"),
            resolution=request.form.get("resolution"),
            accept_expedite=bool(request.form.get("accept_expedite")),
        )
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Request #{request_id} is now {to_status.replace('_', ' ')}.", "success")
    return redirect(url_for("requests.request_detail_page", request_id=request_id))


@bp.post("/requests/<int:request_id>/assign")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_assign(request_id: int):
    try:
        request_service.assign(request_id, request.form.get("assignee") or "", actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("requests.request_detail_page", request_id=request_id))


@bp.post("/requests/<int:request_id>/comment")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_comment(request_id: int):
    try:
        request_service.comment(request_id, request.form.get("note") or "", actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("requests.request_detail_page", request_id=request_id))


@bp.get("/api/requests")
@require_trusted_client
def api_requests():
    rows, filters = _request_list_for_args(identity.current_operator())
    return jsonify(_audit_json_safe({"requests": rows, "filters": filters, "summary": request_store.queue_summary()}))


@bp.get("/api/requests/<int:request_id>")
@require_trusted_client
def api_request_detail(request_id: int):
    req = request_store.get_request(request_id)
    if req is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_audit_json_safe(req))


@bp.post("/api/holds/<int:hold_id>/release")
@require_trusted_client
@require_csrf
@identity.require_operator
def api_hold_release(hold_id: int):
    if g.operator.team not in ("shipping", "management"):
        return jsonify({"ok": False, "message": "Only Shipping or Management can release a hold."}), 403
    try:
        released = request_service.release_hold(hold_id, actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        return jsonify({"ok": False, "message": "Hold not found."}), 404
    return jsonify({"ok": True, "released": released})
