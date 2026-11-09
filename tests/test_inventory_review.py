"""Adversarial regressions for inventory identity, sync ordering and returns."""
from copy import deepcopy
from uuid import uuid4

from fulfillment import admin, inventory, model as m
from test_inventory import (system, setup, ok, call, state, ADMIN,
                            opening, receipt, activate, test_local_checkout_cancel_putaway_and_release_debit as complete_shipment)


def work_order(**extra):
    return dict(work_order="W:100:1:0:0", part="PART-A", uom="EA", serialized=False,
                quantity="10", status="open", reason="Production supply", **extra)


def test_work_order_cannot_be_duplicated_under_an_arbitrary_id(system):
    db, _, service = system
    setup(service)
    first = ok(service, "supply", work_order())
    result, status = call(service, "supply", {**work_order(), "id": "ANOTHER-ID"})
    assert status == 409, result
    assert len(state(db)["supplies"]) == 1
    assert state(db)["supplies"][0]["id"] == first["id"]


def test_manual_work_order_edit_cannot_erase_source_revision_guard(system):
    _, _, service = system
    setup(service)
    source = {**work_order(), "site": "DEMO", "warehouse": "SHIPPING", "location": "SHIP",
              "source": "visual", "event_id": "REV10", "source_revision": 10}
    current = ok(service, "sync-supply", source)
    ok(service, "supply", {**current, "status": "cancelled", "reason": "Production cancelled"})
    result, status = call(service, "sync-supply", {**source, "event_id": "REV9", "source_revision": 9})
    assert status == 409, result


def test_cancelled_work_order_never_reports_available_future_supply(system):
    db, _, service = system
    setup(service)
    ok(service, "supply", {**work_order(), "status": "cancelled"})
    assert state(db)["supplies"][0]["available"] == "0"


def test_opening_evidence_cannot_authorize_a_different_warehouse_boundary(system):
    _, _, service = system
    setup(service)
    opening(service, [])
    changed = ok(service, "configure", dict(version=1, enabled=False, site="DEMO", warehouse="SHIPPING",
        location="OTHER", reason="Changed opening boundary"), ADMIN)
    result, status = call(service, "configure", {**changed, "enabled": True, "reason": "Activate changed boundary"}, ADMIN)
    assert status == 409, result


def test_admin_unassign_releases_local_policy_assignments_but_keeps_sales_promises(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt("ASSEMBLY-A", "2", ["POLICY", "SALES"]))
    activate(service, [{"part": "ASSEMBLY-A", "uom": "EA", "quantity": "2", "serials": ["POLICY", "SALES"]}])
    with db.transaction() as conn:
        conn.execute(m.users.insert().values(id="ADMIN", name="Admin", role="admin", enabled=True, pin_hash="unused"))
        values = dict(source="stock", part="ASSEMBLY-A", uom="EA", quantity="1", customer="DEMO-SUPPLY",
                      order="SO-DEMO-100", hold=False, reason="Protected promise")
        policy = inventory.allocate(conn, ADMIN, values)
        policy["policy_managed"] = True
        inventory.save(conn, m.inventory_allocations, policy)
        sales = inventory.allocate(conn, ADMIN, {**values, "hold": True})
        version = admin.load(conn)["version"]
    result, status = service.command(ADMIN, "admin:unassign", dict(order="SO-DEMO-100", version=version,
        reason="Replan policy assignment"), str(uuid4()))
    assert status == 200, result
    with db.engine.connect() as conn:
        assert inventory.get(conn, m.inventory_allocations, policy["id"])["status"] == "released"
        assert inventory.get(conn, m.inventory_allocations, sales["id"])["status"] == "active"
        assert sum(float(s["available"]) for s in inventory.snapshot(conn)["balances"]) == 1
        assert admin.context(conn)["holds"]


def test_reviewed_return_of_shipped_serial_can_be_checked_out_again(system):
    db, erp, service = system
    complete_shipment(system)
    returning = deepcopy(erp.orders[0])
    returning["order"] = "SO-RETURN"
    returning["lines"] = [{**returning["lines"][0], "quantity": "1"}]
    erp.orders = [returning]
    with db.transaction() as conn:
        administration = admin.load(conn)
        administration["rules"]["cooldown"] = 0
        m.put_setting(conn, "administration", administration)
    ok(service, "adjust", dict(part="ASSEMBLY-A", uom="EA", serials=["LOCAL-A"], quantity="1", reason="Reviewed customer return"))
    with db.engine.connect() as conn:
        assert service.orders(conn)["orders"][0]["order"] == "SO-RETURN"
    result, status = service.command(ADMIN, "checkout", {"order": "SO-RETURN", "cart": "RETURN-CART"}, str(uuid4()))
    assert status == 200, result
