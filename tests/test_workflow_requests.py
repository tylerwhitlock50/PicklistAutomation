"""Independent review, exact sales binding and atomic commitment outcomes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from fulfillment import inventory as inv, model as m, workflow_requests as workflow
from test_inventory import ADMIN, SUP, OP, system, setup, receipt, allocate, ok, state


SALES = {"id": "INSIDE-SALES", "role": "sales"}
OTHER = {"id": "OTHER-SUPERVISOR", "role": "supervisor"}


@pytest.fixture
def workflow_system(system):
    db, erp, service = system
    setup(service)
    with db.transaction() as conn:
        for actor in (ADMIN, SUP, OP, SALES, OTHER):
            conn.execute(m.users.insert().values(id=actor["id"], name=actor["id"], role=actor["role"], enabled=True, pin_hash="test-account"))
    return db, erp, service


def command(service, action, payload, actor=SALES, request_id=None):
    return service.command(actor, "workflow:" + action, payload, request_id or str(uuid4()))


def accepted(service, action, payload, actor=SALES, request_id=None):
    result, status = command(service, action, payload, actor, request_id)
    assert status == 200, result
    return result


def change(record, **extra):
    return dict(id=record["id"], version=record["version"], reason="Reviewed customer request", **extra)


def submit(service, terms=None, kind="allocation", actor=SALES, **extra):
    request = accepted(service, "request-create", dict(kind=kind, terms=terms or allocate(), reason="Customer request", **extra), actor)
    return accepted(service, "request-submit", change(request), actor)


def workflow_state(db):
    with db.engine.connect() as conn:
        return workflow.snapshot(conn)


def draft_terms(erp, **extra):
    return dict(customer="DEMO-SUPPLY", site="DEMO", customer_reference="PO-123", need_by="2026-12-01",
        destination=deepcopy(erp.orders[0]["destination"]), lines=[dict(id="REQUEST-1", part="ACCESSORY-B", uom="EA", quantity="3")],
        reason="Requested by customer", **extra)


def sale(service, erp, **extra):
    draft = accepted(service, "sales-create", draft_terms(erp, **extra))
    return accepted(service, "sales-submit", change(draft))


def test_pending_request_is_not_a_reservation_and_approval_is_durable(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt(amount="5"))
    request = submit(service)
    assert request["status"] == "submitted"
    assert state(db)["balances"][0]["committed"] == "0"
    assert command(service, "request-approve", change(request), SALES)[1] == 403
    approved = accepted(service, "request-approve", change(request), SUP)
    assert approved["status"] == "approved"
    assert state(db)["balances"][0]["committed"] == "3"
    allocation = state(db)["allocations"][0]
    assert allocation["approval_request_id"] == request["id"]
    assert allocation["hold"] is True
    with db.engine.connect() as conn:
        persisted = inv.get(conn, m.approval_requests, request["id"])
    assert [entry["action"] for entry in persisted["history"]] == ["created", "submitted", "approved"]
    assert persisted["history"][1]["detail"]["terms"] == request["terms"]
    assert workflow_state(db)["approval_requests"][0]["progress"]["quantity"] == "3"


def test_self_approval_disabled_account_and_machine_actor_are_denied(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service, actor=SUP)
    error, status = command(service, "request-approve", change(request), SUP)
    assert (status, error["error"]) == (403, "self_approval")
    with db.transaction() as conn:
        conn.execute(m.users.update().where(m.users.c.id == OTHER["id"]).values(enabled=False))
    assert command(service, "request-approve", change(request), OTHER)[1] == 403
    assert command(service, "request-create", dict(kind="allocation", terms=allocate(), reason="Machine"), {"id": "connector", "role": "inventory_integration"})[1] == 403
    assert command(service, "request-create", dict(kind="allocation", terms=allocate(), reason="Spoofed admin"), {"id": SALES["id"], "role": "admin"})[1] == 403
    assert state(db)["allocations"] == []


def test_same_approval_retries_and_concurrent_review_cannot_double_allocate(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service)
    approval_key = str(uuid4())
    first = accepted(service, "request-approve", change(request), SUP, approval_key)
    assert accepted(service, "request-approve", change(request), SUP, approval_key) == first
    assert command(service, "request-approve", change(request), OTHER)[1] == 409
    request2 = submit(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda actor: command(service, "request-approve", change(request2), actor), [SUP, OTHER]))
    assert sorted(status for _, status in results) == [200, 409]
    assert len(state(db)["allocations"]) == 2
    assert state(db)["balances"][0]["committed"] == "6"


def test_competing_approvals_preserve_shortage_request_without_partial_writes(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt(amount="5"))
    first, second = submit(service, allocate(amount="4")), submit(service, allocate(amount="4"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda request: command(service, "request-approve", change(request), SUP), [first, second]))
    assert sorted(status for _, status in results) == [200, 409]
    requests = workflow_state(db)["approval_requests"]
    assert sorted(r["status"] for r in requests) == ["approved", "submitted"]
    assert state(db)["balances"][0]["committed"] == "4"
    assert len(state(db)["allocations"]) == 1


def test_revised_request_needs_fresh_submission_and_old_terms_are_retained(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service)
    revised = accepted(service, "request-update", {**change(request), "terms": allocate(amount="5")})
    assert revised["status"] == "draft"
    assert revised["history"][1]["detail"]["terms"]["quantity"] == "3"
    assert command(service, "request-approve", change(request), SUP)[1] == 409
    assert command(service, "request-approve", change(revised), SUP)[1] == 409
    submitted = accepted(service, "request-submit", change(revised))
    accepted(service, "request-approve", change(submitted), SUP)
    assert state(db)["balances"][0]["committed"] == "5"


def test_rejected_and_cancelled_requests_never_allocate(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    rejected = accepted(service, "request-reject", change(submit(service)), SUP)
    cancelled = accepted(service, "request-cancel", change(submit(service)))
    assert rejected["status"] == "rejected" and cancelled["status"] == "cancelled"
    assert state(db)["allocations"] == []
    assert command(service, "request-approve", change(rejected), OTHER)[1] == 409
    assert command(service, "request-approve", change(cancelled), SUP)[1] == 409


@pytest.mark.parametrize("mutation", ["cancelled", "line_cancelled", "customer", "site", "quantity"])
def test_approval_rechecks_live_order_facts_and_capacity(workflow_system, mutation):
    db, erp, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service, allocate(order="SO-DEMO-100", order_line="2"))
    order = erp.orders[0]
    if mutation == "cancelled":
        order["status"] = "C"
    elif mutation == "line_cancelled":
        order["lines"][1]["status"] = "C"
    elif mutation == "customer":
        order["customer_id"] = "OTHER"
    elif mutation == "site":
        order["site"] = "OTHER"
    else:
        order["lines"][1]["quantity"] = "2"
    assert command(service, "request-approve", change(request), SUP)[1] == 409
    assert state(db)["allocations"] == []
    assert workflow_state(db)["approval_requests"][0]["status"] == "submitted"


def test_order_bound_requests_cannot_overpromise_same_open_line(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt(amount="20"))
    first = submit(service, allocate(amount="4", order="SO-DEMO-100", order_line="2"))
    accepted(service, "request-approve", change(first), SUP)
    second = submit(service, allocate(amount="4", order="SO-DEMO-100", order_line="2"))
    error, status = command(service, "request-approve", change(second), SUP)
    assert (status, error["error"]) == (409, "order_quantity")
    assert state(db)["balances"][0]["committed"] == "4"


def test_approved_future_promise_promotes_on_receipt_without_releasing_hold(workflow_system):
    db, _, service = workflow_system
    supply = ok(service, "supply", dict(work_order="WO-TEST", part="ACCESSORY-B", uom="EA", quantity="5", serialized=False, reason="Production schedule"))
    terms = {**allocate(), "source": "work_order", "source_id": supply["id"]}
    approved = accepted(service, "request-approve", change(submit(service, terms)), SUP)
    ok(service, "receive", receipt(amount="3", work_order_id=supply["id"]))
    progress = workflow_state(db)["approval_requests"][0]["progress"]
    assert progress["status"] == "fulfilled"
    assert progress["children"][0]["hold"] is True
    child = next(a for a in state(db)["allocations"] if a["source"] == "stock")
    assert child["approval_request_id"] == approved["id"]


def test_future_supply_reduction_between_submission_and_approval_is_rechecked(workflow_system):
    db, _, service = workflow_system
    supply = ok(service, "supply", dict(work_order="WO-TEST", part="ACCESSORY-B", uom="EA", quantity="5", serialized=False, reason="Production schedule"))
    request = submit(service, {**allocate(), "source": "work_order", "source_id": supply["id"]})
    ok(service, "supply", {**supply, "quantity": "1", "reason": "Revised supply"})
    assert command(service, "request-approve", change(request), SUP)[1] == 409
    assert state(db)["allocations"] == []


def test_transfer_and_hold_change_are_version_guarded_and_reviewed(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    original = ok(service, "allocate", allocate(amount="5"))
    transfer = submit(service, dict(id=original["id"], version=1, quantity="2", customer="CUSTOMER-B", hold=True, reason="Reassign requested units"), "transfer")
    approved = accepted(service, "request-approve", change(transfer), SUP)
    successor = next(a for a in state(db)["allocations"] if a["id"] == approved["allocation_id"])
    assert successor["customer"] == "CUSTOMER-B" and successor["quantity"] == "2"
    release = submit(service, dict(id=successor["id"], version=successor["version"], status="released", reason="Customer cancelled request"), "allocation_change")
    accepted(service, "request-approve", change(release), SUP)
    assert state(db)["balances"][0]["committed"] == "3"
    original = next(a for a in state(db)["allocations"] if a["id"] == original["id"])
    stale = submit(service, dict(id=original["id"], version=original["version"], hold=False, reason="Release hold"), "allocation_change")
    ok(service, "allocation-update", dict(id=original["id"], version=original["version"], hold=True, reason="Independent review"))
    assert command(service, "request-approve", change(stale), SUP)[1] == 409


def test_adjustment_requires_unchanged_count_basis_and_preserves_protected_stock(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service, dict(part="ACCESSORY-B", uom="EA", quantity="-2", reason="Verified count correction"), "adjustment")
    ok(service, "receive", receipt(amount="1"))
    assert command(service, "request-approve", change(request), SUP)[1] == 409
    assert state(db)["balances"][0]["quantity"] == "11"
    request2 = submit(service, dict(part="ACCESSORY-B", uom="EA", quantity="-2", reason="Verified count correction"), "adjustment")
    accepted(service, "request-approve", change(request2), SUP)
    assert state(db)["balances"][0]["quantity"] == "9"
    ok(service, "allocate", allocate(amount="8"))
    request3 = submit(service, dict(part="ACCESSORY-B", uom="EA", quantity="-2", reason="Count below commitments"), "adjustment")
    assert command(service, "request-approve", change(request3), SUP)[1] == 409
    assert state(db)["balances"][0]["quantity"] == "9"


def test_sales_request_draft_cannot_ship_and_exact_binding_is_recorded(workflow_system):
    db, erp, service = workflow_system
    request = sale(service, erp)
    assert request["status"] == "awaiting_visual_order"
    assert service.command(OP, "checkout", dict(order=request["id"], cart="SALES-DRAFT"), str(uuid4()))[1] == 409
    binding = change(request, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")])
    assert command(service, "sales-bind", binding, SALES)[1] == 403
    bound = accepted(service, "sales-bind", binding, SUP)
    assert bound["status"] == "bound" and bound["order"] == "SO-DEMO-100"
    assert bound["line_bindings"][0]["quantity"] == "3"
    assert command(service, "sales-bind", binding, SUP)[1] == 409
    assert state(db)["allocations"] == []


@pytest.mark.parametrize("mutation", ["cancelled", "line", "part", "uom", "quantity", "customer", "destination"])
def test_sales_binding_rejects_mismatched_erp_order(workflow_system, mutation):
    db, erp, service = workflow_system
    request = sale(service, erp)
    order = erp.orders[0]
    if mutation == "cancelled":
        order["status"] = "C"
    elif mutation == "line":
        order["lines"][1]["line_available"] = False
    elif mutation in ("part", "uom"):
        order["lines"][1][mutation] = "OTHER"
    elif mutation == "quantity":
        order["lines"][1]["quantity"] = "2"
    elif mutation == "customer":
        order["customer_id"] = "OTHER"
    else:
        order["destination"]["postal"] = "00000"
    assert command(service, "sales-bind", change(request, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)[1] == 409
    assert workflow_state(db)["sales_requests"][0]["status"] == "awaiting_visual_order"


def test_duplicate_customer_references_require_independent_review(workflow_system):
    db, erp, service = workflow_system
    first, second = sale(service, erp), sale(service, erp)
    assert second["duplicate_references"] == [first["id"]]
    binding = change(second, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")])
    assert command(service, "sales-bind", binding, SUP)[1] == 409
    bound = accepted(service, "sales-bind", {**binding, "duplicate_reference_review": "Two separately requested deliveries; reviewed exact open quantity"}, SUP)
    assert bound["duplicate_reference_review"]
    with db.transaction() as conn:
        conn.execute(m.users.update().where(m.users.c.id == SALES["id"]).values(role="supervisor"))
    actor = {**SALES, "role": "supervisor"}
    assert command(service, "sales-bind", {**change(first, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), "duplicate_reference_review": "Self review"}, actor)[1] == 403


def test_linked_sales_commitment_needs_approval_and_cannot_exceed_requested_quantity(workflow_system):
    db, erp, service = workflow_system
    ok(service, "receive", receipt())
    sales_request = sale(service, erp)
    request = submit(service, allocate(amount="2"), sales_request_id=sales_request["id"])
    assert state(db)["balances"][0]["committed"] == "0"
    approved = accepted(service, "request-approve", change(request), SUP)
    second = submit(service, allocate(amount="2"), sales_request_id=sales_request["id"])
    assert command(service, "request-approve", change(second), SUP)[1] == 409
    assert command(service, "sales-cancel", change(sales_request))[1] == 409
    allocation = next(a for a in state(db)["allocations"] if a["id"] == approved["allocation_id"])
    release = submit(service, dict(id=allocation["id"], version=allocation["version"], status="released", reason="Customer request withdrawn"), "allocation_change")
    accepted(service, "request-approve", change(release), SUP)
    cancelled = accepted(service, "sales-cancel", change(sales_request))
    assert cancelled["status"] == "cancelled"
    assert command(service, "request-approve", change(second), SUP)[1] == 409


def test_early_release_requires_separate_admin_and_exact_source_context(workflow_system):
    db, erp, service = workflow_system
    terms = dict(order="SO-DEMO-100", expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), reason="Expedite requested")
    request = submit(service, terms, "early_release")
    assert command(service, "request-approve", change(request), SUP)[1] == 403
    erp.orders[0]["destination"]["postal"] = "00000"
    assert command(service, "request-approve", change(request), ADMIN)[1] == 409
    revised = accepted(service, "request-update", {**change(request), "terms": terms})
    submitted = accepted(service, "request-submit", change(revised))
    approved = accepted(service, "request-approve", change(submitted), ADMIN)
    assert approved["control_id"]
    with db.engine.connect() as conn:
        controls = m.get_setting(conn, "administration")["controls"]
    assert any(c["id"] == approved["control_id"] and c["kind"] == "early_release" for c in controls)


def test_approval_retries_after_source_recovery_without_changing_reviewed_terms(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    supply = ok(service, "supply", dict(work_order="WO-STALE", part="ACCESSORY-B", uom="EA", quantity="5", serialized=False, reason="Production schedule"))
    request = submit(service, {**allocate(), "source": "work_order", "source_id": supply["id"]})
    with db.transaction() as conn:
        m.put_setting(conn, "inventory_sync", dict(stream_id="VISUAL", status="matched", sequence=1,
            observed_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()))
    request_key = str(uuid4())
    error, status = command(service, "request-approve", change(request), SUP, request_key)
    assert (status, error["error"]) == (503, "source_unavailable")
    assert workflow_state(db)["approval_requests"][0]["status"] == "submitted"
    with db.transaction() as conn:
        m.put_setting(conn, "inventory_sync", dict(stream_id="VISUAL", status="matched", sequence=2, observed_at=m.now()))
    approved = accepted(service, "request-approve", change(request), SUP, request_key)
    assert approved["status"] == "approved"
    assert len(state(db)["allocations"]) == 1


def test_sales_duplicate_check_includes_existing_mirrored_orders(workflow_system):
    db, erp, service = workflow_system
    with db.transaction() as conn:
        inv.save(conn, m.order_book, dict(id="existing-order", order=dict(site="DEMO", order="OTHER-ORDER", customer_id="DEMO-SUPPLY", customer_reference="po-123")))
    request = sale(service, erp)
    assert request["duplicate_references"] == ["visual:OTHER-ORDER"]
    binding = change(request, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")])
    assert command(service, "sales-bind", binding, SUP)[1] == 409
    bound = accepted(service, "sales-bind", {**binding, "duplicate_reference_review": "Other PO is an intentional separate release"}, SUP)
    assert bound["status"] == "bound"


def test_sales_binding_checks_customer_po_reference(workflow_system):
    _, erp, service = workflow_system
    request = sale(service, erp)
    erp.orders[0]["customer_reference"] = "SOME-OTHER-PO"
    error, status = command(service, "sales-bind", change(request, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)
    assert (status, error["error"]) == (409, "customer_reference")


def test_unrecognized_request_terms_and_invalid_kind_fail_without_writes(workflow_system):
    db, _, service = workflow_system
    assert command(service, "request-create", dict(kind=[], terms={}, reason="Invalid kind"))[1] == 400
    assert command(service, "request-create", dict(kind="allocation", terms={**allocate(), "approved_by": SUP["id"]}, reason="Injected approval"))[1] == 400
    assert workflow_state(db)["approval_requests"] == []


def test_transfer_and_hold_release_cannot_certify_short_future_supply(workflow_system):
    db, _, service = workflow_system
    supply = ok(service, "supply", dict(work_order="WO-SHORT", part="ACCESSORY-B", uom="EA", quantity="5", serialized=False, reason="Production schedule"))
    allocation = ok(service, "allocate", {**allocate(), "source": "work_order", "source_id": supply["id"]})
    transfer = submit(service, dict(id=allocation["id"], version=1, quantity="2", customer="OTHER", reason="Reassign"), "transfer")
    release = submit(service, dict(id=allocation["id"], version=1, hold=False, reason="Release hold"), "allocation_change")
    ok(service, "supply", {**supply, "quantity": "1", "reason": "Source shortfall"})
    assert command(service, "request-approve", change(transfer), SUP)[1] == 409
    assert command(service, "request-approve", change(release), SUP)[1] == 409
    assert state(db)["allocations"][0]["hold"] is True
    cancellation = submit(service, dict(id=allocation["id"], version=1, status="released", reason="Release uncovered promise"), "allocation_change")
    assert accepted(service, "request-approve", change(cancellation), SUP)["status"] == "approved"


def test_quarantined_committed_stock_cannot_transfer_to_another_customer(workflow_system):
    db, _, service = workflow_system
    ok(service, "receive", receipt())
    allocation = ok(service, "allocate", allocate())
    transfer = submit(service, dict(id=allocation["id"], version=1, quantity="2", customer="OTHER", reason="Reassign"), "transfer")
    with db.transaction() as conn:
        m.put_setting(conn, "inventory_sync_blocked", [allocation["items"][0]["stock_id"]])
    assert command(service, "request-approve", change(transfer), SUP)[1] == 409
    assert state(db)["allocations"][0]["customer"] == "DEMO-SUPPLY"


def test_order_source_failure_is_audited_and_same_approval_can_retry(workflow_system, monkeypatch):
    db, erp, service = workflow_system
    ok(service, "receive", receipt())
    request = submit(service, allocate(order="SO-DEMO-100", order_line="2"))
    original = erp.inspect_order
    def unavailable(_identity):
        raise RuntimeError("Source offline")
    monkeypatch.setattr(erp, "inspect_order", unavailable)
    request_id = str(uuid4())
    result, status = command(service, "request-approve", change(request), SUP, request_id)
    assert (status, result["error"]) == (503, "order_source")
    with db.engine.connect() as conn:
        rejected = conn.execute(select(m.events.c.detail).where(m.events.c.action == "rejected:workflow:request-approve")).scalar()
    assert rejected["error"] == "order_source"
    assert state(db)["allocations"] == []
    monkeypatch.setattr(erp, "inspect_order", original)
    assert accepted(service, "request-approve", change(request), SUP, request_id)["status"] == "approved"


def test_sales_bindings_track_cumulative_shipments_without_double_binding_open_demand(workflow_system):
    _, erp, service = workflow_system
    first = sale(service, erp)
    bound = accepted(service, "sales-bind", change(first, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)
    assert bound["line_bindings"][0]["shipment_end"] == "3"
    erp.orders[0]["lines"][1].update(quantity="3", shipped="3", ordered="6")
    second = accepted(service, "sales-create", {**draft_terms(erp), "customer_reference": "PO-SECOND"})
    second = accepted(service, "sales-submit", change(second))
    bound2 = accepted(service, "sales-bind", change(second, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)
    assert bound2["line_bindings"][0]["shipment_start"] == "3"
    third = accepted(service, "sales-create", {**draft_terms(erp), "customer_reference": "PO-THIRD"})
    third = accepted(service, "sales-submit", change(third))
    assert command(service, "sales-bind", change(third, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)[1] == 409


def test_sales_binding_quantity_reduction_is_not_mistaken_for_shipment(workflow_system):
    _, erp, service = workflow_system
    first = sale(service, erp)
    accepted(service, "sales-bind", change(first, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)
    erp.orders[0]["lines"][1].update(quantity="3", shipped="0", ordered="3")
    second = accepted(service, "sales-create", {**draft_terms(erp), "customer_reference": "PO-SECOND"})
    second = accepted(service, "sales-submit", change(second))
    assert command(service, "sales-bind", change(second, order="SO-DEMO-100", line_bindings=[dict(request_line="REQUEST-1", order_line="2")]), SUP)[1] == 409


def test_fulfilled_sales_commitment_cannot_be_approved_again(workflow_system):
    db, erp, service = workflow_system
    ok(service, "receive", receipt())
    sales_request = sale(service, erp)
    request = submit(service, allocate(), sales_request_id=sales_request["id"])
    approved = accepted(service, "request-approve", change(request), SUP)
    with db.transaction() as conn:
        allocation = inv.get(conn, m.inventory_allocations, approved["allocation_id"])
        allocation.update(fulfilled="3", status="fulfilled", items=[])
        inv.save(conn, m.inventory_allocations, allocation)
    second = submit(service, allocate(amount="1"), sales_request_id=sales_request["id"])
    error, status = command(service, "request-approve", change(second), SUP)
    assert (status, error["error"]) == (409, "sales_request")


def test_future_promotion_transfer_and_partial_fulfillment_count_sales_quantity_once(workflow_system):
    db, erp, service = workflow_system
    sales_request = sale(service, erp)
    supply = ok(service, "supply", dict(work_order="WO-SALES", part="ACCESSORY-B", uom="EA", quantity="5", serialized=False, reason="Production schedule"))
    request = submit(service, {**allocate(), "source": "work_order", "source_id": supply["id"]}, sales_request_id=sales_request["id"])
    accepted(service, "request-approve", change(request), SUP)
    ok(service, "receive", receipt(amount="2", work_order_id=supply["id"]))
    with db.engine.connect() as conn:
        assert workflow.sales_accounted_quantity(conn, sales_request, "ACCESSORY-B", "EA") == 3
    child = next(a for a in state(db)["allocations"] if a["source"] == "stock")
    transfer = submit(service, dict(id=child["id"], version=child["version"], quantity="1", customer="DEMO-SUPPLY", reason="Keep same sales ownership"), "transfer")
    approved = accepted(service, "request-approve", change(transfer), SUP)
    with db.transaction() as conn:
        successor = inv.get(conn, m.inventory_allocations, approved["allocation_id"])
        successor.update(fulfilled="1", status="fulfilled", items=[])
        inv.save(conn, m.inventory_allocations, successor)
        assert workflow.sales_accounted_quantity(conn, sales_request, "ACCESSORY-B", "EA") == 3
    second = submit(service, {**allocate(amount="1"), "source": "work_order", "source_id": supply["id"]}, sales_request_id=sales_request["id"])
    assert command(service, "request-approve", change(second), SUP)[1] == 409
    child = next(a for a in state(db)["allocations"] if a["id"] == child["id"])
    transfer_out = submit(service, dict(id=child["id"], version=child["version"], quantity="1", customer="OTHER-CUSTOMER", reason="Release one unit to another customer"), "transfer")
    accepted(service, "request-approve", change(transfer_out), SUP)
    with db.engine.connect() as conn:
        assert workflow.sales_accounted_quantity(conn, sales_request, "ACCESSORY-B", "EA") == 2
    assert accepted(service, "request-approve", change(second), SUP)["status"] == "approved"


def test_direct_transfer_uses_same_sales_identity_rule_and_retains_approval_provenance(workflow_system):
    db, erp, service = workflow_system
    ok(service, "receive", receipt())
    sales_request = sale(service, erp)
    request = submit(service, allocate(), sales_request_id=sales_request["id"])
    approved = accepted(service, "request-approve", change(request), SUP)
    allocation = next(a for a in state(db)["allocations"] if a["id"] == approved["allocation_id"])
    same = ok(service, "allocation-transfer", dict(id=allocation["id"], version=allocation["version"], quantity="1", customer="DEMO-SUPPLY", reason="Split for same customer"))["allocation"]
    assert same["sales_request_id"] == sales_request["id"]
    with db.engine.connect() as conn:
        assert workflow.sales_accounted_quantity(conn, sales_request, "ACCESSORY-B", "EA") == 3
    other = ok(service, "allocation-transfer", dict(id=same["id"], version=same["version"], quantity="1", customer="OTHER", reason="Reassign customer ownership"))["allocation"]
    assert other["sales_request_id"] == "" and other["approval_request_id"] == request["id"]
    with db.engine.connect() as conn:
        assert workflow.sales_accounted_quantity(conn, sales_request, "ACCESSORY-B", "EA") == 2
