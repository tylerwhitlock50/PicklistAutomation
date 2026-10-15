from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash, check_password_hash

from fulfillment import admin, model as m
from fulfillment.app import create_app
from fulfillment.domain import Conflict
from fulfillment.erp import VisualERP
from fulfillment.migration import legacy_snapshot, import_legacy
from test_fulfillment import system, OP, PACKER, SUP, cmd, checkout, picked, verified, ok
from test_fulfillment_integrations import source_fixture

ADMIN = {"id": "ADMIN", "role": "admin"}


@pytest.fixture
def setup(system):
    db, erp, service = system
    with db.transaction() as conn:
        conn.execute(m.users.insert().values(id="ADMIN", name="Admin", role="admin", enabled=True, pin_hash=generate_password_hash("654321")))
    return db, erp, service


def state(db):
    with db.engine.connect() as conn:
        return admin.load(conn)


def change(setup, action, values=None, actor=ADMIN, request_id=None, version=None):
    db, _, service = setup
    payload = {"version": state(db)["version"] if version is None else version, "reason": "Warehouse test", **(values or {})}
    return cmd(service, "admin:" + action, payload=payload, actor=actor, request_id=request_id)


def publish(setup, rules):
    result, status = change(setup, "preview", {"rules": rules})
    assert status == 200, result
    result, status = change(setup, "rules", {"rules": rules, "preview": result["preview"]})
    assert status == 200, result
    return result


def future():
    return (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


def test_admin_required_and_rules_need_exact_current_preview(setup):
    db, _, _ = setup
    rules = state(db)["rules"]
    for actor in (OP, SUP):
        assert change(setup, "preview", {"rules": rules}, actor=actor)[1] == 403
    assert change(setup, "rules", {"rules": rules})[1] == 409
    preview, status = change(setup, "preview", {"rules": rules})
    assert status == 200 and not state(db)["configured"]
    rules["cooldown"] += 1
    assert change(setup, "rules", {"rules": rules, "preview": preview["preview"]})[1] == 409
    publish(setup, rules)
    assert state(db)["rules"]["cooldown"] == 2
    assert change(setup, "control", {"order": "SO-DEMO-100", "kind": "hold"}, version=1)[1] == 409


@pytest.mark.parametrize("field,value", [("cooldown", -1), ("due_days", True), ("lookahead_days", 2.5), ("timezone", "Invalid/Zone"), ("document_checks_required", "false"), ("policies", {"X": {"daily_release_time": "25:00"}}), ("policies", {"X": {"bypass_credit": True}})])
def test_invalid_rules_rejected_before_any_change(setup, field, value):
    db, _, _ = setup
    rules = state(db)["rules"]
    rules[field] = value
    assert change(setup, "preview", {"rules": rules})[1] == 400
    assert state(db)["version"] == 1


def test_hold_beats_early_release_and_revocation_is_audited(setup):
    db, erp, service = setup
    assert change(setup, "control", {"order": "SO-DEMO-100", "kind": "hold"})[1] == 200
    assert change(setup, "control", {"order": "SO-DEMO-100", "kind": "early_release", "expires_at": future()})[1] == 200
    with db.engine.connect() as conn:
        assert service.orders(conn)["orders"] == []
    hold = state(db)["controls"][0]
    assert change(setup, "revoke", {"id": hold["id"]})[1] == 200
    work = checkout(service)
    assert work["order"] == "SO-DEMO-100"
    exception = state(db)["controls"][1]
    assert exception["consumed_by"] == work["id"] and not admin.active(exception)
    with db.engine.connect() as conn:
        assert conn.execute(select(m.events).where(m.events.c.action == "admin:change")).first()


def test_early_release_changes_policy_eligibility_without_changing_rules(setup, monkeypatch):
    db, erp, service = setup
    import fulfillment.erp as module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 15, 16, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)
    rules = state(db)["rules"]
    rules["policies"] = {"DEFAULT": {"account_type": "standard", "accumulate": True, "release_cadence": "daily", "daily_release_time": "23:59", "mix_orders": True}}
    publish(setup, rules)
    with db.engine.connect() as conn:
        assert service.orders(conn)["orders"] == []
    assert change(setup, "control", {"order": "SO-DEMO-100", "kind": "early_release", "expires_at": future()})[1] == 200
    with db.engine.connect() as conn:
        assert len(service.orders(conn)["orders"]) == 1
    assert state(db)["rules"] == admin.validate_rules(rules)


