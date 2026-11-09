"""Durable planning attempts and operator explanations; previews never commit."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fulfillment import admin, inventory, inventory_sync, model as m, order_book
from fulfillment.domain import Conflict, quantity, require


def enqueue(conn, actor, action, resource=None):
    actor_id = actor.get("id") if isinstance(actor, dict) else str(actor)
    identity = m.digest([action, resource])
    current = next((t for t in inventory.rows(conn, m.planning_triggers) if t.get("coalesce_key") == identity and t["status"] == "pending"), None)
    if current:
        current.update(last_at=m.now(), occurrences=current.get("occurrences", 1) + 1)
        # Retain every actor/time even when repeated triggers share one work item.
        current.setdefault("references", []).append(dict(at=m.now(), actor=actor_id, action=action, resource=resource))
        return inventory.save(conn, m.planning_triggers, current)
    record = dict(id=str(uuid4()), coalesce_key=identity, status="pending", action=action, resource=resource,
                  actor=actor_id, at=m.now(), last_at=m.now(), occurrences=1,
                  references=[dict(at=m.now(), actor=actor_id, action=action, resource=resource)])
    return inventory.save(conn, m.planning_triggers, record)


def _inputs(service, conn):
    return dict(policy=admin.context(conn), orders=inventory.rows(conn, m.order_book),
        order_source=order_book.state(conn), stock=inventory.rows(conn, m.inventory_stock),
        stock_version=inventory_sync.stock_version(conn), inventory_source=inventory_sync.status(conn),
        allocations=inventory.rows(conn, m.inventory_allocations), locks=inventory.rows(conn, m.inventory_locks),
        work=service.list_work(conn), reservations=m.get_setting(conn, "reservations", []))


def _decision_stamp(conn):
    return m.digest([admin.context(conn), [(r["id"], r["facts_digest"], r.get("conflicts")) for r in sorted(inventory.rows(conn, m.order_book), key=lambda r: r["id"])],
        inventory_sync.stock_version(conn), sorted(inventory.rows(conn, m.inventory_allocations), key=lambda r: r["id"]),
        sorted(inventory.rows(conn, m.inventory_locks), key=lambda r: r["id"])])


def start(conn, actor):
    actor_id = actor.get("id") if isinstance(actor, dict) else str(actor)
    # A process that crashed after committing its started marker leaves evidence.
    # A serialized worker owns the database lock while it finishes its attempt;
    # abandoned markers are recovered after the lease, not from another live start.
    for previous in inventory.rows(conn, m.planning_runs):
        if previous["status"] == "running" and datetime.fromisoformat(previous["started_at"]) < datetime.now(timezone.utc) - timedelta(minutes=10):
            previous.update(status="interrupted", finished_at=m.now(), error={"code": "interrupted", "message": "Planner did not finish; pending triggers and existing commitments were retained."})
            inventory.save(conn, m.planning_runs, previous)
    record = dict(id=str(uuid4()), actor=actor_id, started_at=m.now(), finished_at=None, status="running", triggers=[],
                  decisions=[], allocation_changes=[], error=None)
    return inventory.save(conn, m.planning_runs, record)


def run(service, conn, actor, run_id=None, refresh=True):
    from fulfillment.service import event
    record = inventory.get(conn, m.planning_runs, run_id) if run_id else start(conn, actor)
    require(record["status"] == "running", "Planning attempt has already finished.", "closed")
    actor_id = actor.get("id") if isinstance(actor, dict) else str(actor)
    result = {"orders": [], "reservations": m.get_setting(conn, "reservations", []), "decisions": [], "as_of": m.now()}
    try:
        if refresh:
            try:
                with conn.begin_nested():
                    order_book.refresh(service, conn, actor)
            except Exception as exc:
                order_book.fail(conn, exc)
                raise
        record["triggers"] = [t for t in inventory.rows(conn, m.planning_triggers) if t["status"] == "pending"]
        record["inputs"] = _inputs(service, conn)
        before = {a["id"]: a for a in record["inputs"]["allocations"]}
        with conn.begin_nested():
            result = service.orders(conn)
            if inventory.config(conn)["enabled"]:
                inventory.accumulate(conn, actor_id, result.get("inventory_accumulations", []))
            previous = m.get_setting(conn, "reservations", [])
            updated = result.get("reservations", [])
            if m.digest(previous) != m.digest(updated):
                m.put_setting(conn, "reservations", updated)
                state = admin.load(conn)
                state["version"] += 1
                m.put_setting(conn, "administration", state)
                event(conn, actor_id, "admin:allocation-refresh", {"before": previous, "after": updated, "planning_run_id": record["id"]})
            after = {a["id"]: a for a in inventory.rows(conn, m.inventory_allocations)}
            for identity in sorted(set(before) | set(after)):
                if before.get(identity) != after.get(identity):
                    if identity in after:
                        after[identity]["planning_run_id"] = record["id"]
                        inventory.save(conn, m.inventory_allocations, after[identity])
                    record["allocation_changes"].append(dict(id=identity, before=before.get(identity), after=after.get(identity)))
            record.update(status="succeeded", finished_at=m.now(), decisions=deepcopy(result["decisions"]),
                eligible_orders=[o["order"] for o in result["orders"]], result=deepcopy(result), decision_stamp=_decision_stamp(conn))
            for trigger in record["triggers"]:
                trigger.update(status="consumed", run_id=record["id"], consumed_at=m.now())
                inventory.save(conn, m.planning_triggers, trigger)
            record["board"] = _board(service, conn, record)
            m.put_setting(conn, "allocation_worker", dict(at=m.now(), run_id=record["id"], status="succeeded",
                eligible_orders=len(result["orders"]), reservations=len(updated)))
    except Exception as exc:
        record.update(status="failed", finished_at=m.now(), allocation_changes=[], error=dict(
            code=getattr(exc, "code", "planning_failed"),
            message=exc.message if isinstance(exc, Conflict) else "Planning failed. Previous commitments were retained; check service diagnostics."))
        record.setdefault("inputs", _inputs(service, conn))
        record["triggers"] = [t for t in inventory.rows(conn, m.planning_triggers) if t["status"] == "pending"]
        m.put_setting(conn, "allocation_worker", dict(at=m.now(), run_id=record["id"], status="failed", error=record["error"]))
        result = {"orders": [], "decisions": [], "reservations": m.get_setting(conn, "reservations", []), "as_of": m.now()}
    inventory.save(conn, m.planning_runs, record)
    event(conn, actor_id, "planning:" + record["status"], dict(run_id=record["id"], error=record["error"], allocation_changes=record["allocation_changes"]))
    return {**result, "planning_run": {k: record.get(k) for k in ("id", "status", "started_at", "finished_at", "error")}}


def execute(service, actor="dispatch-worker"):
    """Two committed phases make a killed worker distinguishable from no run."""
    with service.db.transaction() as conn:
        enqueue(conn, actor, "planning:scheduled")
        attempt = start(conn, actor)
    with service.db.transaction() as conn:
        return run(service, conn, actor, attempt["id"])


def history(conn, run_id=None):
    if run_id:
        return inventory.get(conn, m.planning_runs, run_id)
    records = sorted(inventory.rows(conn, m.planning_runs), key=lambda r: r["started_at"], reverse=True)
    return {"runs": [{k: r.get(k) for k in ("id", "actor", "started_at", "finished_at", "status", "error", "eligible_orders", "triggers", "allocation_changes")} for r in records[:200]],
            "total": len(records), "pending_triggers": [t for t in inventory.rows(conn, m.planning_triggers) if t["status"] == "pending"]}


def _board(service, conn, latest=None):
    decisions = {d["order_id"]: d for d in (latest or {}).get("decisions", [])}
    source = order_book.state(conn)
    context = admin.context(conn)
    works = service.list_work(conn)
    allocations = inventory.active_allocations(conn)
    approvals = inventory.rows(conn, m.approval_requests)
    through = datetime.now(ZoneInfo(context["timezone"])).date() + timedelta(days=context["lookahead_days"])
    result = []
    for record in sorted(inventory.rows(conn, m.order_book), key=lambda r: r["order"]["order"]):
        order = record["order"]
        decision = deepcopy(decisions.get(order["order"]))
        status, label, owner, action = "unevaluated", "Not evaluated yet", "Shipping supervisor", "Run planning."
        active = [w for w in works if w["order"] == order["order"] and w["state"] not in ("cancelled", "released")]
        commitments = [a for a in allocations if a["customer"] == order["customer_id"] and a.get("order") in (None, "", order["order"]) and any(l["part"] == a["part"] and l["uom"] == a["uom"] for l in order["lines"])]
        pending = [a["id"] for a in approvals if a.get("status") == "submitted" and (a.get("order") == order["order"] or a.get("payload", {}).get("order") == order["order"] or a.get("terms", {}).get("order") == order["order"] or (not a.get("terms", {}).get("order") and a.get("terms", {}).get("customer") == order["customer_id"] and any(l["part"] == a.get("terms", {}).get("part") and l["uom"] == a.get("terms", {}).get("uom") for l in order["lines"])))]
        if decision:
            mapping = {"RELEASE": "ready", "ACCUMULATING": "accumulating", "BLOCKED": "order_block", "HOLD": "waiting_stock"}
            status, label = mapping.get(decision["decision"], "waiting_stock"), decision.get("label", decision["decision"])
            owner, action = ("Shipping", "Checkout rechecks the order and locks exact stock.") if status == "ready" else ("Inside Sales / Shipping supervisor", "Review the recorded rule and order-line reasons.")
        if not decision and record["candidates"] and all(not r.get("LINE_AVAILABLE", 1) for r in record["candidates"]):
            status, label, owner, action = "order_block", "Source order lines are unavailable or cancelled", "Inside Sales", "Review the line status in VISUAL and refresh intake."
        if not decision and record["candidates"]:
            dates = [r.get("PROMISE_DEL_DATE") or r.get("PROMISE_SHIP_DATE") or r.get("DESIRED_SHIP_DATE") for r in record["candidates"]]
            if dates and all(d and datetime.fromisoformat(str(d)).date() > through for d in dates):
                status, label, owner, action = "outside_horizon", "Outside planning horizon", "Inside Sales", "Wait for the planning date or review the published horizon."
        if pending and status != "ready":
            status, label, owner, action = "pending_approval", "An allocation request awaits independent review", "Approver", "Review the submitted request; pending requests do not reserve stock."
        if any(a.get("source") == "work_order" for a in commitments) and status in ("waiting_stock", "unevaluated"):
            status, label, owner, action = "future_supply", "Waiting for promised work-order supply", "Production / Receiving", "Receive the exact work order; future promises are not physical stock."
        if any(a.get("hold") for a in commitments) and status != "ready":
            status, label, owner, action = "customer_hold", "Customer commitment is held", "Inside Sales", "Review and authorize removal of the allocation hold."
        if record.get("conflicts"):
            status, label, owner, action = "commitment_conflict", "Order changed after commitment", "Inside Sales", "Resolve the retained promise conflict; stock has not been reassigned."
        if not source["available"]:
            status, label, owner, action = "source_unavailable", "Order source is stale or unavailable", "Integration owner", "Restore the source and run planning; existing commitments remain protected."
        elif inventory_sync.status(conn).get("fresh") is False:
            status, label, owner, action = "source_unavailable", "Inventory source is stale or unavailable", "Integration owner", "Restore a complete fresh inventory capture and run planning."
        if order["status"] in order_book.CLOSED:
            status, label, owner, action = "closed", "Source order closed, cancelled or missing", "Inside Sales", "Review remaining commitments; no stock is automatically freed."
        if active:
            status, label, owner, action = "in_progress", "Fulfillment is in progress", active[0]["operator"], "Continue assigned work; picked stock remains on hand until release."
        released = [w for w in works if w["order"] == order["order"] and w["state"] == "released"]
        if released and not active and not decision and order["status"] not in order_book.CLOSED:
            status, label, owner, action = "released", "Released work awaits confirmed remaining demand", "Shipping supervisor", "Review released shipment and source cumulative shipped quantities."
        result.append(dict(id=record["id"], order=order["order"], site=order["site"], customer=order["customer"], customer_id=order["customer_id"],
            status=status, label=label, owner=owner, next_action=action, decision=decision, lines=order["lines"],
            commitments=commitments, conflicts=record.get("conflicts", []), pending_approvals=pending,
            source_revision=record["source_revision"], observed_at=record["observed_at"], decision_at=(latest or {}).get("finished_at"),
            planning_run_id=(latest or {}).get("id"), work=[dict(id=w["id"], state=w["state"], operator=w["operator"]) for w in active + released]))
    return result


def board(service, conn):
    runs = sorted([r for r in inventory.rows(conn, m.planning_runs) if r["status"] == "succeeded"], key=lambda r: r["started_at"], reverse=True)
    latest = runs[0] if runs else None
    result = _board(service, conn, latest)
    changed = bool(latest and latest.get("decision_stamp") != _decision_stamp(conn))
    if changed:
        for row in result:
            if row["status"] == "ready":
                row.update(status="needs_replanning", label="Inputs changed since the recorded decision", owner="Shipping supervisor", next_action="Run planning to refresh the ready suggestion.")
    return dict(orders=result, source=order_book.state(conn), latest_run={k: latest.get(k) for k in ("id", "started_at", "finished_at", "status")} if latest else None,
        needs_replanning=changed, pending_triggers=[t for t in inventory.rows(conn, m.planning_triggers) if t["status"] == "pending"], as_of=m.now())


def provenance(conn, order):
    """Bind checkout to its fresh policy decision; only link a matching prior run."""
    records = sorted([r for r in inventory.rows(conn, m.planning_runs) if r["status"] == "succeeded"], key=lambda r: r["started_at"], reverse=True)
    latest = records[0] if records else None
    matches = latest and latest.get("decision_stamp") == _decision_stamp(conn) and any(d.get("order_id") == order["order"] and d == order.get("policy") for d in latest["decisions"])
    return dict(at=m.now(), planning_run_id=latest["id"] if matches else None, kind="checkout_recheck",
                policy=deepcopy(order.get("policy")), policy_version=order.get("policy_version"), order_revision=order.get("order_revision"),
                order_observed_at=order.get("order_observed_at"), input_fingerprint=_decision_stamp(conn),
                stock_version=inventory_sync.stock_version(conn), inventory_source=inventory_sync.status(conn), order_source=order_book.state(conn))
