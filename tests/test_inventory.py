"""Inventory accounting, ownership, replay and lifecycle invariants.

Set FULFILLMENT_TEST_DATABASE_URL only to an EMPTY disposable PostgreSQL
database. Explicit opt-in resets the metadata between tests; SQLite is default.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment import inventory as inv, model as m
from fulfillment.app import create_app
from fulfillment.erp import DemoERP
from fulfillment.service import Service
from fulfillment.domain import trace_key

ADMIN = {"id": "ADMIN", "role": "admin"}
SUP = {"id": "SALES", "role": "supervisor"}
OP = {"id": "PICKER", "role": "operator"}


@pytest.fixture
def system(tmp_path):
    url = os.getenv("FULFILLMENT_TEST_DATABASE_URL", "sqlite:///" + (tmp_path / "inventory.db").as_posix())
    db = m.Database(url)
    if os.getenv("FULFILLMENT_TEST_DATABASE_URL"):
        # Explicit opt-in variable points only at a disposable test database.
        m.metadata.drop_all(db.engine)
    db.initialize()
    erp = DemoERP()
    service = Service(db, erp)
    yield db, erp, service
    db.engine.dispose()


def call(service, action, payload, actor=SUP, request_id=None):
    return service.command(actor, "inventory:" + action, payload, request_id or str(uuid4()))


def ok(service, action, payload, actor=SUP):
    result, status = call(service, action, payload, actor)
    assert status == 200, result
    return result


def setup(service):
    return ok(service, "configure", dict(version=0, enabled=False, site="DEMO", warehouse="SHIPPING", location="SHIP", reason="Opening setup"), ADMIN)


def receipt(part="ACCESSORY-B", amount="10", serials=None, **extra):
    return dict(event_id=str(uuid4()), source="manual", site="DEMO", warehouse="SHIPPING", location="SHIP",
                part=part, uom="EA", quantity=amount, serials=serials or [], reason="Received from production", **extra)


def allocate(part="ACCESSORY-B", amount="3", **extra):
    return dict(source="stock", part=part, uom="EA", customer="DEMO-SUPPLY", quantity=amount,
                hold=True, reason="Inside sales request", **extra)


def state(db):
    with db.engine.connect() as conn:
        return inv.snapshot(conn)


def opening(service, balances):
    return ok(service, "reconcile", dict(event_id=str(uuid4()), source="manual", site="DEMO", warehouse="SHIPPING",
        location="SHIP", observed_at=m.now(), balances=balances))


def activate(service, balances):
    opening(service, balances)
    return ok(service, "configure", dict(version=1, enabled=True, site="DEMO", warehouse="SHIPPING", location="SHIP", reason="Opening count verified"), ADMIN)


def test_receipt_replays_across_request_keys_and_actors_without_double_count(system):
    db, _, service = system
    setup(service)
    payload = receipt()
    first = ok(service, "receive", payload)
    assert ok(service, "receive", payload, ADMIN) == first
    assert len(state(db)["ledger"]) == 1
    assert state(db)["balances"][0]["quantity"] == "10"
    assert call(service, "receive", {**payload, "quantity": "11"})[1] == 409
    assert call(service, "receive", {**receipt(), "warehouse": "WRONG"})[1] == 409


def test_receipt_serial_identity_uom_and_rollback(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt("ASSEMBLY-A", "2", ["ONE", "TWO"]))
    assert call(service, "receive", receipt("ASSEMBLY-A", "2", ["NEW", "ONE"]))[1] == 409
    assert {s["serial"] for s in state(db)["balances"]} == {"ONE", "TWO"}
    assert call(service, "receive", receipt("ASSEMBLY-A", "2", []))[1] == 409
    assert call(service, "receive", {**receipt("ASSEMBLY-A", "1", ["NEW"]), "uom": "BOX"})[1] == 409


def test_concurrent_allocations_cannot_oversell_and_roles_are_checked(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="5"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(service, "allocate", allocate(amount="4")), range(2)))
    assert sorted(s for _, s in results) == [200, 409]
    balance = state(db)["balances"][0]
    assert (balance["quantity"], balance["committed"], balance["available"]) == ("5", "4", "1")
    assert call(service, "allocate", allocate(amount="1"), OP)[1] == 403
    assert call(service, "configure", dict(), SUP)[1] == 403


def test_partial_work_order_receipt_preserves_customer_hold_and_shortfall(system):
    db, _, service = system
    setup(service)
    s = ok(service, "supply", dict(work_order="W:123:1:0:0", part="ASSEMBLY-A", uom="EA", serialized=True,
        quantity="5", due_date="2026-11-01", status="open", reason="Known released work order"))
    a = ok(service, "allocate", {**allocate("ASSEMBLY-A", "4"), "source": "work_order", "source_id": s["id"],
                                "review_at": "2020-01-01T00:00:00+00:00"})
    assert call(service, "allocate", {**allocate("ASSEMBLY-A", "2"), "source": "work_order", "source_id": s["id"]})[1] == 409
    ok(service, "receive", receipt("ASSEMBLY-A", "2", ["A", "B"], work_order_id=s["id"]))
    view = state(db)
    assert all(b["available"] == "0" for b in view["balances"])
    parent = next(v for v in view["allocations"] if v["id"] == a["id"])
    child = next(v for v in view["allocations"] if v.get("parent_id") == a["id"])
    assert parent["fulfilled"] == "2" and child["quantity"] == "2"
    assert child["hold"] and child["review_due"] and child["customer"] == "DEMO-SUPPLY"
    updated = ok(service, "supply", {**s, "version": 2, "quantity": "3", "reason": "Production reduced supply"})
    assert updated["shortfall"] == "1"
    assert len(state(db)["allocations"]) == 2


def test_hold_review_never_releases_and_revision_conflicts(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt())
    a = ok(service, "allocate", {**allocate(), "review_at": "2020-01-01T00:00:00+00:00"})
    assert state(db)["allocations"][0]["review_due"]
    assert state(db)["balances"][0]["available"] == "7"
    assert call(service, "allocation-update", dict(id=a["id"], version=0, hold=False, reason="Release for picking"))[1] == 409
    ok(service, "allocation-update", dict(id=a["id"], version=1, hold=False, reason="Release for picking"))
    assert state(db)["balances"][0]["available"] == "7"  # still belongs to customer


def test_order_allocation_validates_customer_and_line(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt())
    payload = {**allocate(), "order": "SO-DEMO-100", "order_line": "2"}
    assert call(service, "allocate", {**payload, "customer": "OTHER-CUSTOMER"})[1] == 409
    assert call(service, "allocate", {**payload, "order_line": "1"})[1] == 409
    assert not state(db)["allocations"]
    assert ok(service, "allocate", payload)["order_line"] == "2"


def test_reconciliation_is_evidence_and_activation_requires_matching_current_state(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="4"))
    config = dict(version=1, enabled=True, site="DEMO", warehouse="SHIPPING", location="SHIP", reason="Activate")
    assert call(service, "configure", config, ADMIN)[1] == 409
    variance = opening(service, [{"part": "ACCESSORY-B", "uom": "EA", "quantity": "3"}])
    assert variance["status"] == "variance" and state(db)["balances"][0]["quantity"] == "4"
    assert call(service, "configure", config, ADMIN)[1] == 409
    opening(service, [{"part": "ACCESSORY-B", "uom": "EA", "quantity": "4"}])
    ok(service, "receive", receipt(amount="1"))
    assert call(service, "configure", config, ADMIN)[1] == 409
    opening(service, [{"part": "ACCESSORY-B", "uom": "EA", "quantity": "5"}])
    assert ok(service, "configure", config, ADMIN)["enabled"]
    assert call(service, "configure", {**config, "version": 2, "enabled": False}, ADMIN)[1] == 409


def test_stock_adjustment_never_consumes_protected_or_checked_out_stock(system):
    db, _, service = system
    setup(service)
    ok(service, "receive", receipt(amount="10"))
    ok(service, "allocate", allocate(amount="8"))
    assert call(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-3", reason="Count loss"))[1] == 409
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-2", reason="Count loss"))
    assert state(db)["balances"][0]["quantity"] == "8"


def test_opening_stock_adopts_terminal_legacy_return_claim(system):
    db, _, service = system
    setup(service)
    with db.transaction() as conn:
        conn.execute(m.works.insert().values(id="old-work", state="released", version=1,
            document={"id": "old-work", "state": "released"}, updated_at=m.now()))
        conn.execute(m.claims.insert().values(key=trace_key("DEMO", "ASSEMBLY-A", "RETURNED"), work_id="old-work"))
    ok(service, "receive", receipt("ASSEMBLY-A", "1", ["RETURNED"]))
    activate(service, [{"part": "ASSEMBLY-A", "uom": "EA", "quantity": "1", "serials": ["RETURNED"]}])
    with db.engine.connect() as conn:
        assert conn.execute(select(m.claims)).first() is None
        assert conn.execute(select(m.works.c.state)).scalar() == "released"


def test_cannot_remap_and_activate_in_one_command(system):
    _, _, service = system
    setup(service)
    opening(service, [])
    result, status = call(service, "configure", dict(version=1, enabled=True, site="DEMO", warehouse="SHIPPING", location="OTHER", reason="New boundary"), ADMIN)
    assert status == 409 and result["error"] == "reconciliation"


def test_local_checkout_cancel_putaway_and_release_debit(system):
    db, erp, service = system
    setup(service)
    ok(service, "receive", receipt("ASSEMBLY-A", "2", ["LOCAL-A", "LOCAL-B"]))
    ok(service, "receive", receipt(amount="6"))
    activate(service, [{"part": "ASSEMBLY-A", "uom": "EA", "quantity": "2", "serials": ["LOCAL-A", "LOCAL-B"]},
                       {"part": "ACCESSORY-B", "uom": "EA", "quantity": "6"}])
    def command(action, work=None, payload=None):
        result, status = service.command(ADMIN, action, {**({"version": work["version"]} if work else {}), **(payload or {})}, str(uuid4()), work["id"] if work else None)
        assert status == 200, result
        return result
    work = command("checkout", payload={"order": "SO-DEMO-100", "cart": "C1"})
    assert work["inventory_owned"]
    assert all(b["available"] == "0" for b in state(db)["balances"])
    first = work["lines"][0]
    work = command("pick", work, {"line_id": first["id"], "location": "SHIP", **({"serial": first["serials"][0]} if first["serialized"] else {"item": first["part"], "quantity": first["quantity"]})})
    work = command("cancel", work, {"reason": "Return for replanning"})
    assert work["state"] == "putaway" and all(b["available"] == "0" for b in state(db)["balances"])
    work = command("putaway", work, {"line_id": first["id"], "location": "SHIP", **({"serial": first["serials"][0]} if first["serialized"] else {"item": first["part"], "quantity": first["quantity"]})})
    assert work["state"] == "cancelled"
    work = command("checkout", payload={"order": "SO-DEMO-100", "cart": "C1"})
    for line in list(work["lines"]):
        work = command("pick", work, {"line_id": line["id"], "location": "SHIP", **({"serial": line["serials"][0]} if line["serialized"] else {"item": line["part"], "quantity": line["quantity"]})})
    work = command("finish-pick", work)
    work = command("handoff", work, {"cart": "C1", "station": "PACK"})
    work = command("package", work, {"barcode": "BOX"})
    for line in list(work["lines"]):
        work = command("pack", work, {"line_id": line["id"], "package": "BOX", **({"serial": line["serials"][0]} if line["serialized"] else {"item": line["part"], "quantity": line["quantity"]})})
    erp.packlists["PL"] = dict(id="PL", site=work["site"], order=work["order"], status="A", destination=deepcopy(work["destination"]),
        lines=[dict(packlist_line=str(i), order=work["order"], order_line=l["order_line"], part=l["part"], uom=l["uom"], quantity=l["quantity"], serials=l["serials"]) for i, l in enumerate(work["lines"])])
    work = command("verify", work, {"packlist": "PL"})
    with db.transaction() as conn:
        service.ingest_label(conn, dict(tracking="TRACK", site=work["site"], order=work["order"], packlist="PL", package="BOX", destination=work["destination"], void=False, exported_at=m.now()))
    work = command("label", work, {"package": "BOX", "tracking": "TRACK"})
    request_id = str(uuid4())
    payload = {"version": work["version"]}
    first = service.command(ADMIN, "release", payload, request_id, work["id"])
    assert first[1] == 200, first
    assert service.command(ADMIN, "release", payload, request_id, work["id"]) == first
    assert all(b["quantity"] == "0" for b in state(db)["balances"])
    assert sum(1 for e in state(db)["ledger"] if e["kind"] == "shipment") == len(work["lines"])


def test_machine_feed_scope_auth_revision_and_replay(system):
    db, erp, service = system
    setup(service)
    app = create_app({"MODE": "demo", "DATABASE_URL": db.engine.url.render_as_string(hide_password=False), "TESTING": True, "INVENTORY_TOKEN": "i" * 40, "VALIDATE_TOKEN": "v" * 40}, erp=erp)
    client = app.test_client()
    headers = {"Authorization": "Bearer " + "i" * 40, "Idempotency-Key": str(uuid4())}
    payload = {**receipt(), "source": "visual"}
    assert client.post("/api/v1/integrations/inventory/receipts", json=payload).status_code == 401
    assert client.post("/api/v1/integrations/inventory/receipts", json=payload, headers={**headers, "Authorization": "Bearer " + "v" * 40}).status_code == 401
    assert client.post("/api/v1/integrations/inventory/receipts", json=payload, headers=headers).status_code == 200
    supply = dict(site="DEMO", warehouse="SHIPPING", location="SHIP", source="visual", event_id="WO-V2", source_revision=2,
        work_order="W:1:1:0:0", part="P", uom="EA", serialized=False, quantity="10", status="open", reason="Source sync")
    def post(value):
        return client.post("/api/v1/integrations/inventory/work-orders", json=value, headers={**headers, "Idempotency-Key": str(uuid4())})
    assert post(supply).status_code == 200
    assert post(supply).status_code == 200
    assert post({**supply, "event_id": "WO-V1", "source_revision": 1}).status_code == 409
    assert post({**supply, "event_id": "WO-V3", "source_revision": 3, "site": "OTHER"}).status_code == 409


def test_api_inventory_requires_session_csrf_and_supervisor(system):
    db, erp, _ = system
    with db.transaction() as conn:
        conn.execute(m.users.insert().values(id="PICKER", name="Picker", role="operator", pin_hash=generate_password_hash("123456"), enabled=True))
    app = create_app({"MODE": "demo", "DATABASE_URL": db.engine.url.render_as_string(hide_password=False), "TESTING": True}, erp=erp)
    client = app.test_client()
    assert client.get("/api/v1/inventory").status_code == 401
    assert client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"}).status_code == 200
    csrf = client.get("/api/v1/auth/me").json["csrf"]
    assert client.get("/api/v1/inventory").status_code == 200
    assert client.post("/api/v1/inventory/receive", json=receipt()).status_code == 403
    assert client.post("/api/v1/inventory/receive", json=receipt(), headers={"X-CSRF-Token": csrf, "Idempotency-Key": "operator"}).status_code == 403
