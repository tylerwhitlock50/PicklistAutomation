"""Count corrections can resolve new identities without synthesizing receipts."""
import pytest

from fulfillment import inventory as inv, model as m
from fulfillment.domain import key
from test_inventory import system, call, ok, state, allocate
from test_inventory_sync import connector


def test_new_serial_correction_requires_current_source_evidence(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    correction = dict(part="ASSEMBLY-A", uom="EA", quantity="2", serials=["FOUND-1", "FOUND-2"], reason="Physically verified return before cutover")
    assert call(service, "adjust", correction)[1] == 409
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity="2", serials=["FOUND-1", "FOUND-2"]))
    source.data["movements"].append(dict(id="110", part="ASSEMBLY-A", uom="EA", quantity="2", serials=[dict(serial="FOUND-1",quantity="1"),dict(serial="FOUND-2",quantity="1")], kind="other", work_order=""))
    assert producer.once()["accepted"]
    assert ok(service, "adjust", correction)["accepted"]
    view = state(db)
    assert {s["serial"] for s in view["balances"] if s["part"] == "ASSEMBLY-A"} == {"FOUND-1", "FOUND-2"}
    assert len([e for e in view["ledger"] if e["kind"] == "adjustment"]) == 2
    assert call(service, "adjust", correction)[1] == 409
    assert producer.once()["accepted"] and state(db)["sync"]["status"] == "matched"


def test_source_correction_cannot_invent_identity_or_use_old_local_comparison(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    source.data["balances"].append(dict(part="NEW-BULK", uom="EA", quantity="4", serials=[]))
    producer.once()
    data = dict(part="NEW-BULK", uom="EA", quantity="5", serials=[], reason="Physical count")
    assert call(service, "adjust", data)[1] == 409
    assert call(service, "adjust", {**data, "quantity":"4", "part":"UNKNOWN"})[1] == 409
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="1", reason="Separate count changes ledger"))
    assert call(service, "adjust", {**data, "quantity":"4"})[0]["error"] == "stale_snapshot"
    producer.once()
    assert ok(service, "adjust", {**data, "quantity":"4"})["accepted"]
    assert next(s for s in state(db)["balances"] if s["part"] == "NEW-BULK")["quantity"] == "4"


def prepare_returned_serial(system, tmp_path):
    db, _, service = system
    source, receiver, outbox, producer = connector(system, tmp_path)
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity="1", serials=["RETURNED-1"]))
    source.data["movements"].append(dict(id="101", part="ASSEMBLY-A", uom="EA", quantity="1",
        serials=[dict(serial="RETURNED-1", quantity="1")], kind="receipt", work_order=""))
    source.data["work_orders"] = [dict(work_order="W:RETURN:1:0:0", part="ASSEMBLY-A", uom="EA",
        serialized=True, remaining="2", status="open", due_date="")]
    assert producer.once(bootstrap=True)["accepted"]
    supply = state(db)["supplies"][0]
    ok(service, "allocate", {**allocate("ASSEMBLY-A", "2"), "source":"work_order", "source_id":supply["id"]})
    ok(service, "adjust", dict(part="ASSEMBLY-A", uom="EA", quantity="-1", serials=["RETURNED-1"], reason="Verified earlier outbound movement"))
    source.data["balances"] = [b for b in source.data["balances"] if b["part"] != "ASSEMBLY-A"]
    source.data["movements"].append(dict(id="102", part="ASSEMBLY-A", uom="EA", quantity="-1",
        serials=[dict(serial="RETURNED-1", quantity="-1")], kind="shipment", work_order=""))
    assert producer.once()["accepted"]
    return source, receiver, outbox, producer