def test_expired_exception_not_effective_and_no_expiry_refused(setup):
    db, _, _ = setup
    assert change(setup, "control", {"order": "SO-DEMO-100", "kind": "early_release"})[1] == 400
    control = {"id": "old", "kind": "early_release", "order": "SO-DEMO-100", "expires_at": "2000-01-01T00:00:00+00:00"}
    with db.transaction() as conn:
        s = admin.load(conn); s["controls"].append(control); m.put_setting(conn, "administration", s)
        assert admin.context(conn)["exceptions"] == []


def test_clear_assignments_holds_order_and_cannot_touch_active_work(setup):
    db, _, service = setup
    with db.transaction() as conn:
        m.put_setting(conn, "reservations", [{"cust_order_id": "SO-DEMO-100", "serial_no": "DEMO-001", "part_id": "ASSEMBLY-A", "line_no": "1"}])
    assert change(setup, "unassign", {"order": "SO-DEMO-100"})[1] == 200
    with db.engine.connect() as conn:
        assert m.get_setting(conn, "reservations") == []
        assert service.orders(conn)["orders"] == []
    assert change(setup, "revoke", {"id": state(db)["controls"][0]["id"]})[1] == 200
    work = checkout(service)
    assert change(setup, "unassign", {"order": work["order"]})[1] == 409
    with db.engine.connect() as conn:
        assert service.get(conn, work["id"])["lines"] == work["lines"]


def test_hold_added_during_picking_blocks_pick_and_final_release(setup):
    db, _, service = setup
    work = checkout(service)
    assert change(setup, "control", {"order": work["order"], "kind": "hold"})[1] == 200
    assert cmd(service, "pick", work, {"line_id": "1", "location": "R09S02", "serial": "DEMO-001"})[1] == 409
    assert change(setup, "revoke", {"id": state(db)["controls"][0]["id"]})[1] == 200
    # An administrator has supervisor correction rights, but no mismatch bypass.
    assert cmd(service, "release", work, actor=ADMIN)[1] == 409
    assert cmd(service, "cancel", work, {"reason": "Cancelled before any picks"}, ADMIN)[1] == 200


def test_document_review_is_tied_to_current_destination_and_expiration(setup):
    db, erp, service = setup
    rules = state(db)["rules"]; rules["document_checks_required"] = True
    publish(setup, rules)
    with db.engine.connect() as conn:
        assert service.orders(conn)["orders"] == []
    info = erp.inspect_order("SO-DEMO-100")
    values = {"order": info["order"], "kind": "document_review", "decision": "pass", "reference": "Training document review", "expires_at": future(), "fingerprint": info["document_fingerprint"]}
    assert change(setup, "control", {**values, "fingerprint": "stale"})[1] == 409
    assert change(setup, "control", values)[1] == 200
    with db.engine.connect() as conn:
        assert len(service.orders(conn)["orders"]) == 1
    erp.orders[0]["destination"]["address1"] = "200 Changed destination"
    with db.engine.connect() as conn:
        assert service.orders(conn)["orders"] == []


def test_rules_preview_and_publish_preserve_checked_out_assignment(setup):
    db, _, service = setup
    work = checkout(service)
    rules = state(db)["rules"]; rules["cooldown"] = 3
    publish(setup, rules)
    with db.engine.connect() as conn:
        assert service.get(conn, work["id"]) == work


def test_accounts_revoke_sessions_protect_last_admin_and_never_store_plain_pin(setup):
    db, _, _ = setup
    values = {"operator": "PACKER", "name": "Pack station", "role": "supervisor", "enabled": True, "pin": "874239"}
    with db.transaction() as conn:
        conn.execute(m.sessions.insert().values(token_hash="test-session", user_id="PACKER", expires=future()))
    version = state(db)["version"]; request_id = str(uuid4())
    first = change(setup, "account", values, request_id=request_id, version=version)
    assert first[1] == 200
    assert change(setup, "account", values, request_id=request_id, version=version) == first
    with pytest.raises(Conflict, match="reused"):
        change(setup, "account", {**values, "pin": "123987"}, request_id=request_id, version=version)
    with db.engine.connect() as conn:
        row = conn.execute(select(m.users).where(m.users.c.id == "PACKER")).first()
        assert check_password_hash(row.pin_hash, "874239")
        assert conn.execute(select(m.sessions).where(m.sessions.c.user_id == "PACKER")).first() is None
        for table in (m.events, m.requests, m.settings):
            assert "874239" not in json.dumps([dict(r) for r in conn.execute(select(table)).mappings()], default=str)
    assert change(setup, "account", {"operator": "ADMIN", "name": "Admin", "role": "operator", "enabled": True})[1] == 409


