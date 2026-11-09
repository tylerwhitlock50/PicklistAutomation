"""Atomic promise movement, customer binding, priority and actionable supply risk."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

from werkzeug.security import generate_password_hash

from fulfillment import admin, inventory, model as m
from fulfillment.app import create_app
from test_inventory import system, setup, ok, call, state, receipt, allocate, activate, ADMIN


def move(service, allocation, amount, customer="NEW-CUSTOMER", **extra):
    return call(service, "allocation-transfer", dict(id=allocation["id"], version=allocation["version"],
        quantity=str(amount), customer=customer, reason="Updated inside sales request", **extra))


def test_partial_customer_transfer_preserves_stock_and_promise_lineage(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="10"))
    original = ok(service, "allocate", {**allocate(amount="6"), "request_reference": "SALES-123", "need_by": "2099-10-20"})
    result, status = move(service, original, 2)
    assert status == 200, result
    child, source = result["allocation"], result["source"]
    assert source["quantity"] == "4" and source["status"] == "active"
    assert child["transfer_from"] == original["id"] and child["customer"] == "NEW-CUSTOMER"
    assert child["request_reference"] == "SALES-123" and child["hold"]
    assert child["need_by"] == "2099-10-20" and child["priority"] == 50
    assert source["transfers"][0]["id"] == child["id"]
    balance = state(db)["balances"][0]
    assert (balance["quantity"], balance["committed"], balance["available"]) == ("10", "6", "4")


def test_binding_validates_customer_and_exact_order_line_without_releasing_stock(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="3"))
    original = ok(service, "allocate", allocate(amount="3"))
    before = state(db)["allocations"]
    assert move(service, original, 3, customer="WRONG", order="SO-DEMO-100", order_line="2")[1] == 409
    assert move(service, original, 3, customer="DEMO-SUPPLY", order="SO-DEMO-100", order_line="1")[1] == 409
    assert state(db)["allocations"] == before
    result, status = move(service, original, 3, customer="DEMO-SUPPLY", order="SO-DEMO-100", order_line="2")
    assert status == 200, result
    assert result["source"]["status"] == "transferred"
    assert result["allocation"]["order_line"] == "2"
    assert state(db)["balances"][0]["available"] == "0"


def test_concurrent_transfers_have_one_winner_and_never_open_public_stock(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="5"))
    original = ok(service, "allocate", allocate(amount="5"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda customer: move(service, original, 5, customer=customer), ["B", "C"]))
    assert sorted(status for _, status in results) == [200, 409]
    assert state(db)["balances"][0]["available"] == "0"
    assert call(service, "allocate", allocate(amount="1"))[1] == 409


def test_future_transfer_and_priority_control_partial_receipt_promotion(system):
    db, _, service = system
    setup(service)
    source = ok(service, "supply", dict(work_order="W:PRIORITY:1:0:0", part="ASSEMBLY-A", uom="EA", serialized=True,
        quantity="6", due_date="2099-10-21", status="open", reason="Production plan"))
    original = ok(service, "allocate", {**allocate("ASSEMBLY-A", "6"), "source": "work_order", "source_id": source["id"],
                                      "priority": 50, "request_reference": "NORMAL"})
    moved, status = move(service, original, 2, priority=0, request_reference="RUSH", need_by="2099-10-20")
    assert status == 200, moved
    child_id = moved["allocation"]["id"]
    ok(service, "receive", receipt("ASSEMBLY-A", "2", ["URGENT-1", "URGENT-2"], work_order_id=source["id"]))
    view = state(db)
    promoted = next(a for a in view["allocations"] if a["source"] == "stock")
    assert promoted["parent_id"] == child_id and promoted["customer"] == "NEW-CUSTOMER"
    assert promoted["priority"] == 0 and promoted["request_reference"] == "RUSH" and promoted["hold"]
    normal = next(a for a in view["allocations"] if a["id"] == original["id"])
    assert normal["fulfilled"] == "0" and normal["quantity"] == "4"


def test_future_shortfall_is_attributed_to_affected_customer_by_priority(system):
    db, _, service = system
    setup(service)
    source = ok(service, "supply", dict(work_order="W:RISK:1:0:0", part="ACCESSORY-B", uom="EA", serialized=False,
        quantity="6", due_date="2099-10-21", status="open", reason="Production plan"))
    ordinary = ok(service, "allocate", {**allocate(amount="4"), "source": "work_order", "source_id": source["id"], "priority": 50})
    urgent = ok(service, "allocate", {**allocate(amount="2"), "source": "work_order", "source_id": source["id"],
                                    "priority": 0, "need_by": "2099-10-20"})
    ok(service, "supply", {**source, "quantity": "3", "reason": "Production yield reduced"})
    promises = {a["id"]: a for a in state(db)["allocations"]}
    assert promises[urgent["id"]]["shortage"] == "0"
    assert {r["code"] for r in promises[urgent["id"]]["risks"]} == {"arrival_after_need_by"}
    assert promises[ordinary["id"]]["shortage"] == "3"
    assert promises[ordinary["id"]]["at_risk"]
    assert "supply_shortfall" in {r["code"] for r in promises[ordinary["id"]]["risks"]}


def test_partial_transfer_preserves_checked_out_units_and_their_owner(system):
    db, erp, service = system
    setup(service)
    erp.orders[0]["lines"] = [erp.orders[0]["lines"][1]]
    ok(service, "receive", receipt(amount="10"))
    original = ok(service, "allocate", {**allocate(amount="8", order="SO-DEMO-100", order_line="2"), "hold": False})
    activate(service, [{"part": "ACCESSORY-B", "uom": "EA", "quantity": "10"}])
    work, status = service.command(ADMIN, "checkout", {"order": "SO-DEMO-100", "cart": "CART"}, str(uuid4()))
    assert status == 200, work
    promise = next(a for a in state(db)["allocations"] if a["id"] == original["id"])
    assert promise["locked_quantity"] == "6" and promise["transferable_quantity"] == "2"
    assert move(service, original, 3)[1] == 409
    moved, status = move(service, original, 2)
    assert status == 200, moved
    assert moved["source"]["quantity"] == "6"
    with db.engine.connect() as conn:
        lock = inventory.get(conn, m.inventory_locks, work["id"])
        assert sum(Decimal(l["quantity"]) for l in lock["lines"]) == 6
        assert all(l["inventory_allocation_id"] == original["id"] for l in lock["lines"])
    assert state(db)["balances"][0]["available"] == "2"


def test_invalid_metadata_and_fractional_serial_transfer_are_atomic(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt("ASSEMBLY-A", "1", ["SERIAL"]))
    original = ok(service, "allocate", allocate("ASSEMBLY-A", "1"))
    before = state(db)["allocations"]
    assert move(service, original, "0.5")[1] == 400
    assert move(service, original, 1, priority=True)[1] == 400
    assert move(service, original, 1, need_by="tomorrow")[1] == 400
    assert state(db)["allocations"] == before


def test_metadata_update_retains_hold_and_ownership(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt())
    allocation = ok(service, "allocate", allocate())
    changed = ok(service, "allocation-update", dict(id=allocation["id"], version=1, priority=20,
        request_reference="SALE-234", need_by="2099-10-20", reason="Sales planning update"))
    assert changed["hold"] and changed["customer"] == allocation["customer"]
    assert changed["priority"] == 20 and state(db)["balances"][0]["available"] == "7"


def test_demand_picker_requires_auth_and_does_not_change_release_horizon(system):
    db, erp, _ = system
    with db.transaction() as conn:
        conn.execute(m.users.insert().values(id="ADMIN", name="Admin", role="admin", enabled=True,
                                            pin_hash=generate_password_hash("123456")))
    original = erp.demand
    def demand(works, context):
        assert context["lookahead_days"] == 365
        return original(works, context)
    erp.demand = demand
    app = create_app({"MODE": "demo", "DATABASE_URL": db.engine.url.render_as_string(hide_password=False), "TESTING": True}, erp=erp)
    client = app.test_client()
    assert client.get("/api/v1/inventory/demand").status_code == 401
    assert client.post("/api/v1/auth/login", json={"operator": "ADMIN", "pin": "123456"}).status_code == 200
    result = client.get("/api/v1/inventory/demand")
    assert result.status_code == 200 and result.json["orders"][0]["customer_id"] == "DEMO-SUPPLY"
    assert result.json["horizon_days"] == 365
    with db.engine.connect() as conn:
        assert admin.context(conn)["lookahead_days"] == 10
