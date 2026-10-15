"""Dispatch-owned settings, release exceptions and operator administration.

All mutations run inside the same transaction/mutex as checkout. Nothing here
writes to VISUAL or opens the legacy database; migration is a separate CLI step.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment import model as m
from fulfillment.domain import digest, identifier, require

DEFAULT_RULES = {
    "policies": {}, "due_days": 1, "cooldown": 1, "lookahead_days": 10,
    "timezone": "America/Denver", "document_checks_required": False,
    "component_codes": ["FG-COMP", "FG-STOCK", "FG-APPAREL", "COMPONENT", "FG-BASE", "FG-RING", "FG-BARREL", "FG-MUZZLE"],
    "excluded_customers": ["CA MARK"],
}
POLICY_FIELDS = {"account_type", "accumulate", "target_guns", "min_guns", "sweep_weekday", "mix_orders",
                 "release_cadence", "daily_release_time", "max_hold_days", "ship_to_cooldown_days"}


def initial_state():
    return {"version": 1, "configured": False, "rules": deepcopy(DEFAULT_RULES), "controls": [], "imported": None}


def load(conn):
    state = m.get_setting(conn, "administration")
    require(state is not None, "Run the Dispatch database migration first.", "schema", 503)
    return deepcopy(state)


def integer(value, name, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum, f"{name} must be an integer from {minimum} to {maximum}.", "invalid", 400)
    return value


def validate_rules(value):
    require(isinstance(value, dict) and set(value) == set(DEFAULT_RULES), "Supply every supported rule field; unknown fields are rejected.", "invalid", 400)
    rules = deepcopy(value)
    for name, maximum in (("due_days", 365), ("cooldown", 30), ("lookahead_days", 365)):
        integer(rules[name], name, 0, maximum)
    require(type(rules["document_checks_required"]) is bool, "Document-check setting must be true or false.", "invalid", 400)
    try:
        ZoneInfo(rules["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        require(False, "Choose a valid IANA timezone, such as America/Denver.", "invalid", 400)
    for name in ("component_codes", "excluded_customers"):
        require(isinstance(rules[name], list) and len(rules[name]) <= 100, f"{name} must be a list of at most 100 values.", "invalid", 400)
        rules[name] = sorted(set(identifier(v, name) for v in rules[name]))
    policies = rules["policies"]
    require(isinstance(policies, dict) and len(policies) <= 1000, "Customer policies must be an object with at most 1000 entries.", "invalid", 400)
    cleaned = {}
    for customer, policy in policies.items():
        customer = identifier(customer, "Customer")
        require(customer not in cleaned, "Customer policies contain duplicate normalized IDs.", "invalid", 400)
        require(isinstance(policy, dict) and not set(policy) - POLICY_FIELDS, f"Unsupported policy fields for {customer}.", "invalid", 400)
        policy = deepcopy(policy)
        for name in ("accumulate", "mix_orders"):
            if name in policy:
                require(type(policy[name]) is bool, f"{name} must be true or false.", "invalid", 400)
        for name, maximum in (("target_guns", 100000), ("min_guns", 100000), ("max_hold_days", 90), ("ship_to_cooldown_days", 30)):
            if name in policy:
                integer(policy[name], name, 0, maximum)
        if policy.get("sweep_weekday") is not None:
            integer(policy["sweep_weekday"], "Sweep weekday", 0, 6)
        if "account_type" in policy:
            require(policy["account_type"] in ("standard", "major"), "Account type must be standard or major.", "invalid", 400)
        if "release_cadence" in policy:
            require(policy["release_cadence"] in ("completion", "threshold", "daily"), "Unknown release cadence.", "invalid", 400)
        if "daily_release_time" in policy:
            require(isinstance(policy["daily_release_time"], str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", policy["daily_release_time"]), "Daily release time must use HH:MM.", "invalid", 400)
        cleaned[customer] = policy
    rules["policies"] = cleaned
    return rules


def active(control):
    return not control.get("revoked_at") and not control.get("consumed_by") and (
        not control.get("expires_at") or datetime.fromisoformat(control["expires_at"]) > datetime.now(timezone.utc))


def context(conn=None, *, state=None):
    state = load(conn) if state is None else state
    rules = deepcopy(state["rules"])
    controls = [c for c in state["controls"] if active(c)]
    reviews = {}
    for c in state["controls"]:
        if c["kind"] == "document_review" and not c.get("revoked_at"):
            reviews[c["order"]] = c  # expired latest review must not resurrect an older approval
    return {**rules, "configured": state["configured"], "version": state["version"],
            "exceptions": [{**c, "cust_order_id": c["order"]} for c in controls if c["kind"] == "early_release"],
            "holds": [{**c, "cust_order_id": c["order"], "hold_kind": "admin"} for c in controls if c["kind"] == "hold"],
            "document_reviews": reviews}


def order_blocks(ctx, order, document_fingerprint=None):
    reasons = ["manual_hold"] if any(h["cust_order_id"] == order for h in ctx["holds"]) else []
    review = ctx.get("document_reviews", {}).get(order)
    if review and active(review) and review.get("decision") == "blocked":
        reasons.append("document_review_blocked")
    if ctx.get("document_checks_required") and not (
        review and active(review) and review.get("decision") == "pass" and review.get("fingerprint") == document_fingerprint
    ):
        reasons.append("document_review_required")
    return reasons


def expiry(value, *, required=False):
    if not value:
        require(not required, "An expiration is required.", "invalid", 400)
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and datetime.now(timezone.utc) < parsed <= datetime.now(timezone.utc) + timedelta(days=366),
                "Expiration must include a timezone and be within the next 366 days.", "invalid", 400)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError):
        require(False, "Enter a valid expiration date and time.", "invalid", 400)


def snapshot(service, conn):
    state = load(conn)
    users = [dict(r) for r in conn.execute(select(m.users.c.id, m.users.c.name, m.users.c.role, m.users.c.enabled)).mappings()]
    return {**state, "controls": [{**c, "active": active(c)} for c in state["controls"]], "users": users,
            "allocation_worker": m.get_setting(conn, "allocation_worker"),
            "reservations": m.get_setting(conn, "reservations", []),
            "work": service.list_work(conn),
            "audit": [dict(r) for r in conn.execute(select(m.events).where(m.events.c.action.like("admin:%")).order_by(m.events.c.at.desc()).limit(100)).mappings()]}


def execute(service, conn, actor, action, payload):
    # Recheck role under the same lock that protects account and policy mutations.
    user = conn.execute(select(m.users).where(m.users.c.id == actor["id"])).first()
    require(user and user.enabled and user.role == "admin", "An administrator must make this change.", "role", 403)
    state = load(conn)
    require(payload.get("version") == state["version"], "Administration changed. Reload and review the current values.", "revision")
    before = deepcopy(state)
    if action == "preview":
        rules = validate_rules(payload.get("rules"))
        current = service.erp.available(service.list_work(conn), m.get_setting(conn, "reservations", []), context(conn, state={**state, "configured": True}))
        proposed = service.erp.available(service.list_work(conn), m.get_setting(conn, "reservations", []), context(state={**state, "configured": True, "rules": rules}))
        token = str(uuid4())
        m.put_setting(conn, "preview:" + actor["id"], {"token": token, "rules": digest(rules), "version": state["version"],
                      "expires": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()})
        old = {o["order"]: o for o in current["orders"]}
        new = {o["order"]: o for o in proposed["orders"]}
        return {"preview": token, "version": state["version"], "newly_eligible": sorted(set(new) - set(old)),
                "no_longer_eligible": sorted(set(old) - set(new)),
                "changed_prescriptions": sorted(k for k in set(old) & set(new) if old[k]["lines"] != new[k]["lines"]),
                "eligible_count": len(new), "decisions": proposed.get("decisions", []),
                "active_work_unchanged": sum(w["state"] not in ("cancelled", "released") for w in service.list_work(conn))}
    reason = str(payload.get("reason", "")).strip()
    require(3 <= len(reason) <= 1000, "Record a reason (3–1000 characters).", "invalid", 400)
    if action == "reconcile":
        result = service.reconcile_allocations(conn, actor["id"])
        return {"ok": True, "version": load(conn)["version"], "eligible_orders": len(result["orders"])}
    if action == "rules":
        rules = validate_rules(payload.get("rules"))
        preview = m.get_setting(conn, "preview:" + actor["id"], {})
        require(preview.get("token") == payload.get("preview") and preview.get("rules") == digest(rules)
                and preview.get("version") == state["version"] and preview.get("expires", "") > m.now(),
                "Preview these exact settings again before saving.", "preview")
        state.update(rules=rules, configured=True)
        # Preview cannot change reservations. Publishing atomically reconciles only
        # queued allocations; checked-out documents and claims remain unchanged.
        result = service.erp.available(service.list_work(conn), m.get_setting(conn, "reservations", []), context(state=state))
        m.put_setting(conn, "reservations", result.get("reservations", []))
    elif action in ("control", "unassign"):
        order = identifier(payload.get("order"), "Order")
        info = service.erp.inspect_order(order)
        kind = "hold" if action == "unassign" else payload.get("kind")
        require(kind in ("hold", "early_release", "document_review"), "Unknown order control.", "invalid", 400)
        work = [w for w in service.list_work(conn) if w["order"] == order and w["state"] not in ("cancelled", "released")]
        require(not work or (action != "unassign" and kind != "early_release"),
                "This order has active work. Transfer or cancel it and complete putaway before clearing assignments or replanning.")
        control = {"id": str(uuid4()), "kind": kind, "order": order, "reason": reason, "actor": actor["id"], "at": m.now(),
                   "expires_at": expiry(payload.get("expires_at"), required=kind in ("early_release", "document_review")), "revoked_at": None}
        if kind == "document_review":
            require(payload.get("fingerprint") == info["document_fingerprint"], "Order document context changed. Review it again.", "revision")
            require(payload.get("decision") in ("pass", "blocked"), "Record pass or blocked.", "invalid", 400)
            require(bool(str(payload.get("reference", "")).strip()), "Record the document or review reference.", "invalid", 400)
            control.update(fingerprint=info["document_fingerprint"], decision=payload["decision"], reference=str(payload["reference"])[:500])
        state["controls"].append(control)
        if kind == "hold":
            m.put_setting(conn, "reservations", [r for r in m.get_setting(conn, "reservations", []) if r["cust_order_id"] != order])
    elif action == "revoke":
        control = next((c for c in state["controls"] if c["id"] == payload.get("id")), None)
        require(control is not None and not control.get("revoked_at"), "Control is missing or already revoked.")
        control.update(revoked_at=m.now(), revoked_by=actor["id"], revoke_reason=reason)
    elif action == "account":
        user_id = identifier(payload.get("operator"), "Operator")
        role, enabled = payload.get("role"), payload.get("enabled")
        require(role in ("operator", "supervisor", "admin") and type(enabled) is bool, "Choose a valid role and enabled state.", "invalid", 400)
        old = conn.execute(select(m.users).where(m.users.c.id == user_id)).first()
        if old and old.role == "admin" and old.enabled and (not enabled or role != "admin"):
            require(conn.execute(select(m.users.c.id).where(m.users.c.role == "admin").where(m.users.c.enabled.is_(True)).where(m.users.c.id != user_id)).first(),
                    "Keep at least one enabled administrator.")
        if not enabled:
            require(not any(w["operator"] == user_id and w["state"] not in ("cancelled", "released") for w in service.list_work(conn)),
                    "Transfer this operator's active work before disabling the account.")
        name = str(payload.get("name", "")).strip()
        require(1 <= len(name) <= 120, "Enter a display name (maximum 120 characters).", "invalid", 400)
        values = {"name": name, "role": role, "enabled": enabled}
        pin = payload.get("pin")
        require(old or pin, "A new account requires a PIN.", "invalid", 400)
        if pin is not None:
            require(isinstance(pin, str) and pin.isascii() and pin.isdigit() and 6 <= len(pin) <= 32, "PIN must contain 6–32 digits.", "invalid", 400)
            values["pin_hash"] = generate_password_hash(pin)
        if old:
            conn.execute(m.users.update().where(m.users.c.id == user_id).values(**values))
        else:
            conn.execute(m.users.insert().values(id=user_id, **values))
        if old and (pin is not None or old.role != role or old.enabled != enabled):
            conn.execute(m.sessions.delete().where(m.sessions.c.user_id == user_id))
        # Audit metadata deliberately excludes PINs and password hashes.
        from fulfillment.service import event
        event(conn, actor["id"], "admin:account-change", {"reason": reason, "operator": user_id,
              "before": {k: getattr(old, k) for k in ("name", "role", "enabled")} if old else None,
              "after": {"name": name, "role": role, "enabled": enabled}, "pin_changed": pin is not None})
    else:
        require(False, "Unknown administration command.", "not_found", 404)
    state["version"] += 1
    m.put_setting(conn, "administration", state)
    from fulfillment.service import event
    event(conn, actor["id"], "admin:change", {"operation": action, "reason": reason, "before": before, "after": state})
    return {"ok": True, "version": state["version"]}
