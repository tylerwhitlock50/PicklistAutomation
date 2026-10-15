"""The cloned audit workflow against an explicitly disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from fulfillment import model as m
from fulfillment.app import create_app
from fulfillment.audit import runtime, service, store
from test_fulfillment import system


@pytest.fixture
def audit(system):
    db, erp, _ = system
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Inventory audit parity requires disposable PostgreSQL")
    with db.transaction() as conn:
        conn.execute(text("TRUNCATE dispatch_audit_expected, dispatch_audit_scans, dispatch_audit_resolutions, dispatch_audit_sessions, dispatch_audit_locations RESTART IDENTITY CASCADE"))
        store.initialize(conn)
        conn.execute(m.users.insert().values(id="ADMIN", name="Admin", role="admin", enabled=True, pin_hash=generate_password_hash("123456")))
    app = create_app({"TESTING": True, "MODE": "demo", "DATABASE_URL": str(db.engine.url.render_as_string(hide_password=False))}, erp=erp)
    client = app.test_client()
    assert client.post("/api/v1/auth/login", json={"operator": "ADMIN", "pin": "123456"}).status_code == 200
    csrf = client.get("/api/v1/auth/me").json["csrf"]
    def post(path, payload=None, key=None):
        return client.post(path, json=payload or {}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": key or str(uuid4())})
    assert client.get("/api/audit/dashboard").status_code == 200
    yield app, client, post, db, csrf
    app.extensions["fulfillment_db"].engine.dispose()


def start(audit, kind="location", warehouse="SHIPPING", location="R09S02", key=None):
    result = audit[2]("/api/audit/sessions", {"target_kind": kind, "warehouse": warehouse, "location": location, "operator": "SPOOFED"}, key)
    assert result.status_code == 200, result.json
    return result.json["id"]


def action(audit, sid, name, payload=None, key=None):
    return audit[2](f"/api/audit/session/{sid}/{name}", payload, key)


def test_audit_clone_scopes_scans_duplicates_near_matches_and_completion(audit):
    sid = start(audit)
    one = action(audit, sid, "scan", {"serial": "DEMO-001", "location": "R09S02", "operator": "SPOOFED"}, "scan-one")
    assert one.status_code == 200 and one.json["result"] == "verified", one.json
    repeat = action(audit, sid, "scan", {"serial": "DEMO-001", "location": "R09S02", "operator": "SPOOFED"}, "scan-one")
    assert repeat.json == one.json
    assert action(audit, sid, "scan", {"serial": "DEMO-001", "location": "R09S02"}).json["is_duplicate"]
    misplaced = action(audit, sid, "scan", {"serial": "DEMO-003", "location": "R09S02"})
    assert misplaced.json["result"] == "misplaced"
    unknown = action(audit, sid, "scan", {"serial": "DEMO-004", "location": "R09S02"})
    assert unknown.json["result"] == "unexpected" and unknown.json["near_matches"]
    complete = action(audit, sid, "complete")
    assert complete.status_code == 200, complete.json
    assert complete.json["session"]["expected_count"] == 2
    assert complete.json["session"]["missing_count"] == 1
    assert complete.json["session"]["accuracy_pct"] == 50
    assert action(audit, sid, "scan", {"serial": "DEMO-002", "location": "R09S02"}).status_code == 409
    with audit[3].engine.connect() as conn:
        assert set(conn.execute(text("SELECT operator FROM dispatch_audit_scans")).scalars()) == {"ADMIN"}
        assert conn.execute(text("SELECT count(*) FROM dispatch_audit_scans")).scalar() == 4
        assert conn.execute(text("SELECT count(*) FROM fulfillment_claims")).scalar() == 0


@pytest.mark.parametrize("kind,expected", [("warehouse", 2), ("all", 4), ("tied", 1)])
def test_scope_and_tied_order_scanning(audit, kind, expected):
    sid = start(audit, kind)
    assert audit[1].get(f"/api/audit/session/{sid}/state").json["counts"]["expected"] == expected
    if kind == "tied":
        assert action(audit, sid, "scan", {"serial": "DEMO-TIED", "location": "SO-DEMO-100"}).json["result"] == "verified"


def test_resolution_history_and_bad_scan_reset(audit):
    sid = start(audit)
    action(audit, sid, "scan", {"serial": "DEMO-001", "location": "WRONG"})
    result = action(audit, sid, "resolve", {"serial": "DEMO-001", "resolution": "bad_scan", "note": "Scanner mistake"})
    assert result.status_code == 200 and result.json["item"]["status"] == "pending", result.json
    assert action(audit, sid, "resolve", {"serial": "DEMO-001", "resolution": "erp_synced"}).status_code == 400
    action(audit, sid, "complete")
    assert action(audit, sid, "resolve", {"serial": "DEMO-002", "resolution": "follow_up", "note": "Investigating"}).status_code == 200
    assert action(audit, sid, "resolve", {"serial": "DEMO-002", "resolution": "relocated", "note": "Returned to rack"}).status_code == 200
    with audit[3].engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM dispatch_audit_resolutions")).scalar() == 3
        assert conn.execute(text("SELECT status FROM dispatch_audit_expected WHERE serial='DEMO-002'")).scalar() == "missing"


def test_all_pages_analytics_and_exports(audit):
    sid = start(audit)
    action(audit, sid, "scan", {"serial": "UNKNOWN", "location": "R09S02"})
    action(audit, sid, "complete")
    action(audit, sid, "resolve", {"serial": "UNKNOWN", "resolution": "follow_up", "note": "=1+1"})
    for path in ("/audit", f"/audit/session/{sid}", "/audit/analytics", "/audit/settings", "/api/audit/analytics"):
        response = audit[1].get(path)
        assert response.status_code == 200, (path, response.get_data(as_text=True)[:500])
        assert response.headers["Cache-Control"] == "no-store"
    report = audit[1].get(f"/audit/session/{sid}/export")
    assert report.status_code == 200, report.json
    book = load_workbook(BytesIO(report.data))
    assert book.sheetnames == ["Expected", "Unexpected", "Resolutions"]
    assert all(cell.data_type != "f" for sheet in book for row in sheet for cell in row)
    daily = audit[1].get("/audit/export")
    assert daily.status_code == 200 and "'=1+1" in daily.get_data(as_text=True)


def test_permissions_csrf_idempotency_and_snapshot_failure(audit, monkeypatch):
    app, client, _, db, _ = audit
    assert app.test_client().get("/audit").status_code == 401
    assert client.post("/api/audit/sessions", json={}).status_code == 403
    first = start(audit, key="same-start")
    assert start(audit, key="same-start") == first
    assert audit[2]("/api/audit/sessions", {"target_kind": "all"}, "same-start").status_code == 409
    monkeypatch.setattr(service, "run_audit_sql_file", lambda *args: (_ for _ in ()).throw(RuntimeError("Unavailable")))
    assert audit[2]("/api/audit/sessions", {"target_kind": "all"}).status_code == 409
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM dispatch_audit_sessions")).scalar() == 1
    client.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"})
    assert client.get("/audit/settings").status_code == 403


def test_dashboard_start_form_uses_dispatch_csrf_and_retries_safely(audit):
    _, client, _, db, csrf = audit
    assert 'action="/audit/session/start"' in client.get("/audit").get_data(as_text=True)
    body = {"target_kind": "all", "csrf_token": csrf, "request_key": "form-start"}
    first = client.post("/audit/session/start", data=body)
    assert first.status_code == 302
    second = client.post("/audit/session/start", data=body)
    assert first.location == second.location
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM dispatch_audit_sessions")).scalar() == 1


def test_audit_settings_revision_cadence_and_identity(audit):
    _, client, _, db, csrf = audit
    data = {"version": "1", "accuracy_target_pct": "98", "dwell_target_hours": "24", "sync_minutes": "10",
        "dwell_locations": "SHIPPING/STAGE", "scope": "SHIPPING/R09S02", "cadence_days": "3", "reason": "Count this rack frequently", "csrf_token": csrf, "request_key": "settings"}
    assert client.post("/audit/settings", data=data).status_code == 302
    assert client.post("/audit/settings", data=data).status_code == 302
    assert client.post("/audit/settings", data={**data, "request_key": "stale"}).status_code == 409
    with db.engine.connect() as conn:
        assert m.get_setting(conn, "audit_configuration")["version"] == 2
        assert conn.execute(text("SELECT cadence_days FROM dispatch_audit_locations WHERE scope='SHIPPING/R09S02'")).scalar() == 3


def test_scan_and_completion_are_atomic_across_clients(audit):
    sid = start(audit)
    app = audit[0]
    def run(name):
        c = app.test_client(); c.post("/api/v1/auth/login", json={"operator": "PICKER", "pin": "123456"})
        csrf = c.get("/api/v1/auth/me").json["csrf"]
        return c.post(f"/api/audit/session/{sid}/{name}", json={"serial": "DEMO-001", "location": "R09S02"} if name == "scan" else {}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())})
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(run, ["scan", "complete"]))
    assert responses[0].status_code in (200, 409) and responses[1].status_code == 200
    with audit[3].engine.connect() as conn:
        assert conn.execute(text("SELECT verified_count FROM dispatch_audit_sessions WHERE id=:id"), {"id": sid}).scalar() == (1 if responses[0].status_code == 200 else 0)


def test_recheck_requires_fresh_unambiguous_unit_evidence_and_preserves_detection(audit, monkeypatch):
    from zoneinfo import ZoneInfo
    sid = start(audit)
    action(audit, sid, "scan", {"serial": "NEW-SERIAL", "location": "SHIPPING/R09S02"})
    stamp = datetime.now(ZoneInfo("America/Denver")).replace(tzinfo=None)
    balance = {"warehouse": "SHIPPING", "location": "R09S02", "qty": 1, "last_txn_at": stamp - timedelta(days=1)}
    evidence = {"NEW-SERIAL": [balance]}
    monkeypatch.setattr(service, "fetch_serial_onhand_locations", lambda serials: evidence)
    assert action(audit, sid, "recheck").json["auto_resolved"] == 0
    balance["last_txn_at"] = stamp + timedelta(seconds=1)
    balance["qty"] = 2
    assert action(audit, sid, "recheck").json["auto_resolved"] == 0
    balance["qty"] = 1
    assert action(audit, sid, "recheck").json["auto_resolved"] == 1
    assert action(audit, sid, "recheck").json["auto_resolved"] == 0
    assert audit[1].get(f"/api/audit/session/{sid}").json["counts"]["unexpected"] == 1


def test_qualified_location_and_schema_upgrade_preserve_audit_history(audit):
    sid = start(audit)
    result = action(audit, sid, "scan", {"serial": "DEMO-001", "location": "MAIN/R09S02"})
    assert result.json["result"] == "misplaced"
    result = action(audit, sid, "scan", {"serial": "DEMO-001", "location": "SHIPPING/R09S02"})
    assert result.json["result"] == "verified"
    db = audit[3]
    with db.transaction() as conn:
        conn.execute(m.schema.update().values(version=2))
    db.initialize()
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM dispatch_audit_scans")).scalar() == 2
        assert conn.execute(text("SELECT version FROM fulfillment_schema")).scalar() == 3


def test_site_scoping_and_legacy_independence():
    root = Path(__file__).resolve().parents[1]
    for name in ("audit_serialized_inventory.sql", "audit_locations_sync.sql", "audit_dwell_time.sql"):
        query = (root / "fulfillment/sql" / name).read_text(encoding="utf-8")
        assert "it.SITE_ID = :site" in query
        assert set(text(query).compile().params) == {"site"}
    for path in (root / "fulfillment/audit").rglob("*.py"):
        contents = path.read_text(encoding="utf-8")
        assert "from picklist" not in contents and "import picklist" not in contents
        assert 'os.getenv("DATABASE_URL")' not in contents


def test_ambiguous_snapshot_rejected():
    rows = runtime.demo_rows()
    with pytest.raises(Exception, match="ambiguous"):
        runtime.validate_snapshot(rows + [rows[0]])
