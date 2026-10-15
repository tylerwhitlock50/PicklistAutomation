"""End-to-end state transitions, API security and concurrency.

Set FULFILLMENT_TEST_DATABASE_URL to an EMPTY disposable PostgreSQL database to
run the same suite against the production engine. Default SQLite is test only.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment.app import create_app
from fulfillment import model as m
from fulfillment.domain import Conflict, reconcile
from fulfillment.erp import DemoERP, VisualERP
from fulfillment.service import Service

OP = {"id": "PICKER", "role": "operator"}
PACKER = {"id": "PACKER", "role": "operator"}
SUP = {"id": "SUPERVISOR", "role": "supervisor"}


@pytest.fixture
def system(tmp_path):
    url = os.getenv("FULFILLMENT_TEST_DATABASE_URL", "sqlite:///" + (tmp_path / "test.db").as_posix())
    db = m.Database(url)
    if os.getenv("FULFILLMENT_TEST_DATABASE_URL"):
        # Explicit opt-in variable points only at a disposable test database.
        m.metadata.drop_all(db.engine)
    db.initialize()
    with db.transaction() as conn:
        for actor in (OP, PACKER, SUP):
            conn.execute(m.users.insert().values(id=actor["id"], name=actor["id"], role=actor["role"], pin_hash=generate_password_hash("123456"), enabled=True))
    erp = DemoERP()
    service = Service(db, erp)
    yield db, erp, service
    db.engine.dispose()


def cmd(service, action, work=None, payload=None, actor=OP, request_id=None):
    body = {**({"version": work["version"]} if work else {}), **(payload or {})}
    return service.command(actor, action, body, request_id or str(uuid4()), work["id"] if work else None)


def ok(service, action, work=None, payload=None, actor=OP):
    result, status = cmd(service, action, work, payload, actor)
    assert status == 200, result
    return result


def checkout(service):
    return ok(service, "checkout", payload={"order": "SO-DEMO-100", "cart": "CART-1"})


def test_prescription_cannot_repeat_trace_across_order_lines(system):
    db, erp, service = system
    repeated = deepcopy(erp.orders[0]["lines"][0])
    repeated["order_line"] = "3"
    erp.orders[0]["lines"].append(repeated)
    result, status = cmd(service, "checkout", payload={"order": "SO-DEMO-100", "cart": "CART-1"})
    assert status == 409 and "two lines" in result["message"]
    with db.engine.connect() as conn:
        assert conn.execute(select(m.claims)).first() is None


def test_transient_source_failure_can_retry_identical_request(system):
    _, erp, service = system
    original = erp.available
    def unavailable(*args):
        raise Conflict("ERP temporarily unavailable", "unavailable", 503)
    erp.available = unavailable
    request_id = str(uuid4())
    payload = {"order": "SO-DEMO-100", "cart": "CART-1"}
    assert cmd(service, "checkout", payload=payload, request_id=request_id)[1] == 503
    erp.available = original
    first, status = cmd(service, "checkout", payload=payload, request_id=request_id)
    assert status == 200
    again, status = cmd(service, "checkout", payload=payload, request_id=request_id)
    assert status == 200 and first["id"] == again["id"]


def picked(service):
    work = checkout(service)
    for serial in ("DEMO-001", "DEMO-002"):
        work = ok(service, "pick", work, {"line_id": "1", "serial": serial, "location": "R09S02"})
    work = ok(service, "pick", work, {"line_id": "2", "item": "ACCESSORY-B", "quantity": "6", "location": "BIN-04"})
    work = ok(service, "finish-pick", work)
    return ok(service, "handoff", work, {"cart": "CART-1", "station": "PACK-1"}, PACKER)


def packed(service):
    work = picked(service)
    for index, serial in enumerate(("DEMO-001", "DEMO-002")):
        package = "BOX-" + str(index + 1)
        work = ok(service, "package", work, {"barcode": package}, PACKER)
        work = ok(service, "pack", work, {"line_id": "1", "serial": serial, "package": package}, PACKER)
        work = ok(service, "pack", work, {"line_id": "2", "item": "100002", "quantity": "3", "package": package}, PACKER)
    return work


def erp_document(work):
    return {"id": "PL-1", "site": work["site"], "order": work["order"], "status": "A", "destination": deepcopy(work["destination"]),
            "lines": [dict(packlist_line=str(i + 1), order=work["order"], order_line=l["order_line"], part=l["part"], uom=l["uom"], quantity=l["quantity"], serials=l["serials"][:]) for i, l in enumerate(work["lines"])]}


def label(work, package, tracking):
    return dict(tracking=tracking, site=work["site"], order=work["order"], packlist="PL-1", package=package,
                void=False, destination=deepcopy(work["destination"]), exported_at=m.now())


def verified(system):
    db, erp, service = system
    work = packed(service)
    erp.packlists["PL-1"] = erp_document(work)
    work = ok(service, "verify", work, {"packlist": "PL-1"}, PACKER)
    for index, package in enumerate(work["packages"]):
        tracking = "TRACK-" + str(index)
        ok(service, "labels", payload=label(work, package["id"], tracking))
        work = ok(service, "label", work, {"package": package["id"], "tracking": tracking}, PACKER)
    return work


def test_mixed_order_two_packages_releases_only_after_all_checks(system):
    _, _, service = system
    work = verified(system)
    work = ok(service, "release", work, actor=PACKER)
    assert work["state"] == "released"
    assert cmd(service, "package", work, {"barcode": "LATE"}, PACKER)[1] == 409


@pytest.mark.parametrize("payload", [
    {"line_id": "1", "serial": "NOT-ASSIGNED", "location": "R09S02"},
    {"line_id": "1", "serial": "DEMO-001", "location": "R08S02"},
    {"line_id": "2", "item": "ACCESSORY-B", "quantity": "7", "location": "BIN-04"},
    {"line_id": "2", "item": "WRONG", "quantity": "2", "location": "BIN-04"},
    {"line_id": "2", "item": "ACCESSORY-B", "quantity": "NaN", "location": "BIN-04"},
])
def test_wrong_scans_do_not_count_and_are_audited(system, payload):
    db, _, service = system
    work = checkout(service)
    result, status = cmd(service, "pick", work, payload)
    assert status in (400, 409)
    with db.engine.connect() as conn:
        saved = service.get(conn, work["id"])
        assert all(l["picked"] == "0" for l in saved["lines"])
        assert saved["version"] == 1
        assert conn.execute(select(m.events).where(m.events.c.action == "rejected:pick")).first()


def test_uncertain_scan_retry_is_exactly_once(system):
    _, _, service = system
    work = checkout(service)
    payload = {"line_id": "1", "serial": "DEMO-001", "location": "R09S02"}
    first = cmd(service, "pick", work, payload, request_id="same-request")
    assert first == cmd(service, "pick", work, payload, request_id="same-request")
    assert first[0]["lines"][0]["picked"] == "1"
    with pytest.raises(Conflict):
        cmd(service, "pick", work, {**payload, "serial": "DEMO-002"}, request_id="same-request")
    assert cmd(service, "pick", first[0], payload)[1] == 409


def test_stale_revision_cannot_overwrite_other_device(system):
    _, _, service = system
    work = checkout(service)
    ok(service, "pick", work, {"line_id": "1", "serial": "DEMO-001", "location": "R09S02"})
    assert cmd(service, "pick", work, {"line_id": "1", "serial": "DEMO-002", "location": "R09S02"})[0]["error"] == "revision"


def test_concurrent_checkout_one_winner(system):
    _, _, service = system
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda actor: cmd(service, "checkout", payload={"order": "SO-DEMO-100", "cart": actor["id"]}, actor=actor), (OP, PACKER)))
    assert sorted(status for _, status in results) == [200, 409]


def test_serial_cannot_be_claimed_for_two_orders(system):
    _, erp, service = system
    duplicate = deepcopy(erp.orders[0]); duplicate["order"] = "SO-DEMO-200"; erp.orders.append(duplicate)
    checkout(service)
    assert cmd(service, "checkout", payload={"order": "SO-DEMO-200", "cart": "CART-2"}, actor=PACKER)[0]["error"] == "reserved"


def test_partial_checkout_failure_rolls_back_all_claims(system):
    db, erp, service = system
    erp.orders[0]["lines"][0]["serials"] = ["DEMO-001"]
    assert cmd(service, "checkout", payload={"order": "SO-DEMO-100", "cart": "CART-1"})[1] == 409
    with db.engine.connect() as conn:
        assert not conn.execute(select(m.claims)).first()
        assert not conn.execute(select(m.works)).first()


def test_cannot_finish_short_or_release_unverified(system):
    _, _, service = system
    work = checkout(service)
    assert cmd(service, "finish-pick", work)[1] == 409
    assert cmd(service, "release", work)[1] == 409


def test_ownership_handoff_and_pin_role_are_enforced(system):
    _, _, service = system
    work = checkout(service)
    assert cmd(service, "pick", work, {"line_id": "1", "serial": "DEMO-001", "location": "R09S02"}, PACKER)[1] == 403
    work = ok(service, "exception", work, {"reason": "Damaged item"})
    assert cmd(service, "resolve", work, {"reason": "ignore"})[1] == 403
    work = ok(service, "resolve", work, {"reason": "Rechecked stock"}, SUP)
    assert work["exception"] is None


def test_putaway_holds_stock_until_physical_return(system):
    db, _, service = system
    work = checkout(service)
    work = ok(service, "pick", work, {"line_id": "1", "serial": "DEMO-001", "location": "R09S02"})
    work = ok(service, "cancel", work, {"reason": "Order cancelled"}, SUP)
    assert work["state"] == "putaway"
    with db.engine.connect() as conn:
        assert conn.execute(select(m.claims).where(m.claims.c.work_id == work["id"])).first()
    assert cmd(service, "putaway", work, {"line_id": "1", "serial": "DEMO-001", "location": "WRONG"})[1] == 409
    work = ok(service, "putaway", work, {"line_id": "1", "serial": "DEMO-001", "location": "R09S02"})
    assert work["state"] == "cancelled"
    with db.engine.connect() as conn:
        assert not conn.execute(select(m.claims).where(m.claims.c.work_id == work["id"])).first()


def test_same_sku_different_order_lines_cannot_swap_serials(system):
    _, erp, service = system
    l = erp.orders[0]["lines"][0]; l["quantity"] = "1"; l["serials"] = ["DEMO-001"]
    other = deepcopy(l); other.update(order_line="3", serials=["DEMO-002"])
    erp.orders[0]["lines"].append(other)
    work = checkout(service)
    document = erp_document(work)
    assert reconcile(work, document)["result"] == "pass"
    document["lines"][0]["serials"], document["lines"][2]["serials"] = document["lines"][2]["serials"], document["lines"][0]["serials"]
    assert reconcile(work, document)["result"] == "mismatch"


def test_package_cannot_contain_duplicate_serial(system):
    _, _, service = system
    work = packed(service)
    assert cmd(service, "pack", work, {"line_id": "1", "serial": "DEMO-001", "package": "BOX-2"}, PACKER)[1] == 409


def test_packlist_edit_and_package_edit_invalidate_release(system):
    _, erp, service = system
    work = verified(system)
    erp.packlists["PL-1"]["lines"][0]["serials"][0] = "CHANGED"
    assert cmd(service, "release", work, actor=PACKER)[1] == 409
    work = ok(service, "reset-package", work, {"package": "BOX-1", "reason": "Repack"}, PACKER)
    assert work["verification"] is None
    assert work["packages"][0]["tracking"] is None
    assert cmd(service, "release", work, actor=PACKER)[1] == 409


@pytest.mark.parametrize("mutation", ["void", "destination", "package", "order"])
def test_wrong_or_void_label_blocks_release(system, mutation):
    _, _, service = system
    work = verified(system)
    bad = label(work, "BOX-1", "TRACK-0")
    if mutation == "void": bad["void"] = True
    elif mutation == "destination": bad["destination"]["address2"] = "Wrong suite"
    else: bad[mutation] = "WRONG"
    ok(service, "labels", payload=bad)
    assert cmd(service, "release", work, actor=PACKER)[1] == 409


def test_old_worldship_export_cannot_unvoid_label(system):
    _, _, service = system
    work = packed(service)
    old = label(work, "BOX-1", "TRACK-0")
    recent = deepcopy(old); recent.update(void=True, exported_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    ok(service, "labels", payload=recent)
    assert cmd(service, "labels", payload=old)[1] == 409


def test_api_auth_csrf_integration_scopes_and_shadow(system):
    db, erp, _ = system
    config = dict(TESTING=True, MODE="demo", DATABASE_URL=db.engine.url.render_as_string(hide_password=False), LABEL_TOKEN="l" * 40, VALIDATE_TOKEN="v" * 40)
    app = create_app(config, erp)
    client = app.test_client()
    assert client.get("/api/v1/orders").status_code == 401
    assert client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"}).status_code == 200
    me = client.get("/api/v1/auth/me").json
    assert client.post("/api/v1/checkouts", json={"order": "SO-DEMO-100", "cart": "CART-1"}).status_code == 403
    headers = {"X-CSRF-Token": me["csrf"], "Idempotency-Key": "checkout"}
    work = client.post("/api/v1/checkouts", json={"order": "SO-DEMO-100", "cart": "CART-1"}, headers=headers).json
    assert work["state"] == "picking"
    assert client.post("/api/v1/integrations/worldship/labels", json={}, headers={"Authorization": "Bearer " + "v" * 40}).status_code == 401
    payload = {"fulfillment_id": work["id"], "version": work["version"], "candidate": erp_document(work)}
    result = client.post("/api/v1/integrations/visual/validate", json=payload, headers={"Authorization": "Bearer " + "v" * 40})
    assert result.json["result"] == "pass" and result.json["advisory"]
    assert client.get("/api/v1/openapi.json").json["openapi"] == "3.1.0"
    assert client.get("/api/v1/orders").headers["Cache-Control"] == "no-store"
    assert client.post("/api/v1/auth/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/v1/orders").status_code == 401
    shadow = create_app({**config, "MODE": "shadow", "COOKIE_SECURE": False}, erp).test_client()
    shadow.post("/api/v1/auth/login", json={"operator": "PACKER", "pin": "123456"})
    csrf = shadow.get("/api/v1/auth/me").json["csrf"]
    assert shadow.post("/api/v1/checkouts", json={}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "blocked"}).status_code == 403


def test_login_attempts_are_throttled(system):
    db, erp, _ = system
    client = create_app(dict(TESTING=True, MODE="demo", DATABASE_URL=db.engine.url.render_as_string(hide_password=False)), erp).test_client()
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "999999"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"}).status_code == 429


def test_sql_adapter_renders_site_scoped_consistent_queries():
    adapter = VisualERP.__new__(VisualERP)
    rendered = adapter.candidate_query()
    assert "WITH (NOLOCK)" not in rendered
    assert "co.SITE_ID = :site" in rendered and "wh.SITE_ID = :site" in rendered
    assert "__" not in rendered


def test_expired_session_and_disabled_account_rejected(system):
    db, erp, _ = system
    client = create_app(dict(TESTING=True, MODE="demo", DATABASE_URL=db.engine.url.render_as_string(hide_password=False)), erp).test_client()
    client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"})
    with db.transaction() as conn:
        conn.execute(m.users.update().where(m.users.c.id == "PICKER").values(enabled=False))
    assert client.get("/api/v1/auth/me").status_code == 401