def test_admin_api_role_csrf_shadow_and_pin_security(setup):
    db, erp, _ = setup
    app = create_app(dict(TESTING=True, MODE="shadow", COOKIE_SECURE=False, DATABASE_URL=db.engine.url.render_as_string(hide_password=False)), erp)
    client = app.test_client()
    client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"})
    assert client.get("/api/v1/admin/state").status_code == 403
    client.post("/api/v1/auth/login", json={"operator": "ADMIN", "pin": "654321"})
    data = client.get("/api/v1/admin/state").json
    assert "pin_hash" not in json.dumps(data)
    assert client.post("/api/v1/admin/control", json={}).status_code == 403
    csrf = client.get("/api/v1/auth/me").json["csrf"]
    response = client.post("/api/v1/admin/control", json={"version": data["version"], "order": "SO-DEMO-100", "kind": "hold", "reason": "Shadow configuration test"}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "shadow-hold"})
    assert response.status_code == 200
    assert client.post("/api/v1/checkouts", json={}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "shadow-pick"}).status_code == 403


def test_schema_one_upgrade_preserves_work_and_requires_configuration_review(setup):
    db, _, service = setup
    work = checkout(service)
    with db.transaction() as conn:
        conn.execute(m.schema.update().values(version=1))
        conn.execute(m.settings.delete().where(m.settings.c.key == "administration"))
    db.initialize()
    with db.engine.connect() as conn:
        assert service.get(conn, work["id"]) == work
        assert conn.execute(select(m.schema.c.version)).scalar() == m.SCHEMA_VERSION
        assert not admin.load(conn)["configured"]


def test_source_uses_own_rule_settings_for_sql_and_context(setup):
    db, _, _ = setup
    adapter = VisualERP.__new__(VisualERP); adapter.database = db
    with db.transaction() as conn:
        s = admin.load(conn); s["rules"].update(lookahead_days=23, component_codes=["OWN-COMP"], excluded_customers=["O'BRIEN"]); m.put_setting(conn, "administration", s)
    rendered = adapter.candidate_query(adapter.context())
    assert "23" in rendered and "OWN-COMP" in rendered and "O''BRIEN" in rendered
    assert "WITH (NOLOCK)" not in rendered


def test_import_is_atomic_one_time_and_preserves_holds(setup):
    db, _, _ = setup
    erp, *_ = source_fixture()
    data = {"rules": deepcopy(admin.DEFAULT_RULES), "controls": [{"id": "import", "kind": "hold", "order": "SO-1", "reason": "Imported mismatch", "expires_at": None}], "unfinished": 0,
            "reservations": [{"cust_order_id": "SO-1", "line_no": "1", "part_id": "PART-1", "serial_no": "SN-1"}]}
    with db.transaction() as conn:
        import_legacy(conn, data, erp, "test")
    assert not state(db)["configured"] and state(db)["controls"][0]["reason"] == "Imported mismatch"
    with pytest.raises(Conflict, match="fresh"):
        with db.transaction() as conn: import_legacy(conn, data, erp, "test")


def test_legacy_reader_does_not_write_or_drop_document_holds(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as source:
        source.executescript("""CREATE TABLE settings(key TEXT,value TEXT);
        INSERT INTO settings VALUES('readiness_ocr_enabled','true');
        CREATE TABLE pick_orders(status TEXT); CREATE TABLE verify_sessions(status TEXT);
        CREATE TABLE release_gate_serial_reservations(status TEXT);
        CREATE TABLE release_gate_exceptions(revoked_at TEXT);
        CREATE TABLE manual_order_holds(released_at TEXT);
        CREATE TABLE readiness_holds(id INTEGER,cust_order_id TEXT,cleared_at TEXT,blocking INTEGER,reason_code TEXT);
        INSERT INTO readiness_holds VALUES(1,'SO-1',NULL,1,'ship_to_vs_ffl_premise_mismatch');""")
    before = path.read_bytes()
    data = legacy_snapshot(path)
    assert path.read_bytes() == before
    assert data["rules"]["document_checks_required"] is True
    assert data["controls"][0]["kind"] == "hold"


def test_deployment_has_no_legacy_runtime_imports_or_mounts():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "fulfillment").rglob("*.py"):
        assert "from picklist" not in path.read_text(encoding="utf-8")
        assert "import picklist" not in path.read_text(encoding="utf-8")
    docker = (root / "Dockerfile.fulfillment").read_text()
    assert "COPY picklist" not in docker and "COPY sql" not in docker
    compose = (root / "docker-compose.fulfillment.yml").read_text()
    assert "/legacy" not in compose and "FULFILLMENT_LEGACY_DB" not in compose


