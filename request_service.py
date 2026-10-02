"""Request queue orchestration: store writes plus the side effects that make a
request mean something on the floor.

- An accepted ship request (expedite) becomes a release-gate exception so the
  next picklist includes the order, unless a blocking readiness hold says it
  cannot ship anyway.
- An acknowledged hold request becomes a manual order hold that removes the
  order from the picklist and shows on the readiness pages until it expires.
- Every meaningful change posts a Teams card to the team that has to act.

app.py injects the gate/notifier/readiness callables through ``configure``.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

import request_store

_logger = logging.getLogger("picklist-app.requests")
_deps: dict[str, Any] = {
    "add_exception": None,         # (cust_order_id, reason, created_by, expires_at) -> int
    "revoke_exception": None,      # (exception_id, actor) -> bool
    "invalidate_gate_cache": None, # () -> None
    "refresh_readiness": None,     # () -> None (async ok)
    "blocking_holds": None,        # (order_id) -> list[dict]
    "notify": None,                # (event, **card) -> bool
    "public_url": None,            # (path) -> str | None
    "now": None,                   # () -> datetime (tz-aware, local)
    "expedite_max_hours": 72,
    "logger": None,
}
TEAM_LABELS = {"sales": "Inside Sales", "shipping": "Shipping", "finance": "Finance", "management": "Management"}


def configure(**deps: Any) -> None:
    unknown = set(deps) - set(_deps)
    if unknown:
        raise ValueError(f"unknown request_service deps: {sorted(unknown)}")
    _deps.update(deps)
    if deps.get("logger") is not None:
        global _logger  # noqa: PLW0603
        _logger = deps["logger"]


def _call(name: str, *args: Any, default: Any = None) -> Any:
    fn = _deps.get(name)
    if fn is None:
        return default
    return fn(*args)


def _now() -> dt.datetime:
    value = _call("now")
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _link(path: str) -> Optional[str]:
    try:
        return _call("public_url", path)
    except Exception:  # noqa: BLE001
        return None


def _notify(event: str, **card: Any) -> None:
    try:
        _call("notify", event, **card)
    except TypeError:
        # notify is positional-only in some stubs
        try:
            fn = _deps.get("notify")
            if fn:
                fn(event, **card)
        except Exception:  # noqa: BLE001
            _logger.exception("Request notification failed (%s)", event)
    except Exception:  # noqa: BLE001
        _logger.exception("Request notification failed (%s)", event)


def _refresh_readiness() -> None:
    try:
        _call("refresh_readiness")
    except Exception:  # noqa: BLE001
        _logger.exception("Readiness refresh after request change failed")


def _invalidate_gate() -> None:
    try:
        _call("invalidate_gate_cache")
    except Exception:  # noqa: BLE001
        _logger.exception("Release gate cache invalidation failed")


# --------------------------------------------------------------------------- facts for cards


def _request_facts(req: dict[str, Any]) -> list[tuple[str, Any]]:
    facts: list[tuple[str, Any]] = [("Requested by", f"{req['created_by']} ({TEAM_LABELS.get(req.get('created_team'), req.get('created_team') or 'unknown')})")]
    if req.get("cust_order_id"):
        facts.append(("Order", req["cust_order_id"]))
    if req.get("work_order_id"):
        facts.append(("Work order", req["work_order_id"]))
    fields = req.get("fields") or {}
    if req["request_type"] == "ship_request":
        if fields.get("needed_by"):
            facts.append(("Needed by", fields["needed_by"]))
        facts.append(("Service", request_store.SERVICE_LEVELS.get(fields.get("service_level", ""), fields.get("service_level", ""))))
        if fields.get("expedite"):
            facts.append(("Expedite", "yes"))
    elif req["request_type"] == "hold_exception":
        facts.append(("Hold type", request_store.EXCEPTION_KINDS.get(fields.get("exception_kind", ""), "")))
        facts.append(("Expires", (fields.get("expires_at") or "")[:10]))
    elif req["request_type"] == "inventory_discrepancy":
        if fields.get("part_id"):
            facts.append(("Part", fields["part_id"]))
        if fields.get("serial_no"):
            facts.append(("Serial", fields["serial_no"]))
        if fields.get("expected_location") or fields.get("actual_location"):
            facts.append(("Expected / actual", f"{fields.get('expected_location') or '?'} / {fields.get('actual_location') or '?'}"))
    elif req["request_type"] == "order_problem":
        facts.append(("Problem", request_store.PROBLEM_KINDS.get(fields.get("problem_kind", ""), "")))
    facts.append(("Priority", req.get("priority", "normal")))
    if req.get("sla_due_at"):
        facts.append(("Due", req["sla_due_at"][:16].replace("T", " ")))
    return facts


# --------------------------------------------------------------------------- public API


def create(**kwargs: Any) -> dict[str, Any]:
    req = request_store.create_request(**kwargs)
    _notify(
        "request_created",
        title=f"New {req['type_label'].lower()} for {TEAM_LABELS.get(req['owner_team'], req['owner_team'])}: {req['title']}",
        text=req.get("body") or None,
        facts=_request_facts(req),
        link=_link(f"/requests/{req['id']}"),
        link_label="Open request",
        event_key=f"request_created:{req['id']}",
    )
    return req


def _expedite_expiry(req: dict[str, Any]) -> dt.datetime:
    now = _now()
    max_hours = int(_deps.get("expedite_max_hours") or 72)
    cap = now + dt.timedelta(hours=max_hours)
    needed_by = (req.get("fields") or {}).get("needed_by")
    if needed_by:
        try:
            day = dt.date.fromisoformat(str(needed_by)[:10])
            end_of_day = dt.datetime.combine(day, dt.time(23, 59), tzinfo=now.tzinfo)
            if end_of_day > now:
                return min(end_of_day, cap)
        except ValueError:
            pass
    return cap


def blocking_holds_for(order_id: str) -> list[dict[str, Any]]:
    try:
        holds = _call("blocking_holds", order_id, default=[]) or []
    except Exception:  # noqa: BLE001
        _logger.exception("Blocking hold lookup failed for %s", order_id)
        return []
    return [h for h in holds if h.get("blocking") and h.get("reason_code") not in {"manual_hold"}]


def _accept_expedite(req: dict[str, Any], *, actor: str, actor_team: str) -> Optional[int]:
    if req.get("linked_exception_id"):
        return req["linked_exception_id"]
    order_id = req.get("cust_order_id") or ""
    blockers = blocking_holds_for(order_id)
    if blockers:
        first = blockers[0]
        raise ValueError(
            f"{order_id} cannot be expedited: {first.get('label') or first.get('reason_code')} "
            f"(owner {TEAM_LABELS.get(first.get('owner_team'), first.get('owner_team'))}). Clear that hold first."
        )
    manual = [h for h in (_call("blocking_holds", order_id, default=[]) or []) if h.get("reason_code") == "manual_hold"]
    if manual:
        raise ValueError(f"{order_id} is under a manual hold. Release the hold before expediting.")
    expires = _expedite_expiry(req)
    exception_id = _call(
        "add_exception",
        order_id,
        f"Ship request #{req['id']} by {req['created_by']}: {req['title']}",
        actor,
        expires.isoformat(),
    )
    request_store.record_link_event(
        req["id"], "exception_added", actor=actor, actor_team=actor_team,
        note=f"release-gate exception #{exception_id} until {expires.isoformat(timespec='minutes')}",
        linked_exception_id=exception_id,
    )
    _invalidate_gate()
    return exception_id


def _revoke_expedite(req: dict[str, Any], *, actor: str, actor_team: str) -> None:
    exception_id = req.get("linked_exception_id")
    if not exception_id:
        return
    try:
        revoked = _call("revoke_exception", exception_id, actor, default=False)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Could not revoke exception %s", exception_id)
        revoked = False
        request_store.record_link_event(req["id"], "exception_revoked", actor=actor, actor_team=actor_team, note=f"revoke failed: {exc}")
        return
    request_store.record_link_event(
        req["id"], "exception_revoked", actor=actor, actor_team=actor_team,
        note=f"release-gate exception #{exception_id} {'revoked' if revoked else 'was already inactive'}",
    )
    _invalidate_gate()


def _activate_hold(req: dict[str, Any], *, actor: str, actor_team: str) -> Optional[int]:
    if req.get("linked_manual_hold_id"):
        return req["linked_manual_hold_id"]
    fields = req.get("fields") or {}
    hold_id = request_store.add_manual_hold(
        cust_order_id=req.get("cust_order_id") or "",
        work_order_id=req.get("work_order_id") or fields.get("work_order_id"),
        hold_kind=fields.get("exception_kind", ""),
        reason=f"Request #{req['id']}: {req['title']}" + (f" - {req['body']}" if req.get("body") else ""),
        expires_at=fields.get("expires_at", ""),
        created_by=actor,
        request_id=req["id"],
    )
    request_store.record_link_event(
        req["id"], "hold_added", actor=actor, actor_team=actor_team,
        note=f"manual hold #{hold_id} until {str(fields.get('expires_at', ''))[:10]}",
        linked_manual_hold_id=hold_id,
    )
    _invalidate_gate()
    _refresh_readiness()
    return hold_id


def _release_hold(req: dict[str, Any], *, actor: str, actor_team: str, why: str) -> None:
    hold_id = req.get("linked_manual_hold_id")
    if not hold_id:
        return
    released = request_store.release_manual_hold(hold_id, actor=actor)
    request_store.record_link_event(
        req["id"], "hold_released", actor=actor, actor_team=actor_team,
        note=f"manual hold #{hold_id} {'released' if released else 'was already released'} ({why})",
    )
    if released:
        _invalidate_gate()
        _refresh_readiness()


def transition(request_id: int, to_status: str, *, actor: str, actor_team: str = "",
               note: Optional[str] = None, resolution: Optional[str] = None,
               accept_expedite: bool = False) -> dict[str, Any]:
    req = request_store.get_request(request_id, with_events=False)
    if req is None:
        raise LookupError(f"Request #{request_id} not found.")
    target = (to_status or "").strip().lower()
    if target not in request_store.TRANSITIONS.get(req["status"], set()):
        raise ValueError(f"A request that is {req['status'].replace('_', ' ')} cannot move to {target.replace('_', ' ')}.")

    accepting = target in ("acknowledged", "in_progress")
    if req["request_type"] == "ship_request" and accepting and (accept_expedite or (req.get("fields") or {}).get("expedite")):
        if actor_team not in ("shipping", "management"):
            raise ValueError("Only Shipping or Management can accept an expedite.")
        _accept_expedite(req, actor=actor, actor_team=actor_team)
    if req["request_type"] == "hold_exception" and accepting:
        if actor_team not in ("shipping", "management"):
            raise ValueError("Only Shipping or Management can approve a hold.")
        _activate_hold(req, actor=actor, actor_team=actor_team)

    updated = request_store.transition(request_id, target, actor=actor, actor_team=actor_team, note=note, resolution=resolution)

    if target in request_store.TERMINAL_STATUSES:
        if req["request_type"] == "ship_request":
            _revoke_expedite(updated, actor=actor, actor_team=actor_team)
        if req["request_type"] == "hold_exception":
            _release_hold(updated, actor=actor, actor_team=actor_team, why=target)
        _notify(
            "request_done",
            title=f"{updated['type_label']} {target}: {updated['title']}",
            text=(resolution or note or None),
            facts=[("Closed by", f"{actor} ({TEAM_LABELS.get(actor_team, actor_team or 'unknown')})"), ("Requested by", updated["created_by"])]
            + ([("Order", updated["cust_order_id"])] if updated.get("cust_order_id") else []),
            link=_link(f"/requests/{updated['id']}"),
            link_label="Open request",
            event_key=f"request_{target}:{updated['id']}:{updated['updated_at']}",
        )
    return request_store.get_request(request_id)  # type: ignore[return-value]


def assign(request_id: int, assignee: str, *, actor: str, actor_team: str = "") -> dict[str, Any]:
    updated = request_store.assign(request_id, assignee, actor=actor, actor_team=actor_team)
    if assignee:
        _notify(
            "request_assigned",
            title=f"{updated['type_label']} assigned to {assignee}: {updated['title']}",
            facts=[("Assigned by", actor)] + ([("Order", updated["cust_order_id"])] if updated.get("cust_order_id") else []),
            link=_link(f"/requests/{updated['id']}"),
            link_label="Open request",
            event_key=f"request_assigned:{updated['id']}:{updated['updated_at']}",
        )
    return updated


def comment(request_id: int, note: str, *, actor: str, actor_team: str = "") -> dict[str, Any]:
    return request_store.comment(request_id, note, actor=actor, actor_team=actor_team)


def release_hold(hold_id: int, *, actor: str, actor_team: str = "") -> bool:
    hold = request_store.get_manual_hold(hold_id)
    if hold is None:
        raise LookupError(f"Hold #{hold_id} not found.")
    released = request_store.release_manual_hold(hold_id, actor=actor)
    if released:
        if hold.get("request_id"):
            request_store.record_link_event(hold["request_id"], "hold_released", actor=actor, actor_team=actor_team,
                                            note=f"manual hold #{hold_id} released early")
        _invalidate_gate()
        _refresh_readiness()
    return released


def expire_holds() -> list[dict[str, Any]]:
    expired = request_store.expire_manual_holds(_now())
    for hold in expired:
        if hold.get("request_id"):
            try:
                request_store.record_link_event(hold["request_id"], "hold_released", actor="system", note=f"manual hold #{hold['id']} expired")
            except Exception:  # noqa: BLE001
                _logger.exception("Could not record hold expiry on request %s", hold.get("request_id"))
    if expired:
        _invalidate_gate()
        _notify(
            "hold_resolved",
            title=f"{len(expired)} manual hold{'s' if len(expired) != 1 else ''} expired",
            rows=[[h["cust_order_id"], request_store.EXCEPTION_KINDS.get(h["hold_kind"], h["hold_kind"]), h["created_by"], h["expires_at"][:10]] for h in expired][:25],
            columns=["Order", "Hold", "Requested by", "Expired"],
            link=_link("/orders"),
            event_key=f"holds_expired:{_now().date().isoformat()}:{expired[0]['id']}",
        )
    return expired


def active_manual_holds() -> list[dict[str, Any]]:
    return request_store.active_manual_holds(_now())


def queue_summary(days: int = 30) -> dict[str, Any]:
    return request_store.queue_summary(days)
