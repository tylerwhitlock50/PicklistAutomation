"""A durable plan does not replace live hard checks at stock checkout."""
from copy import deepcopy

import pytest
from sqlalchemy import inspect, select

from fulfillment import inventory, model as m, planning
from fulfillment.domain import Conflict
from test_fulfillment_integrations import source_fixture
from test_inventory_planning import local_system, order, receipt, SUP
from test_order_planning import configure


@pytest.mark.parametrize("change,expected", [
    ({"CREDIT_STATUS": "H"}, "credit"),
    ({"ORDER_RELEASED": 0, "ORDER_STATUS": "X"}, None),
    ({"ORDER_RELEASED": 0, "ORDER_STATUS": "C"}, None),
    ({"LINE_AVAILABLE": 0, "LINE_STATUS": "X"}, None),
    ({"LINE_AVAILABLE": 0, "LINE_STATUS": "C"}, None),
    ({"SHIPTO_ADDR_1": "200 Changed Destination"}, "destination changed"),
])
def test_live_order_changes_after_plan_reject_checkout_without_custody(local_system, change, expected):
    db, _, service = local_system
    configure(db)
    adapter, _, detail, *_ = source_fixture()
    adapter.site = "DEMO"
    # Keep the real VISUAL hard-check implementation, with only its SQL read
    # replaced by the fixture's mutable source rows.
    del adapter.blocking
    detail.update(CREDIT_STATUS="A", SHIPTO_FFL_NUMBER="SYNTHETIC-TEST-FFL", SHIPTO_FFL_EXPIRY_RAW="2099-12-31")
    service.erp = adapter
    service.live = True
    with db.transaction() as conn:
        inventory.receive(conn, SUP, dict(site="DEMO", warehouse="SHIPPING", location="DISPATCH",
            event_id="RECEIPT", source="visual", part="PART-1", uom="EA", quantity="2",
            serials=["A", "B"], reason="Completed goods"))
        result = planning.run(service, conn, SUP)
        assert result["planning_run"]["status"] == "succeeded"
        assert [o["order"] for o in result["orders"]] == ["SO-1"], str(result)
        stock_before = deepcopy(inventory.rows(conn, m.inventory_stock))
        allocations_before = deepcopy(inventory.rows(conn, m.inventory_allocations))

    detail.update(change)
    with db.transaction() as conn:
        # The last successful full capture is still ready. Checkout must read
        # current source facts rather than merely trusting that decision.
        assert [o["order"] for o in service.orders(conn)["orders"]] == ["SO-1"]
    response, status = service.command(SUP, "checkout", {"order": "SO-1", "cart": "CART-1"}, "after-source-edit")
    assert status == 409
    if expected:
        assert expected in response["message"].lower()
    with db.transaction() as conn:
        assert not conn.execute(select(m.works)).first()
        assert not conn.execute(select(m.claims)).first()
        assert not inventory.rows(conn, m.inventory_locks)
        assert inventory.rows(conn, m.inventory_stock) == stock_before
        assert inventory.rows(conn, m.inventory_allocations) == allocations_before
        assert planning.history(conn)["runs"][0]["status"] == "succeeded"
        # The rejection itself is durable and exact retries remain rejected.
        assert conn.execute(select(m.requests)).first().status == 409
    assert service.command(SUP, "checkout", {"order": "SO-1", "cart": "CART-1"}, "after-source-edit") == (response, status)


def test_fully_posted_closed_lines_recheck_credit_and_documents_before_release():
    adapter, _, detail, *_ = source_fixture()
    work = adapter.available([], [])["orders"][0]
    del adapter.blocking
    detail.update(ORDER_STATUS="C", LINE_STATUS="C", SHIPPED_QTY=2, OPEN_QTY=0,
                  CREDIT_STATUS="A", SHIPTO_FFL_NUMBER="SYNTHETIC-TEST-FFL", SHIPTO_FFL_EXPIRY_RAW="2099-12-31")
    adapter.check_order(work, final=True)
    detail["CREDIT_STATUS"] = "H"
    with pytest.raises(Conflict, match="credit"):
        adapter.check_order(work, final=True)
    detail["CREDIT_STATUS"] = "A"
    detail["SHIPTO_FFL_EXPIRY_RAW"] = "2000-01-01"
    with pytest.raises(Conflict, match="ffl"):
        adapter.check_order(work, final=True)


def test_schema_four_upgrade_preserves_existing_custody_and_commitments(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 3, ["A", "B", "C"])
        inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="1",
            customer="CUSTOMER-B", hold=True, reason="Retained inside-sales promise"))
    response, status = service.command(SUP, "checkout", {"order": "SO-A", "cart": "CART-1"}, "before-migration")
    assert status == 200
    old_tables = (m.works, m.claims, m.inventory_stock, m.inventory_allocations,
                  m.inventory_ledger, m.inventory_locks, m.events, m.requests, m.settings)
    new_tables = (m.order_book, m.planning_runs, m.planning_triggers, m.sales_requests,
                  m.approval_requests, m.daily_closes, m.inventory_source_movements)
    with db.transaction() as conn:
        before = {table.name: sorted([dict(row) for row in conn.execute(select(table)).mappings()], key=m.digest)
                  for table in old_tables}
        assert before[m.inventory_stock.name] and before[m.inventory_allocations.name]
        assert before[m.works.name] and before[m.inventory_locks.name]
        conn.execute(m.schema.update().values(version=4))
        for table in new_tables:
            table.drop(conn)

    db.initialize()
    db.initialize()
    assert {table.name for table in new_tables} <= set(inspect(db.engine).get_table_names())
    with db.transaction() as conn:
        assert conn.execute(select(m.schema.c.version)).scalar_one() == 5
        assert {table.name: sorted([dict(row) for row in conn.execute(select(table)).mappings()], key=m.digest)
                for table in old_tables} == before
        assert all(not conn.execute(select(table)).first() for table in new_tables)
    assert service.command(SUP, "checkout", {"order": "SO-A", "cart": "CART-1"}, "before-migration") == (response, status)


def test_live_local_inventory_requires_complete_order_mirror(local_system):
    db, _, service = local_system
    configure(db)
    service.live = True
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        with pytest.raises(Conflict, match="complete VISUAL order book") as caught:
            service.orders(conn)
        assert caught.value.status == 503
    response, status = service.command(SUP, "checkout", {"order": "SO-1001", "cart": "CART-1"}, "before-order-capture")
    assert status == 503 and response["error"] == "order_source_unavailable"
    with db.transaction() as conn:
        assert not conn.execute(select(m.works)).first()
        assert not conn.execute(select(m.claims)).first()
        assert not inventory.rows(conn, m.inventory_locks)