@pytest.mark.parametrize("serials", [["RETURNED-1"], ["RETURNED-1", "NEW-2"]])
def test_known_serial_receipt_is_reviewed_without_receiving_or_promoting_and_replay_is_safe(system, tmp_path, serials):
    db, _, service = system
    source, receiver, outbox, producer = prepare_returned_serial(system, tmp_path)
    source.data["work_orders"][0].update(remaining="0", status="closed")
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity=str(len(serials)), serials=serials))
    source.data["movements"].append(dict(id="103", part="ASSEMBLY-A", uom="EA", quantity=str(len(serials)),
        serials=[dict(serial=s, quantity="1") for s in serials], kind="receipt", work_order="W:RETURN:1:0:0"))
    before_ledger = len(state(db)["ledger"])
    receiver.lose_once = True
    assert producer.once()["error"] == "transport"
    assert outbox.inspect()["pending"]
    view = state(db)
    assert view["sync"]["status"] == "variance" and view["sync"]["sequence"] == 3
    assert len(view["ledger"]) == before_ledger
    assert [(s["serial"], s["quantity"]) for s in view["balances"] if s["part"] == "ASSEMBLY-A"] == [("RETURNED-1", "0")]
    assert view["supplies"][0]["received"] == view["supplies"][0]["quantity"] == "0"
    assert len(view["allocations"]) == 1 and view["allocations"][0]["fulfilled"] == "0"
    assert [i["kind"] for i in view["sync"]["issues"]] == ["receipt_review"]
    with db.engine.connect() as conn:
        result = inv.get(conn, m.inventory_inbox, key("DEMO", "visual", "receipt", "IT:103"))["response"]
    assert result["accepted"] and result["review_required"] and result["items"] == []
    assert result["quantity"] == str(len(serials)) and result["part"] == "ASSEMBLY-A" and result["uom"] == "EA"
    assert result["work_order_id"] == view["supplies"][0]["id"]
    assert producer.once()["accepted"] and not outbox.inspect()["pending"]
    assert receiver.payloads[-1] == receiver.payloads[-2] and len(state(db)["ledger"]) == before_ledger
    supply = view["supplies"][0]
    rejected, status = call(service, "link-receipt", dict(event_id="IT:103", work_order_id=supply["id"],
        version=supply["version"], reason="Attempt to bind reviewed arrival"))
    assert status == 409 and rejected["error"] == "receipt_review" and "count adjustment" in rejected["message"]
    assert ok(service, "adjust", dict(part="ASSEMBLY-A", uom="EA", quantity=str(len(serials)), serials=serials,
        reason="Inspected return/rework and verified source comparison"))["accepted"]
    assert producer.once()["accepted"]
    final = state(db)
    assert final["sync"]["status"] == "matched"
    assert {s["serial"]:s["quantity"] for s in final["balances"] if s["part"] == "ASSEMBLY-A"} == {s:"1" for s in serials}
    assert final["supplies"][0]["received"] == "0" and final["allocations"][0]["fulfilled"] == "0"


def test_unlinked_known_serial_receipt_does_not_offer_work_order_matching(system, tmp_path):
    db, _, _ = system
    source, _, _, producer = prepare_returned_serial(system, tmp_path)
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity="1", serials=["RETURNED-1"]))
    source.data["movements"].append(dict(id="103", part="ASSEMBLY-A", uom="EA", quantity="1",
        serials=[dict(serial="RETURNED-1", quantity="1")], kind="receipt", work_order=""))
    assert producer.once()["accepted"]
    assert [i["kind"] for i in state(db)["sync"]["issues"]] == ["receipt_review"]


@pytest.mark.parametrize("serials,amount", [(["RETURNED-1", "RETURNED-1"], "2"), (["RETURNED-1"], "2")])
def test_review_receipt_still_rejects_duplicate_serials_and_quantity_mismatch_atomically(system, tmp_path, serials, amount):
    db, _, _ = system
    source, _, outbox, producer = prepare_returned_serial(system, tmp_path)
    source.data["movements"].append(dict(id="103", part="ASSEMBLY-A", uom="EA", quantity=amount,
        serials=[dict(serial=s, quantity="1") for s in serials], kind="receipt", work_order="W:RETURN:1:0:0"))
    before = state(db)
    assert not producer.once()["accepted"] and outbox.inspect()["pending"]
    after = state(db)
    assert after["sync"]["sequence"] == 2 and after["sync"]["issues"] == []
    assert after["ledger"] == before["ledger"] and after["supplies"] == before["supplies"]
    with db.engine.connect() as conn:
        assert not any(r["id"] == key("DEMO", "visual", "receipt", "IT:103") for r in inv.rows(conn, m.inventory_inbox))


def test_distinct_receipts_reusing_new_serial_in_one_capture_are_all_reviewed(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    assert producer.once(bootstrap=True)["accepted"]
    source.data["balances"].append(dict(part="ASSEMBLY-A", uom="EA", quantity="1", serials=["BETWEEN-POLLS"]))
    source.data["movements"].extend([
        dict(id="104", part="ASSEMBLY-A", uom="EA", quantity="1", serials=[dict(serial="BETWEEN-POLLS", quantity="1")], kind="receipt", work_order=""),
        dict(id="105", part="ASSEMBLY-A", uom="EA", quantity="-1", serials=[dict(serial="BETWEEN-POLLS", quantity="-1")], kind="shipment", work_order=""),
        dict(id="106", part="ASSEMBLY-A", uom="EA", quantity="1", serials=[dict(serial="BETWEEN-POLLS", quantity="1")], kind="receipt", work_order=""),
    ])
    assert producer.once()["accepted"]
    view = state(db)
    assert not any(s["part"] == "ASSEMBLY-A" for s in view["balances"])
    assert len(view["ledger"]) == 1 and view["sync"]["status"] == "variance"
    assert [i["kind"] for i in view["sync"]["issues"]] == ["receipt_review", "receipt_review"]
    with db.engine.connect() as conn:
        for event in ("IT:104", "IT:106"):
            receipt = inv.get(conn, m.inventory_inbox, key("DEMO", "visual", "receipt", event))["response"]
            assert receipt["review_required"] and receipt["items"] == [] and receipt["quantity"] == "1"
    assert ok(service, "adjust", dict(part="ASSEMBLY-A", uom="EA", quantity="1", serials=["BETWEEN-POLLS"],
        reason="Verified net return after multiple movements between source captures"))["accepted"]
    assert producer.once()["accepted"] and state(db)["sync"]["status"] == "matched"
    assert len(state(db)["ledger"]) == 2
