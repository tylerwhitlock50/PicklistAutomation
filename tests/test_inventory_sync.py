"""Receiver transaction fencing and durable producer replay across crashes."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from fulfillment import inventory as inv, inventory_sync as sync, model as m
from fulfillment.inventory_connector import Connector, Outbox, DeliveryError, build_batch
from test_inventory import system, setup, ok, call, state, receipt, allocate, ADMIN

MACHINE = {"id": "visual-inventory", "role": "inventory_integration"}
SCOPE = dict(site="DEMO", warehouse="SHIPPING", location="SHIP")


class Source:
    def __init__(self):
        self.data = dict(scope=SCOPE, balances=[dict(part="ACCESSORY-B", uom="EA", quantity="10", serials=[])],
                         work_orders=[], movements=[dict(id="100", part="ACCESSORY-B", uom="EA", quantity="10", serials=[], kind="receipt", work_order="")])

    def capture(self):
        return {**deepcopy(self.data), "observed_at": m.now()}


class Receiver:
    def __init__(self, service):
        self.service, self.lose_once = service, False
        self.payloads = []

    def state(self):
        with self.service.db.engine.connect() as conn:
            return sync.status(conn)

    def deliver(self, payload):
        self.payloads.append(deepcopy(payload))
        result, status = self.service.command(MACHINE, "inventory:sync-batch", payload, payload["event_id"])
        if status != 200:
            raise DeliveryError(result["error"], result["message"])
        if self.lose_once:
            self.lose_once = False
            raise DeliveryError("transport", "Lost response after commit")
        return result


def connector(system, tmp_path):
    db, _, service = system
    setup(service)
    source, receiver = Source(), Receiver(service)
    outbox = Outbox(tmp_path / "outbox.db")
    return source, receiver, outbox, Connector(source, receiver, outbox)


def test_opening_baseline_and_late_lower_id_are_received_once(system, tmp_path):
    db, _, _ = system
    source, receiver, outbox, producer = connector(system, tmp_path)
    assert producer.once(bootstrap=True)["accepted"]
    assert state(db)["balances"][0]["quantity"] == "10"
    assert producer.once()["accepted"]
    assert len(state(db)["ledger"]) == 1
    source.data["movements"].append(dict(id="99", part="ACCESSORY-B", uom="EA", quantity="2", serials=[], kind="receipt", work_order=""))
    source.data["balances"][0]["quantity"] = "12"
    receiver.lose_once = True
    assert producer.once()["error"] == "transport"
    assert outbox.inspect()["pending"]
    assert state(db)["balances"][0]["quantity"] == "12"
    assert producer.once()["accepted"]
    assert receiver.payloads[-1] == receiver.payloads[-2]
    assert state(db)["balances"][0]["quantity"] == "12" and len(state(db)["ledger"]) == 2


def test_changed_and_deleted_source_records_become_issues_not_stock_replays(system, tmp_path):
    db, _, _ = system
    source, _, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    source.data["movements"][0]["quantity"] = "9"
    source.data["balances"][0]["quantity"] = "9"
    producer.once()
    view = state(db)
    assert view["balances"][0]["quantity"] == "10" and view["balances"][0]["available"] == "0"
    assert view["balances"][0]["sync_blocked"]
    assert any(i["kind"] == "source_edit" for i in view["sync"]["issues"])
    source.data["movements"] = []
    producer.once()
    assert any(i["kind"] == "source_deletion" for i in state(db)["sync"]["issues"])


def test_stale_capture_rebuilds_without_reusing_rejected_payload(system, tmp_path):
    db, _, service = system
    source, receiver, outbox, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    original = receiver.deliver
    changed = False
    def interleave(payload):
        nonlocal changed
        if not changed:
            changed = True
            ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="1", reason="Count correction"))
        return original(payload)
    receiver.deliver = interleave
    assert producer.once()["error"] == "capture_stale"
    assert not outbox.inspect()["pending"]
    assert producer.once()["accepted"]
    assert receiver.payloads[-1]["event_id"] != receiver.payloads[-2]["event_id"]
    assert state(db)["balances"][0]["quantity"] == "11"


def test_failed_batch_rolls_back_supply_receipt_and_checkpoint(system, tmp_path):
    db, _, _ = system
    source, _, outbox, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    source.data["movements"].append(dict(id="101", part="ASSEMBLY-A", uom="EA", quantity="2", serials=[dict(serial="DUP", quantity="1"),dict(serial="DUP",quantity="1")], kind="receipt", work_order=""))
    result = producer.once()
    assert not result["accepted"] and outbox.inspect()["pending"]
    assert state(db)["sync"]["sequence"] == 1 and len(state(db)["balances"]) == 1


def test_linked_arrival_after_work_order_closes_preserves_promises(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    source.data["work_orders"] = [dict(work_order="W:1:1:0:0", part="ASSEMBLY-A", uom="EA", serialized=True, remaining="2", status="open", due_date="2026-11-01")]
    producer.once(bootstrap=True)
    supply = state(db)["supplies"][0]
    ok(service, "allocate", {**allocate("ASSEMBLY-A", "2"), "source": "work_order", "source_id": supply["id"]})
    source.data["work_orders"][0].update(remaining="0", status="closed")
    source.data["movements"].append(dict(id="102", part="ASSEMBLY-A", uom="EA", quantity="2", serials=[dict(serial="A",quantity="1"),dict(serial="B",quantity="1")], kind="receipt", work_order="W:1:1:0:0"))
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity="2", serials=["A", "B"]))
    assert producer.once()["accepted"]
    view = state(db)
    assert view["supplies"][0]["status"] == "closed" and view["supplies"][0]["received"] == "2"
    assert all(s["available"] == "0" for s in view["balances"] if s["part"] == "ASSEMBLY-A")


def test_unlinked_arrival_can_promote_existing_stock_without_duplicate_receipt(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    source.data["work_orders"] = [dict(work_order="W:1:1:0:0", part="ACCESSORY-C", uom="EA", serialized=False, remaining="4", status="open", due_date="")]
    producer.once(bootstrap=True)
    supply = state(db)["supplies"][0]
    ok(service, "allocate", {**allocate("ACCESSORY-C", "4"), "source": "work_order", "source_id": supply["id"]})
    source.data["work_orders"][0].update(remaining="0", status="closed")
    source.data["movements"].append(dict(id="103", part="ACCESSORY-C", uom="EA", quantity="4", serials=[], kind="receipt", work_order=""))
    source.data["balances"].append(dict(part="ACCESSORY-C", uom="EA", quantity="4", serials=[]))
    producer.once()
    view = state(db)
    assert any(i["kind"] == "unlinked_receipt" for i in view["sync"]["issues"])
    supply = view["supplies"][0]
    payload = dict(event_id="IT:103", work_order_id=supply["id"], version=supply["version"], reason="Verified production transfer paperwork")
    assert ok(service, "link-receipt", payload)["matched"]
    view = state(db)
    stock = next(s for s in view["balances"] if s["part"] == "ACCESSORY-C")
    assert stock["quantity"] == stock["committed"] == "4" and stock["available"] == "0"
    assert not view["sync"]["issues"]
    assert call(service, "link-receipt", payload)[1] == 409


def test_connected_source_prevents_manual_double_receiving_and_lost_outbox_restart(system, tmp_path):
    db, _, service = system
    source, receiver, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    assert call(service, "receive", receipt())[1] == 409
    assert call(service, "supply", dict())[1] == 409
    lost = Connector(source, receiver, Outbox(tmp_path / "new-outbox.db"))
    with pytest.raises(Exception, match="checkpoint differ"):
        lost.once(bootstrap=True)
    assert state(db)["balances"][0]["quantity"] == "10"


def test_acknowledgement_does_not_clear_stock_quarantine(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    source.data["balances"][0]["quantity"] = "9"
    source.data["movements"].append(dict(id="104", part="ACCESSORY-B", uom="EA", quantity="-1", serials=[], kind="other", work_order=""))
    producer.once()
    ok(service, "sync-ack", dict(id="MOVE:104", reason="Investigation assigned"))
    assert state(db)["balances"][0]["sync_blocked"]
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-1", reason="Confirmed writeoff"))
    producer.once()
    assert not state(db)["balances"][0]["sync_blocked"]