def test_worker_requires_configuration_and_cutover_and_retains_stock_on_failure(setup):
    db, _, service = setup
    from fulfillment.worker import tick
    assert tick(service, "DEMO", "demo") is False
    publish(setup, state(db)["rules"])
    assert tick(service, "DEMO", "shadow") is False
    with pytest.raises(Conflict, match="exclusive"):
        tick(service, "DEMO", "live")
    assert tick(service, "DEMO", "demo") is True
    with db.engine.connect() as conn:
        assert m.get_setting(conn, "allocation_worker")["eligible_orders"] == 1
    def fail(*args):
        raise Conflict("ERP unavailable", "unavailable", 503)
    with db.transaction() as conn:
        m.put_setting(conn, "reservations", [{"serial_no": "protected"}])
    service.erp.available = fail
    with pytest.raises(Conflict):
        tick(service, "DEMO", "demo")
    with db.engine.connect() as conn:
        assert m.get_setting(conn, "reservations") == [{"serial_no": "protected"}]


def test_admin_cannot_disable_operator_with_active_work(setup):
    _, _, service = setup
    checkout(service)
    response, status = change(setup, "account", {"operator": "PICKER", "name": "Picker", "role": "operator", "enabled": False})
    assert status == 409 and "Transfer" in response["message"]


def test_admin_cancellation_of_picked_stock_keeps_claims_until_putaway(setup):
    db, _, service = setup
    work = picked(service)
    cancelled = ok(service, "cancel", work, {"reason": "Return the cart"}, ADMIN)
    assert cancelled["state"] == "putaway"
    with db.engine.connect() as conn:
        assert conn.execute(select(m.claims).where(m.claims.c.work_id == work["id"])).first()
    assert change(setup, "unassign", {"order": work["order"]})[1] == 409


def test_live_readiness_still_blocks_early_release_and_requires_current_review():
    from test_readiness import row
    erp, _, detail, *_ = source_fixture()
    del erp.blocking  # Exercise the real readiness and admin checks, not the fixture stub.
    detail.update(row(PART_ID="PART-1", ORDER_QTY=2, OPEN_QTY=2, CUSTOMER_ID="CUSTOMER"))
    ctx = erp.context()
    ctx["exceptions"] = [{"cust_order_id": "SO-1", "expires_at": future()}]
    assert erp.available([], [], ctx)["orders"]
    ctx["document_checks_required"] = True
    assert erp.available([], [], ctx)["orders"] == []
    ctx["document_reviews"] = {"SO-1": {"decision": "pass", "fingerprint": erp.document_fingerprint([detail]), "expires_at": future()}}
    assert erp.available([], [], ctx)["orders"]
    detail["CREDIT_STATUS"] = "H"
    assert erp.available([], [], ctx)["orders"] == []
    detail["CREDIT_STATUS"] = "A"
    ctx["holds"] = [{"cust_order_id": "SO-1", "expires_at": None, "hold_kind": "admin"}]
    assert erp.available([], [], ctx)["orders"] == []


def test_worker_persists_own_sticky_serials_and_line_identity(setup):
    db, _, _ = setup
    from fulfillment.service import Service
    from fulfillment.worker import tick
    erp, *_ = source_fixture()
    service = Service(db, erp)
    with db.transaction() as conn:
        config = admin.load(conn)
        config["configured"] = True
        config["rules"]["policies"] = {"CUSTOMER": {"accumulate": True, "target_guns": 2}}
        m.put_setting(conn, "administration", config)
    assert tick(service, "TEST", "demo")
    with db.engine.connect() as conn:
        before = m.get_setting(conn, "reservations")
    assert {(r["cust_order_id"], str(r["line_no"]), r["serial_no"]) for r in before} == {("SO-1", "1", "SN-1"), ("SO-1", "1", "SN-2")}
    assert tick(service, "TEST", "demo")
    with db.engine.connect() as conn:
        after = m.get_setting(conn, "reservations")
    assert [{k: v for k, v in r.items() if k != "existing"} for r in after] == [{k: v for k, v in r.items() if k != "existing"} for r in before]
    assert all(r["existing"] for r in after)
