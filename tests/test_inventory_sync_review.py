"""Independent regressions for source-history replay and commitment quarantine."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fulfillment import model as m
from fulfillment.app import create_app
from fulfillment.domain import Conflict
from fulfillment.inventory_connector import Connector, Outbox, Receiver as HttpReceiver
from test_inventory import system, setup, ok, allocate, state, ADMIN
from test_inventory_sync import connector, Source, MACHINE, SCOPE


def test_bootstrap_history_reappearing_does_not_receive_opening_stock_twice(system, tmp_path):
    db, _, _ = system
    source, _, _, producer = connector(system, tmp_path)
    original = deepcopy(source.data["movements"])
    assert producer.once(bootstrap=True)["accepted"]
    source.data["movements"] = []
    source.data["balances"] = []
    assert producer.once()["accepted"]
    source.data["movements"] = original
    source.data["balances"] = [dict(part="ACCESSORY-B", uom="EA", quantity="10", serials=[])]
    assert producer.once()["accepted"]
    view = state(db)
    assert view["balances"][0]["quantity"] == "10"
    assert len(view["ledger"]) == 1


def test_reappearing_linked_receipt_does_not_inflate_remaining_supply(system, tmp_path):
    db, _, _ = system
    source, _, _, producer = connector(system, tmp_path)
    source.data["work_orders"] = [dict(work_order="W:1:1:0:0", part="ACCESSORY-C", uom="EA", serialized=False,
        remaining="4", status="open", due_date="")]
    assert producer.once(bootstrap=True)["accepted"]
    movement = dict(id="101", part="ACCESSORY-C", uom="EA", quantity="2", serials=[], kind="receipt", work_order="W:1:1:0:0")
    source.data["work_orders"][0]["remaining"] = "2"
    source.data["movements"].append(movement)
    source.data["balances"].append(dict(part="ACCESSORY-C", uom="EA", quantity="2", serials=[]))
    assert producer.once()["accepted"]
    source.data["movements"].remove(movement)
    source.data["balances"].pop()
    assert producer.once()["accepted"]
    source.data["movements"].append(movement)
    source.data["balances"].append(dict(part="ACCESSORY-C", uom="EA", quantity="2", serials=[]))
    assert producer.once()["accepted"]
    supply = state(db)["supplies"][0]
    assert supply["received"] == "2"
    assert supply["quantity"] == "4"
    assert supply["available"] == "2"


def test_duplicate_receipts_in_batch_cannot_inflate_future_supply(system, tmp_path):
    db, _, service = system
    _, receiver, _, producer = connector(system, tmp_path)
    assert producer.once(bootstrap=True)["accepted"]
    remote = receiver.state()
    movement = dict(event_id="IT:101", part="ACCESSORY-C", uom="EA", quantity="2", serials=[], work_order="W:1:1:0:0", reason="Production receipt")
    payload = dict(**SCOPE, source="visual", kind="delta", stream_id=remote["stream_id"], event_id="BATCH:DUPLICATE",
        sequence=2, expected_stock_version=remote["stock_version"], observed_at=m.now(), receipts=[movement, movement],
        work_orders=[dict(work_order="W:1:1:0:0", part="ACCESSORY-C", uom="EA", serialized=False, remaining="2", status="open", due_date="")],
        balances=[dict(part="ACCESSORY-B", uom="EA", quantity="10"), dict(part="ACCESSORY-C", uom="EA", quantity="2")])
    _, status = service.command(MACHINE, "inventory:sync-batch", payload, payload["event_id"])
    assert status == 409
    assert state(db)["sync"]["sequence"] == 1
    assert not state(db)["supplies"]


def test_receiver_replayed_receipt_in_new_batch_does_not_inflate_supply(system, tmp_path):
    db, _, _ = system
    source, receiver, _, producer = connector(system, tmp_path)
    source.data["work_orders"] = [dict(work_order="W:1:1:0:0", part="ACCESSORY-C", uom="EA", serialized=False,
        remaining="4", status="open", due_date="")]
    assert producer.once(bootstrap=True)["accepted"]
    source.data["work_orders"][0]["remaining"] = "2"
    source.data["movements"].append(dict(id="101", part="ACCESSORY-C", uom="EA", quantity="2", serials=[], kind="receipt", work_order="W:1:1:0:0"))
    source.data["balances"].append(dict(part="ACCESSORY-C", uom="EA", quantity="2", serials=[]))
    assert producer.once()["accepted"]
    replay = deepcopy(receiver.payloads[-1])
    replay.update(sequence=3, event_id="BATCH:REPLAY", observed_at=m.now(), expected_stock_version=receiver.state()["stock_version"])
    replay["movement_evidence"]["base_sequence"] = 2  # A new envelope still carries the current evidence checkpoint.
    assert receiver.deliver(replay)["accepted"]
    supply = state(db)["supplies"][0]
    assert (supply["quantity"], supply["received"], supply["available"]) == ("4", "2", "2")


def test_quarantine_explains_risk_on_existing_customer_commitment(system, tmp_path):
    db, _, service = system
    source, _, _, producer = connector(system, tmp_path)
    assert producer.once(bootstrap=True)["accepted"]
    promise = ok(service, "allocate", allocate(amount="5"))
    source.data["balances"][0]["quantity"] = "9"
    assert producer.once()["accepted"]
    allocation = next(a for a in state(db)["allocations"] if a["id"] == promise["id"])
    assert allocation["at_risk"]
    assert any(r["code"] == "source_quarantine" for r in allocation["risks"])


def test_packlist_proven_release_catches_up_to_visual_debit_before_local_debit(system, tmp_path):
    db, erp, service = system
    source, _, _, producer = connector(system, tmp_path)
    assert producer.once(bootstrap=True)["accepted"]
    erp.orders[0]["lines"] = [erp.orders[0]["lines"][1]]
    ok(service, "configure", dict(version=1, enabled=True, **SCOPE, reason="Verified opening stock"), ADMIN)
    def command(action, work=None, payload=None):
        result, status = service.command(ADMIN, action, {**({"version": work["version"]} if work else {}), **(payload or {})},
            str(uuid4()), work["id"] if work else None)
        assert status == 200, result
        return result
    work = command("checkout", payload=dict(order="SO-DEMO-100", cart="CART"))
    line = work["lines"][0]
    work = command("pick", work, dict(line_id=line["id"], location="SHIP", item=line["part"], quantity=line["quantity"]))
    work = command("finish-pick", work)
    work = command("handoff", work, dict(cart="CART", station="PACK"))
    work = command("package", work, dict(barcode="BOX"))
    work = command("pack", work, dict(line_id=line["id"], package="BOX", item=line["part"], quantity=line["quantity"]))
    erp.packlists["PL"] = dict(id="PL", site=work["site"], order=work["order"], status="A", destination=deepcopy(work["destination"]),
        lines=[dict(packlist_line="1", order=work["order"], order_line=line["order_line"], part=line["part"], uom=line["uom"],
                    quantity=line["quantity"], serials=line["serials"])])
    work = command("verify", work, dict(packlist="PL"))
    with db.transaction() as conn:
        service.ingest_label(conn, dict(tracking="TRACK", site=work["site"], order=work["order"], packlist="PL", package="BOX",
            destination=work["destination"], void=False, exported_at=m.now()))
    work = command("label", work, dict(package="BOX", tracking="TRACK"))
    # VISUAL posting precedes the final Dispatch release, so a captured debit
    # legitimately makes this stock temporarily quarantined for new picking.
    source.data["balances"][0]["quantity"] = "4"
    source.data["movements"].append(dict(id="101", part="ACCESSORY-B", uom="EA", quantity="-6", serials=[], kind="shipment", work_order=""))
    assert producer.once()["accepted"]
    assert state(db)["balances"][0]["sync_blocked"]
    assert command("release", work)["state"] == "released"
    assert producer.once()["accepted"]
    view = state(db)
    assert view["balances"][0]["quantity"] == "4"
    assert not view["balances"][0]["sync_blocked"]
    assert len(view["ledger"]) == 2


def test_real_http_receiver_auth_health_fence_and_recovery(system, tmp_path):
    db, erp, service = system
    setup(service)
    app = create_app(dict(MODE="demo", DATABASE_URL=db.engine.url.render_as_string(hide_password=False), TESTING=True,
        INVENTORY_TOKEN="i" * 40, VALIDATE_TOKEN="v" * 40), erp=erp)
    client = app.test_client()
    path = "/api/v1/integrations/inventory/state"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer " + "v" * 40}).status_code == 401
    headers = {"Authorization": "Bearer " + "i" * 40}
    assert client.get(path, headers=headers).status_code == 200
    assert client.post("/api/v1/inventory/adjust", headers=headers, json={}).status_code == 401

    class Session:
        def request(self, method, url, headers, json, **kwargs):
            response = client.open(url.removeprefix("https://dispatch.example.test"), method=method, headers=headers, json=json)
            return SimpleNamespace(status_code=response.status_code, json=lambda: response.get_json())

    receiver = HttpReceiver("https://dispatch.example.test", "i" * 40, session=Session())
    source = Source()
    source.data["work_orders"] = [dict(work_order="W:RISK:1:0:0", part="ACCESSORY-B", uom="EA", serialized=False,
        remaining="4", status="open", due_date="")]
    producer = Connector(source, receiver, Outbox(tmp_path / "http-outbox.db"))
    assert producer.once(bootstrap=True)["accepted"]
    supply = state(db)["supplies"][0]
    promise = ok(service, "allocate", {**allocate(amount="4"), "source": "work_order", "source_id": supply["id"]})
    capture = source.capture
    def invalid_source():
        raise Conflict("Source location is locked.", "inventory_source", 503)
    source.capture = invalid_source
    assert producer.once()["error"] == "capture_failed"
    view = state(db)
    assert view["sync"]["status"] == "source_error"
    assert view["balances"][0]["sync_blocked"] and view["balances"][0]["available"] == "0"
    allocation = next(a for a in view["allocations"] if a["id"] == promise["id"])
    assert allocation["at_risk"] and any(r["code"] == "source_unavailable" for r in allocation["risks"])
    source.capture = capture
    assert producer.once()["accepted"]
    assert state(db)["sync"]["status"] == "matched"
    assert state(db)["balances"][0]["available"] == "10"
