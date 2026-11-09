"""Local stock entitlements remain authoritative through release-policy planning."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os

import pytest

from fulfillment import admin, inventory, local_planning, model as m
from fulfillment.erp import DemoERP, VisualERP
from fulfillment.service import Service
from test_fulfillment_integrations import source_fixture


SUP = {"id": "SUP", "role": "supervisor"}


@pytest.fixture
def local_system(tmp_path):
    url = os.getenv("FULFILLMENT_TEST_DATABASE_URL", "sqlite:///" + (tmp_path / "planning.db").as_posix())
    db = m.Database(url)
    if os.getenv("FULFILLMENT_TEST_DATABASE_URL"):
        # Explicit opt-in resets only the caller's disposable test database.
        m.metadata.drop_all(db.engine)
    db.initialize()
    erp = DemoERP()
    service = Service(db, erp)
    with db.transaction() as conn:
        m.put_setting(conn, "inventory", dict(enabled=True, site="DEMO", warehouse="SHIPPING", location="DISPATCH", version=1))
    yield db, erp, service
    db.engine.dispose()


def context(**changes):
    result = admin.context(state={**admin.initial_state(), "configured": True})
    result.update(policies={"DEFAULT": {"accumulate": False}}, cooldown=0)
    return {**result, **changes}


def order(erp, name, customer, amount, *, serialized=True):
    result = deepcopy(erp.orders[0])
    result.update(order=name, customer=customer, customer_id=customer)
    result["lines"] = [dict(order_line="1", part="PART-A", quantity=str(amount), uom="EA", serialized=serialized,
                            serials=[], warehouse="ERP-BIN", location="ERP-LOCATION", upc="123")]
    return result


def receipt(conn, amount, serials=None, **extra):
    return inventory.receive(conn, SUP, dict(site="DEMO", warehouse="SHIPPING", location="DISPATCH", event_id="RECEIPT",
        source="visual", part="PART-A", uom="EA", quantity=str(amount), serials=serials or [], reason="Completed goods", **extra))


def test_only_local_serials_and_local_location_reach_prescription(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["LOCAL-1", "LOCAL-2"])
        result = local_planning.orders(service, conn, context())
        lines = result["orders"][0]["lines"]
        assert {s for line in lines for s in line["serials"]} == {"LOCAL-1", "LOCAL-2"}
        assert {line["location"] for line in lines} == {"DISPATCH"}
        assert all(line["inventory_stock_id"] for line in lines)
        assert result["reservations"] == [] and result["inventory_source"] == "app"


def test_customer_allocation_never_leaks_to_earlier_other_customer(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2), order(erp, "SO-B", "CUSTOMER-B", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["LOCAL-1", "LOCAL-2"])
        allocation = inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="2",
            customer="CUSTOMER-B", hold=False, reason="Inside sales promise"))
        result = local_planning.orders(service, conn, context())
        assert [o["order"] for o in result["orders"]] == ["SO-B"]
        assert all(l["inventory_allocation_id"] == allocation["id"] for l in result["orders"][0]["lines"])


def test_incomplete_ordinary_order_does_not_starve_later_complete_order(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2), order(erp, "SO-B", "CUSTOMER-B", 1)]
    with db.transaction() as conn:
        receipt(conn, 1, ["LOCAL-1"])
        result = local_planning.orders(service, conn, context())
        assert [o["order"] for o in result["orders"]] == ["SO-B"]


def test_bulk_stocking_quantity_keeps_precision_through_legacy_gate(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", "1.12345678", serialized=False)]
    with db.transaction() as conn:
        receipt(conn, "1.12345678")
        result = local_planning.orders(service, conn, context())
        assert result["orders"][0]["lines"][0]["quantity"] == "1.12345678"


def test_future_supply_waits_for_receipt_and_explicit_hold_release(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 1)]
    with db.transaction() as conn:
        supply = inventory.supply(conn, SUP, dict(work_order="WO-100/0/0", part="PART-A", uom="EA", quantity="1",
            serialized=True, status="open", reason="Production plan"))
        inventory.allocate(conn, SUP, dict(source="work_order", source_id=supply["id"], part="PART-A", uom="EA",
            quantity="1", customer="CUSTOMER-A", order="SO-A", hold=True, reason="Sales hold"))
        assert local_planning.orders(service, conn, context())["orders"] == []
        receipt(conn, 1, ["LOCAL-1"], work_order_id=supply["id"])
        assert local_planning.orders(service, conn, context())["orders"] == []
        physical = next(a for a in inventory.active_allocations(conn) if a["source"] == "stock")
        inventory.update_allocation(conn, SUP, dict(id=physical["id"], version=physical["version"], hold=False, reason="Customer ready"))
        assert [o["order"] for o in local_planning.orders(service, conn, context())["orders"]] == ["SO-A"]


def test_batch_threshold_can_combine_customer_orders_without_double_spending(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 1), order(erp, "SO-B", "CUSTOMER-A", 1)]
    policies = {"DEFAULT": {"accumulate": True, "account_type": "major", "mix_orders": True,
                             "release_cadence": "threshold", "target_guns": 2}}
    with db.transaction() as conn:
        receipt(conn, 2, ["LOCAL-1", "LOCAL-2"])
        result = local_planning.orders(service, conn, context(policies=policies))
        assert {o["order"] for o in result["orders"]} == {"SO-A", "SO-B"}
        serials = [s for o in result["orders"] for line in o["lines"] for s in line["serials"]]
        assert len(serials) == len(set(serials)) == 2


def test_accumulation_is_returned_for_worker_but_get_does_not_write(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 3)]
    policies = {"DEFAULT": {"accumulate": True, "account_type": "major", "mix_orders": False,
                             "release_cadence": "threshold", "target_guns": 3}}
    with db.transaction() as conn:
        receipt(conn, 2, ["LOCAL-1", "LOCAL-2"])
        before = inventory.snapshot(conn)
        result = local_planning.orders(service, conn, context(policies=policies))
        assert not result["orders"] and len(result["inventory_accumulations"]) == 1
        assert result["decisions"][0]["decision"] == "ACCUMULATING"
        assert inventory.snapshot(conn)["allocations"] == before["allocations"]


def test_policy_allocation_age_releases_partial_stock_but_sales_hold_wins(local_system):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 3)]
    policies = {"DEFAULT": {"accumulate": True, "account_type": "major", "mix_orders": False,
                             "release_cadence": "threshold", "target_guns": 3, "max_hold_days": 7}}
    with db.transaction() as conn:
        receipt(conn, 1, ["LOCAL-1"])
        initial = local_planning.orders(service, conn, context(policies=policies))
        inventory.accumulate(conn, "worker", initial["inventory_accumulations"])
        allocation = inventory.active_allocations(conn)[0]
        allocation["created_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        inventory.save(conn, m.inventory_allocations, allocation)
        aged = local_planning.orders(service, conn, context(policies=policies))
        assert aged["orders"][0]["policy"]["reason_code"] == "maximum_hold_reached"
        assert aged["orders"][0]["lines"][0]["serials"] == ["LOCAL-1"]
        inventory.update_allocation(conn, SUP, dict(id=allocation["id"], version=allocation["version"], hold=True, reason="Inside sales hold"))
        assert local_planning.orders(service, conn, context(policies=policies))["orders"] == []


def test_local_visual_demand_does_not_read_stock_or_serial_supply():
    adapter, candidate, *_ = source_fixture()
    statements = []
    def read(conn, query, **params):
        statements.append(query)
        assert "dbo.PART_LOCATION" not in query and "TraceLocation" not in query
        return [] if "RecentShippedOrders" in query else [deepcopy(candidate)]
    adapter.read = read
    adapter.stocks = lambda conn: pytest.fail("Local demand must not read ERP stock")
    result = adapter.demand([], adapter.context())
    assert result["orders"][0]["lines"][0]["quantity"] == "2"
    assert len(statements) == 2


def test_order_detail_sql_uses_order_facts_without_erp_inventory():
    adapter = VisualERP.__new__(VisualERP)
    adapter.site = "DEMO"
    def read(conn, query, **params):
        assert "dbo.PART_LOCATION" not in query and "EligibleSupply" not in query
        assert "p.STOCK_UM" in query and "co.SITE_ID = :site" in query
        return [{"SITE_ID": "DEMO"}]
    adapter.read = read
    adapter.detail(None, "SO-A")


@pytest.mark.parametrize("block", ["documents", "destination"])
def test_local_hard_block_replaces_prior_gate_reason_consistently(local_system, block):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 1)]
    ctx = context(holds=[dict(cust_order_id="SO-A", hold_kind="sales")])
    if block == "documents":
        ctx["document_checks_required"] = True
        expected = "document_review_required"
    else:
        erp.orders[0]["destination"]["address1"] = ""
        expected = "Incomplete destination"
        # The gate still receives the sales hold, while this order-fact adapter
        # isolates the incomplete-address override being exercised.
        demand = erp.demand
        def without_order_blocks(works, context):
            result = demand(works, context)
            for manifest in result["orders"]:
                manifest["blocks"] = []
            return result
        erp.demand = without_order_blocks
    with db.transaction() as conn:
        receipt(conn, 1, ["LOCAL-1"])
        result = local_planning.orders(service, conn, ctx)
        assert not result["orders"] and not result["inventory_accumulations"]
        decision = result["decisions"][0]
        assert decision["decision"] == "BLOCKED"
        assert decision["reason_code"] == "hard_block"
        assert decision["label"].startswith("BLOCKED - ") and expected in decision["label"]
        assert decision["allocations"] == []


@pytest.mark.parametrize("failure", ["stale", "source_error"])
def test_unhealthy_inventory_source_removes_readiness_and_prevents_checkout(local_system, failure):
    db, erp, service = local_system
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        state = admin.load(conn)
        state["configured"] = True
        state["rules"].update(policies={"DEFAULT": {"accumulate": False}}, cooldown=0)
        m.put_setting(conn, "administration", state)
        receipt(conn, 2, ["LOCAL-1", "LOCAL-2"])
        # Cover both an existing customer commitment and the uncommitted pool.
        inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="1",
            customer="CUSTOMER-A", order="SO-A", hold=False, reason="Inside sales promise"))
        healthy = dict(stream_id="TEST-STREAM", observed_at=m.now(), status="matched", sequence=1)
        m.put_setting(conn, "inventory_sync", healthy)
        assert [o["order"] for o in service.orders(conn)["orders"]] == ["SO-A"]
        unhealthy = dict(healthy)
        if failure == "stale":
            unhealthy["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        else:
            unhealthy["source_error"] = dict(code="capture_failed", at=m.now())
        m.put_setting(conn, "inventory_sync", unhealthy)
        assert not service.orders(conn)["orders"]
        assert all(s["sync_blocked"] and s["available"] == "0" for s in inventory.snapshot(conn)["balances"])
    result, status = service.command(SUP, "checkout", dict(order="SO-A", cart="CART-1"), "unhealthy-checkout")
    assert status == 409 and result["error"] == "ineligible"
    with db.transaction() as conn:
        assert service.list_work(conn) == []
        assert inventory.rows(conn, m.inventory_locks) == []
        m.put_setting(conn, "inventory_sync", {**healthy, "observed_at": m.now()})
    result, status = service.command(SUP, "checkout", dict(order="SO-A", cart="CART-1"), "healthy-checkout")
    assert status == 200 and result["state"] == "picking"
    assert sum(int(line["quantity"]) for line in result["lines"]) == 2
