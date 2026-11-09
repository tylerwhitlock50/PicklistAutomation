"""Demand intake and durable planning evidence across retries and source failures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from fulfillment import admin, inventory, model as m, order_book, planning
from fulfillment.domain import Conflict
from fulfillment.erp import VisualERP
from fulfillment.worker import tick
from test_inventory_planning import local_system, context, order, receipt, SUP


def configure(db, **rules):
    with db.transaction() as conn:
        state = admin.load(conn)
        state["configured"] = True
        state["rules"].update(policies={"DEFAULT": {"accumulate": False}}, cooldown=0, **rules)
        m.put_setting(conn, "administration", state)


def intake(erp, order_id="SO-A", quantity="2", revision=1, **changes):
    raw = order(erp, order_id, "CUSTOMER-A", quantity)
    raw.update(status="R", credit_approved=True)
    raw.update(changes)
    return dict(event_id="EVENT-" + str(revision), observed_at=m.now(), source_revision=revision, order=raw)


def test_machine_order_replay_old_revision_and_conflicting_revision(local_system):
    db, erp, service = local_system
    event = intake(erp)
    with db.transaction() as conn:
        first = order_book.upsert(service, conn, SUP, event)
        assert order_book.upsert(service, conn, SUP, event) == first
        later = intake(erp, quantity="3", revision=3)
        assert order_book.upsert(service, conn, SUP, later)["order"]["lines"][0]["quantity"] == "3"
        older = intake(erp, quantity="1", revision=2)
        assert order_book.upsert(service, conn, SUP, older)["ignored_older_revision"]
        assert order_book.get(conn, "DEMO", "SO-A")["order"]["lines"][0]["quantity"] == "3"
        with pytest.raises(Conflict, match="revision"):
            order_book.upsert(service, conn, SUP, {**intake(erp, quantity="4", revision=3), "event_id": "OTHER-EVENT"})
        assert len(inventory.rows(conn, m.order_book)) == 1
        trigger = inventory.rows(conn, m.planning_triggers)[0]
        assert trigger["occurrences"] == 2 and len(trigger["references"]) == 2
        assert not order_book.state(conn)["available"]  # one event is not a complete capture


def test_malformed_external_order_is_rejected(local_system):
    db, erp, service = local_system
    with db.transaction() as conn:
        event = intake(erp)
        event["order"]["lines"].append(deepcopy(event["order"]["lines"][0]))
        with pytest.raises(Conflict, match="Duplicate"):
            order_book.upsert(service, conn, SUP, event)
        event = intake(erp)
        event["order"]["site"] = "ANOTHER"
        with pytest.raises(Conflict, match="outside"):
            order_book.upsert(service, conn, SUP, event)


def test_full_capture_retains_missing_order_and_its_commitments(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        allocation = inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="2", customer="CUSTOMER-A", order="SO-A", hold=True, reason="Promise"))
        order_book.refresh(service, conn, SUP)
        erp.orders = []
        order_book.refresh(service, conn, SUP)
        mirrored = order_book.get(conn, "DEMO", "SO-A")
        assert mirrored["order"]["status"] == "missing" and mirrored["conflicts"][0]["allocation_id"] == allocation["id"]
        assert inventory.active_allocations(conn)[0]["quantity"] == "2"
        assert order_book.demand(service, conn, admin.context(conn))["orders"] == []
        assert planning.board(service, conn)["orders"][0]["status"] == "closed"


def test_quantity_reduction_flags_promise_without_reassignment(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="2", customer="CUSTOMER-A", order="SO-A", hold=False, reason="Promise"))
        order_book.refresh(service, conn, SUP)
        erp.orders[0]["lines"][0]["quantity"] = "1"
        result = planning.run(service, conn, SUP)
        assert result["planning_run"]["status"] == "succeeded" and not result["orders"]
        assert order_book.get(conn, "DEMO", "SO-A")["conflicts"]
        assert inventory.remaining(inventory.active_allocations(conn)[0]) == 2


def test_worker_persists_source_inputs_decisions_and_preview_does_not_write(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
    assert tick(service, "DEMO", "demo")
    with db.transaction() as conn:
        runs = planning.history(conn)["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "succeeded"
        run = planning.history(conn, runs[0]["id"])
        assert run["inputs"]["orders"] and run["inputs"]["stock"] and run["inputs"]["policy"]
        assert run["decisions"][0]["lines"][0]["available"] == "2"
        assert run["decisions"][0]["lines"][0]["missing"] == "0"
        before = inventory.rows(conn, m.planning_runs)
        preview = service.orders(conn)
        assert preview["orders"] and inventory.rows(conn, m.planning_runs) == before
        evidence = planning.provenance(conn, preview["orders"][0])
        assert evidence["policy"]["decision"] == "RELEASE" and evidence["planning_run_id"] == run["id"]
        assert planning.board(service, conn)["orders"][0]["status"] == "ready"


def test_source_outage_retains_mirror_and_failed_run_with_safe_error(local_system, monkeypatch):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    assert tick(service, "DEMO", "demo")
    monkeypatch.setattr(erp, "order_snapshot", lambda ctx: (_ for _ in ()).throw(RuntimeError("password=DO-NOT-EXPOSE")))
    with pytest.raises(Conflict, match="Planning failed"):
        tick(service, "DEMO", "demo")
    with db.transaction() as conn:
        assert len(inventory.rows(conn, m.order_book)) == 1
        failed = planning.history(conn)["runs"][0]
        assert failed["status"] == "failed" and "DO-NOT-EXPOSE" not in str(failed)
        assert planning.board(service, conn)["orders"][0]["status"] == "source_unavailable"
        assert planning.history(conn)["pending_triggers"]
        with pytest.raises(Conflict, match="stale or unavailable"):
            service.orders(conn)


def test_failed_allocation_save_rolls_back_and_retains_attempt(local_system, monkeypatch):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
    def partial(conn, actor, values):
        inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="1", customer="CUSTOMER-A", hold=True, reason="Should rollback"))
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(inventory, "accumulate", partial)
    result = planning.execute(service)
    assert result["planning_run"]["status"] == "failed"
    with db.transaction() as conn:
        assert not inventory.active_allocations(conn)
        assert planning.history(conn)["runs"][0]["status"] == "failed"


def test_abandoned_attempt_is_recorded_as_interrupted_and_retry_keeps_triggers(local_system):
    db, erp, service = local_system
    with db.transaction() as conn:
        planning.enqueue(conn, SUP, "inventory:receive", "R1")
        abandoned = planning.start(conn, SUP)
        abandoned["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        inventory.save(conn, m.planning_runs, abandoned)
    with db.transaction() as conn:
        planning.start(conn, SUP)
        assert planning.history(conn, abandoned["id"])["status"] == "interrupted"
        assert planning.history(conn)["pending_triggers"]


def test_board_retains_outside_horizon_and_active_work(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    capture = erp.order_snapshot(context())
    capture["candidates"][0]["PROMISE_SHIP_DATE"] = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
    erp.order_snapshot = lambda ctx: capture
    with db.transaction() as conn:
        result = planning.run(service, conn, SUP)
        assert not result["orders"] and len(inventory.rows(conn, m.order_book)) == 1
        assert planning.board(service, conn)["orders"][0]["status"] == "outside_horizon"
        work = dict(id="W1", order="SO-A", state="picking", operator="PICKER", created_at=m.now(), updated_at=m.now(), version=1, ship_to="MAIN", customer_id="CUSTOMER-A", lines=[])
        conn.execute(m.works.insert().values(id="W1", state="picking", version=1, updated_at=m.now(), document=work))
        assert planning.board(service, conn)["orders"][0]["status"] == "in_progress"


def test_rule_or_stock_change_marks_old_ready_decision_as_needing_run(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        planning.run(service, conn, SUP)
        state = admin.load(conn)
        state["version"] += 1
        m.put_setting(conn, "administration", state)
        assert planning.board(service, conn)["orders"][0]["status"] == "needs_replanning"
        assert planning.history(conn)["runs"][0]["status"] == "succeeded"


def test_complete_source_query_has_no_horizon_or_inventory_read():
    import re
    erp = object.__new__(VisualERP)
    query = erp.without_supply(erp.candidate_query())
    query, count = re.subn(r"\s+AND COALESCE\(\s*CAST\(COALESCE\(col\.PROMISE_DEL_DATE[\s\S]*?\) <= prm\.THROUGH_DATE", "", query, count=1)
    assert count == 1 and ") <= prm.THROUGH_DATE" not in query
    assert "dbo.PART_LOCATION" not in query and "NOLOCK" not in query


def test_visual_snapshot_preserves_exact_line_status_reference_and_baseline():
    from test_fulfillment_integrations import source_fixture
    adapter, candidate, detail, _, _ = source_fixture()
    detail.update(CUSTOMER_PO_REF="PO-123", LINE_STATUS="X", CREDIT_STATUS="A")
    candidate["LINE_AVAILABLE"] = 0
    snapshot = adapter.order_snapshot()
    assert snapshot["complete"] is True and snapshot["site"] == "TEST"
    captured = snapshot["orders"][0]
    assert captured["customer_reference"] == "PO-123"
    assert captured["lines"][0]["status"] == "X" and captured["baseline"]["1"]["shipped"] == "0"
    assert adapter.inspect_order("SO-1")["lines"][0]["line_available"] is False


def test_stale_complete_capture_blocks_ready_and_preserves_history(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        planned = planning.run(service, conn, SUP)
        source = m.get_setting(conn, "order_book_state")
        source["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        m.put_setting(conn, "order_book_state", source)
        with pytest.raises(Conflict, match="stale"):
            service.orders(conn)
        assert planning.board(service, conn)["orders"][0]["status"] == "source_unavailable"
        saved = planning.history(conn, planned["planning_run"]["id"])
        assert saved["board"][0]["status"] == "ready"


def test_destination_change_review_persists_until_explicit_allocation_revision(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        allocation = inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="2", customer="CUSTOMER-A", order="SO-A", hold=True, reason="Promise"))
        order_book.refresh(service, conn, SUP)
        erp.orders[0]["destination"]["address1"] = "200 Changed Way"
        order_book.refresh(service, conn, SUP)
        order_book.refresh(service, conn, SUP)
        assert order_book.get(conn, "DEMO", "SO-A")["conflicts"]
        inventory.update_allocation(conn, SUP, dict(id=allocation["id"], version=allocation["version"], hold=True, reason="Reviewed destination change"))
        order_book.refresh(service, conn, SUP)
        assert not order_book.get(conn, "DEMO", "SO-A")["conflicts"]


def test_new_snapshot_cannot_be_undone_by_older_event(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 4)]
    old = intake(erp, quantity="1", revision=99)
    old["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.transaction() as conn:
        order_book.refresh(service, conn, SUP)
        assert order_book.upsert(service, conn, SUP, old)["ignored_older_revision"]
        assert order_book.get(conn, "DEMO", "SO-A")["order"]["lines"][0]["quantity"] == "4"


def test_destination_conflict_follows_partial_receipt_promotion_until_explicit_review(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 4)]
    with db.transaction() as conn:
        supply = inventory.supply(conn, SUP, dict(work_order="WO-DEST", part="PART-A", uom="EA", quantity="4", serialized=True, status="open", reason="Production plan"))
        parent = inventory.allocate(conn, SUP, dict(source="work_order", source_id=supply["id"], part="PART-A", uom="EA", quantity="4", customer="CUSTOMER-A", order="SO-A", hold=False, reason="Approved future promise"))
        order_book.refresh(service, conn, SUP)
        erp.orders[0]["destination"]["address1"] = "200 Changed Destination"
        order_book.refresh(service, conn, SUP)
        receipt(conn, 2, ["A", "B"], work_order_id=supply["id"])
        child = next(a for a in inventory.active_allocations(conn) if a["source"] == "stock")
        order_book.refresh(service, conn, SUP)
        conflicts = order_book.get(conn, "DEMO", "SO-A")["conflicts"]
        assert {c["allocation_id"] for c in conflicts} == {parent["id"], child["id"]}
        assert not service.orders(conn)["orders"]
        # A further refresh must not discard lineage or treat automatic version
        # changes as a human review of the changed customer destination.
        order_book.refresh(service, conn, SUP)
        assert len(order_book.get(conn, "DEMO", "SO-A")["conflicts"]) == 2
        for allocation in inventory.active_allocations(conn):
            inventory.update_allocation(conn, SUP, dict(id=allocation["id"], version=allocation["version"], hold=False, reason="Reviewed changed customer destination"))
        order_book.refresh(service, conn, SUP)
        assert not order_book.get(conn, "DEMO", "SO-A")["conflicts"]


def test_pending_request_is_secondary_to_an_existing_ready_order(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        planning.run(service, conn, SUP)
        inventory.save(conn, m.approval_requests, dict(id="PENDING-REVIEW", status="submitted", terms={"order": "SO-A"}))
        row = planning.board(service, conn)["orders"][0]
        assert row["status"] == "ready" and row["pending_approvals"] == ["PENDING-REVIEW"]
        assert service.orders(conn)["orders"]


def test_closed_short_line_retains_and_flags_existing_commitment(local_system):
    db, erp, service = local_system
    configure(db)
    erp.orders = [order(erp, "SO-A", "CUSTOMER-A", 2)]
    capture = erp.order_snapshot(context())
    with db.transaction() as conn:
        receipt(conn, 2, ["A", "B"])
        allocation = inventory.allocate(conn, SUP, dict(source="stock", part="PART-A", uom="EA", quantity="2", customer="CUSTOMER-A", order="SO-A", hold=False, reason="Approved promise"))
        order_book.refresh(service, conn, SUP)
        capture["orders"][0]["lines"][0]["status"] = "C"
        capture["candidates"][0]["LINE_AVAILABLE"] = 0
        capture["observed_at"] = m.now()
        erp.order_snapshot = lambda ctx: capture
        order_book.refresh(service, conn, SUP)
        assert order_book.get(conn, "DEMO", "SO-A")["conflicts"][0]["allocation_id"] == allocation["id"]
        assert inventory.active_allocations(conn)[0]["quantity"] == "2"
        assert not service.orders(conn)["orders"]
