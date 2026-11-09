"""HTTP boundaries for sales, planning, approvals, order ingestion and daily close."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment import admin, inventory as inv, model as m
from fulfillment.app import create_app
from fulfillment.inventory_connector import Connector, DeliveryError, Outbox
from fulfillment.openapi import specification
from fulfillment.workflow_requests import TERM_FIELDS
from test_daily_close import ModernSource
from test_inventory import system, setup, receipt, allocate, ok, state, ADMIN, SUP, OP
from test_workflow_requests import SALES, OTHER, change, draft_terms


ORDER_TOKEN = "order-only-secret-" + "x" * 32
INVENTORY_TOKEN = "inventory-only-secret-" + "y" * 32
LABEL_TOKEN = "label-only-secret-" + "z" * 32


@pytest.fixture
def api(system):
    db, erp, service = system
    setup(service)
    pin_hash = generate_password_hash("123456")
    with db.transaction() as conn:
        for actor in (ADMIN, SUP, OP, SALES, OTHER):
            conn.execute(m.users.insert().values(id=actor["id"], name=actor["id"], role=actor["role"], enabled=True, pin_hash=pin_hash))
        settings = admin.load(conn)
        settings["configured"] = True
        m.put_setting(conn, "administration", settings)
    app = create_app(dict(MODE="demo", TESTING=True, DATABASE_URL=str(db.engine.url), SITE="DEMO",
                          ORDER_TOKEN=ORDER_TOKEN, INVENTORY_TOKEN=INVENTORY_TOKEN, LABEL_TOKEN=LABEL_TOKEN), erp=erp)
    yield app, db, erp, service
    app.extensions["fulfillment_db"].engine.dispose()


def login(app, actor=SALES):
    client = app.test_client()
    result = client.post("/api/v1/auth/login", json=dict(operator=actor["id"], pin="123456"))
    assert result.status_code == 200, result.json
    me = client.get("/api/v1/auth/me")
    assert me.json["operator"]["role"] == actor["role"]
    return client, me.json["csrf"]


def post(session, path, body, key=None, **headers):
    client, csrf = session
    return client.post("/api/v1/" + path, json=body,
                       headers={"X-CSRF-Token": csrf, "Idempotency-Key": key or str(uuid4()), **headers})


def passed(response):
    assert response.status_code == 200, response.json
    return response.json


def machine_headers(token=ORDER_TOKEN, key=None):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key or str(uuid4())}


def test_operating_views_require_session_and_sales_can_read_them(api):
    app, _, _, _ = api
    guest = app.test_client()
    sales, _ = login(app)
    for path in ("planning/board", "planning/runs", "workflow", "daily-close", "inventory"):
        assert guest.get("/api/v1/" + path).status_code == 401
        response = sales.get("/api/v1/" + path)
        assert response.status_code == 200, response.json
        assert response.headers["Cache-Control"] == "no-store"
    assert sales.get("/api/v1/admin/state").status_code == 403
    assert sales.get("/api/v1/daily-close/missing/packet").status_code == 404
    assert sales.get("/api/v1/planning/runs/missing").status_code == 404


@pytest.mark.parametrize("route,payload", [
    ("checkouts", {"order": "SO-DEMO-100", "cart": "C1"}),
    ("fulfillments/fake/release", {"version": 1}),
    ("inventory/allocate", allocate()),
    ("planning/run", {"reason": "Sales planning"}),
    ("daily-close/prepare", {"reason": "Sales close", "pause_confirmed": True}),
    ("admin/account", {"version": 1}),
    ("demo/fake/documents", {}),
])
def test_sales_cannot_mutate_shipping_inventory_planning_or_close(api, route, payload):
    app, _, _, _ = api
    result = post(login(app), route, payload)
    assert result.status_code == 403 and result.json["error"] == "role"


def test_sales_intake_requires_csrf_idempotency_and_same_origin(api):
    app, db, erp, _ = api
    client, csrf = login(app)
    body = draft_terms(erp)
    assert client.post("/api/v1/workflow/sales-create", json=body, headers={"Idempotency-Key": "missing-csrf"}).status_code == 403
    assert client.post("/api/v1/workflow/sales-create", json=body, headers={"X-CSRF-Token": csrf}).status_code == 400
    assert post((client, csrf), "workflow/sales-create", body, Origin="https://foreign.example").status_code == 403
    key = str(uuid4())
    first = passed(post((client, csrf), "workflow/sales-create", body, key))
    assert passed(post((client, csrf), "workflow/sales-create", body, key)) == first
    assert post((client, csrf), "workflow/sales-create", {**body, "customer_reference": "DIFFERENT"}, key).status_code == 409
    submitted = passed(post((client, csrf), "workflow/sales-submit", change(first)))
    assert submitted["status"] == "awaiting_visual_order"
    with db.engine.connect() as conn:
        assert len(inv.rows(conn, m.sales_requests)) == 1
        assert inv.rows(conn, m.inventory_allocations) == []


def test_http_approval_keeps_expired_review_hold_reserved_until_explicit_change(api):
    app, db, _, service = api
    ok(service, "receive", receipt())
    sales, supervisor, operator = login(app), login(app, SUP), login(app, OP)
    terms = {**allocate(), "review_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
    request = passed(post(sales, "workflow/request-create", dict(kind="allocation", terms=terms, reason="Inside Sales hold")))
    submitted = passed(post(sales, "workflow/request-submit", change(request)))
    assert state(db)["balances"][0]["committed"] == "0"
    assert post(operator, "workflow/request-approve", change(submitted)).status_code == 403
    key = str(uuid4())
    approved = passed(post(supervisor, "workflow/request-approve", change(submitted), key))
    assert passed(post(supervisor, "workflow/request-approve", change(submitted), key)) == approved
    assert approved["status"] == "approved"
    allocation = state(db)["allocations"][0]
    assert allocation["hold"] and allocation["review_due"]
    assert state(db)["balances"][0]["committed"] == "3"
    passed(post(supervisor, "planning/run", {"reason": "Recheck expired review holds"}))
    assert state(db)["allocations"][0]["hold"] is True
    assert state(db)["balances"][0]["committed"] == "3"


def test_session_identity_changes_are_rechecked_on_workflow_mutation(api):
    app, db, erp, _ = api
    session = login(app)
    with db.transaction() as conn:
        conn.execute(m.users.update().where(m.users.c.id == SALES["id"]).values(enabled=False))
    response = post(session, "workflow/sales-create", draft_terms(erp))
    assert response.status_code == 401
    with db.engine.connect() as conn:
        assert inv.rows(conn, m.sales_requests) == []


def test_order_ingestion_requires_its_separate_bearer_and_has_no_csrf_requirement(api):
    app, db, erp, _ = api
    client = app.test_client()
    payload = dict(event_id="ORDER-1", observed_at=m.now(), source_revision=1, order=erp.inspect_order("SO-DEMO-100"))
    assert client.post("/api/v1/integrations/orders", json=payload).status_code == 401
    for token in (INVENTORY_TOKEN, LABEL_TOKEN):
        assert client.post("/api/v1/integrations/orders", json=payload, headers=machine_headers(token)).status_code == 401
    assert client.post("/api/v1/integrations/orders", json=payload, headers={"Authorization": "Bearer " + ORDER_TOKEN}).status_code == 400
    key = str(uuid4())
    first = passed(client.post("/api/v1/integrations/orders", json=payload, headers=machine_headers(key=key)))
    assert first["order"]["order"] == "SO-DEMO-100"
    assert passed(client.post("/api/v1/integrations/orders", json=payload, headers=machine_headers(key=key))) == first
    assert client.get("/api/v1/integrations/inventory/state", headers=machine_headers()).status_code == 401
    assert client.post("/api/v1/integrations/worldship/labels", json={}, headers=machine_headers()).status_code == 401
    same_source_other_key = passed(client.post("/api/v1/integrations/orders", json=payload, headers=machine_headers()))
    assert same_source_other_key == first
    changed = {**payload, "event_id": "ORDER-OLDER", "source_revision": 0, "observed_at": m.now(), "order": {**payload["order"], "customer": "OLD NAME"}}
    ignored = passed(client.post("/api/v1/integrations/orders", json=changed, headers=machine_headers()))
    assert ignored["ignored_older_revision"] is True
    with db.engine.connect() as conn:
        records = inv.rows(conn, m.order_book)
        assert len(records) == 1 and records[0]["order"]["customer"] != "OLD NAME"


def test_planning_run_is_audited_idempotent_and_failure_remains_inspectable(api, monkeypatch):
    app, _, erp, _ = api
    session = login(app, SUP)
    key = str(uuid4())
    body = {"reason": "Supervisor requested order refresh"}
    result = passed(post(session, "planning/run", body, key))
    assert result["planning_run"]["status"] == "succeeded"
    assert passed(post(session, "planning/run", body, key)) == result
    history = passed(session[0].get("/api/v1/planning/runs"))
    assert history["total"] == 1
    run = passed(session[0].get("/api/v1/planning/runs/" + result["planning_run"]["id"]))
    assert run["inputs"]["orders"] and run["decisions"]
    board = passed(session[0].get("/api/v1/planning/board"))
    assert board["orders"][0]["owner"] and board["orders"][0]["next_action"]
    def offline(_context):
        raise RuntimeError("Test source outage")
    monkeypatch.setattr(erp, "order_snapshot", offline)
    failed = post(session, "planning/run", {"reason": "Verify unavailable source"})
    assert failed.status_code == 503 and failed.json["planning_run"]["status"] == "failed"
    retained = passed(session[0].get("/api/v1/planning/runs/" + failed.json["planning_run"]["id"]))
    assert retained["status"] == "failed" and retained["error"]
    assert passed(session[0].get("/api/v1/planning/board"))["orders"][0]["status"] == "source_unavailable"


class HTTPInventoryReceiver:
    def __init__(self, app):
        self.client = app.test_client()

    def state(self):
        return passed(self.client.get("/api/v1/integrations/inventory/state", headers=machine_headers(INVENTORY_TOKEN)))

    def deliver(self, payload):
        response = self.client.post("/api/v1/integrations/inventory/batches", json=payload, headers=machine_headers(INVENTORY_TOKEN, payload["event_id"]))
        if response.status_code != 200:
            raise DeliveryError(response.json["error"], response.json["message"])
        return response.json

    def confirm_delivery(self, checkpoint):
        return passed(self.client.post("/api/v1/integrations/inventory/health", json=dict(event_id="CONFIRM-" + str(checkpoint["sequence"]),
            source="visual", stream_id=checkpoint["stream_id"], sequence=checkpoint["sequence"], code="delivery_confirmed"), headers=machine_headers(INVENTORY_TOKEN)))


def test_http_close_requires_complete_capture_independent_signer_and_retains_packet(api, tmp_path):
    app, _, _, _ = api
    source, receiver = ModernSource(), HTTPInventoryReceiver(app)
    connector = Connector(source, receiver, Outbox(tmp_path / "operations-outbox.db"))
    assert connector.once(bootstrap=True)["accepted"]
    preparer, signer = login(app, SUP), login(app, OTHER)
    prepare_key = str(uuid4())
    body = dict(reason="Coordinated warehouse close", pause_confirmed=True)
    prepared = passed(post(preparer, "daily-close/prepare", body, prepare_key))
    assert prepared["evidence_status"] == "balanced"
    assert passed(post(preparer, "daily-close/prepare", body, prepare_key)) == prepared
    assert post(preparer, "daily-close/sign", change(prepared)).status_code == 403
    signed = passed(post(signer, "daily-close/sign", change(prepared)))
    assert signed["approval_status"] == "signed"
    response = signer[0].get("/api/v1/daily-close/" + signed["id"] + "/packet")
    assert response.status_code == 200
    assert response.json["packet"] == prepared["packet"]
    assert response.headers["Content-Disposition"] == 'attachment; filename="dispatch-close-' + signed["id"] + '.json"'
    assert passed(signer[0].get("/api/v1/daily-close"))["closes"][0]["signed_by"] == OTHER["id"]
    assert post(signer, "daily-close/sign", change(signed)).status_code == 409


def test_incomplete_close_cannot_be_signed_even_with_exception_override(api):
    app, _, _, _ = api
    preparer, signer = login(app, SUP), login(app, OTHER)
    prepared = passed(post(preparer, "daily-close/prepare", dict(reason="Missing source close review", pause_confirmed=True)))
    assert prepared["evidence_status"] == "incomplete"
    refused = post(signer, "daily-close/sign", change(prepared, allow_exceptions=True))
    assert refused.status_code == 409 and refused.json["error"] == "close_incomplete"


def test_only_inventory_capture_endpoint_accepts_envelope_above_two_mib(api):
    app, _, _, _ = api
    client = app.test_client()
    large = {"source": "visual", "padding": "x" * (2 * 1024 * 1024)}
    accepted_size = client.post("/api/v1/integrations/inventory/batches", json=large, headers=machine_headers(INVENTORY_TOKEN))
    assert accepted_size.status_code in (400, 409), accepted_size.json
    sales = login(app)
    assert post(sales, "workflow/sales-create", large).status_code == 413
    oversized = {"source": "visual", "padding": "x" * (16 * 1024 * 1024)}
    assert client.post("/api/v1/integrations/inventory/batches", json=oversized, headers=machine_headers(INVENTORY_TOKEN)).status_code == 413


def test_api_contract_covers_every_new_command_and_permission_header():
    spec = specification()
    paths = spec["paths"]
    workflow_actions = ("sales-create", "sales-update", "sales-submit", "sales-cancel", "sales-bind",
                        "request-create", "request-update", "request-submit", "request-cancel", "request-approve", "request-reject")
    close_actions = ("prepare", "exception", "sign", "supersede", "correspondence", "correspondence-cancel")
    for prefix, actions in (("workflow", workflow_actions), ("daily-close", close_actions)):
        for action in actions:
            operation = paths["/" + prefix + "/" + action]["post"]
            assert operation["security"] == [{"operatorCookie": []}]
            assert {"Idempotency-Key", "X-CSRF-Token"} <= {p["name"] for p in operation["parameters"]}
    for path in ("/workflow", "/daily-close", "/daily-close/{id}/packet", "/planning/board", "/planning/runs", "/planning/runs/{id}"):
        assert paths[path]["get"]["security"] == [{"operatorCookie": []}]
    order = paths["/integrations/orders"]["post"]
    assert order["security"] == [{"orderIntegrationBearer": []}]
    assert "X-CSRF-Token" not in {p["name"] for p in order["parameters"]}
    assert "Idempotency-Key" in {p["name"] for p in order["parameters"]}
    schema = order["requestBody"]["content"]["application/json"]["schema"]
    assert {"event_id", "source_revision", "observed_at", "order"} <= set(schema["required"])
    account = paths["/admin/account"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "sales" in account["properties"]["role"]["enum"]
    batch = paths["/integrations/inventory/batches"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"movement_changes", "movement_evidence"} <= set(batch["properties"])
    health = paths["/integrations/inventory/health"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"delivery_pending", "delivery_confirmed"} <= set(health["properties"]["code"]["enum"])


def required_fields(schema, document):
    """Check actual JSON's required fields, including nested/ref response objects."""
    if "$ref" in schema:
        schema = specification()["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    for component in schema.get("allOf", []):
        required_fields(component, document)
    if isinstance(document, dict):
        assert set(schema.get("required", [])) <= set(document)
        for field, child in schema.get("properties", {}).items():
            if field in document:
                required_fields(child, document[field])
    elif isinstance(document, list):
        for row in document:
            required_fields(schema.get("items", {}), row)


def test_actual_http_response_fields_match_published_operating_contract(api):
    app, _, erp, service = api
    specs = specification()["components"]["schemas"]
    sales, supervisor = login(app), login(app, SUP)
    draft = passed(post(sales, "workflow/sales-create", draft_terms(erp)))
    required_fields(specs["SalesRequest"], draft)
    ok(service, "receive", receipt())
    request = passed(post(sales, "workflow/request-create", dict(kind="allocation", terms=allocate(), reason="Review requested hold")))
    submitted = passed(post(sales, "workflow/request-submit", change(request)))
    approved = passed(post(supervisor, "workflow/request-approve", change(submitted)))
    required_fields(specs["ApprovalRequest"], approved)
    feed = dict(event_id="CONTRACT-ORDER", observed_at=m.now(), source_revision=1, order=erp.inspect_order("SO-DEMO-100"))
    mirrored = passed(app.test_client().post("/api/v1/integrations/orders", json=feed, headers=machine_headers()))
    required_fields(specs["OrderMirror"], mirrored)
    result = passed(post(supervisor, "planning/run", {"reason": "Validate retained explanation"}))
    required_fields(specs["PlanningRun"], passed(supervisor[0].get("/api/v1/planning/runs/" + result["planning_run"]["id"])))
    required_fields(specs["OrderBoard"], passed(supervisor[0].get("/api/v1/planning/board")))
    prepared = passed(post(supervisor, "daily-close/prepare", dict(reason="Validate incomplete evidence response", pause_confirmed=True)))
    required_fields(specs["DailyClose"], prepared)
    for kind, fields in TERM_FIELDS.items():
        assert set(specs["ApprovalTerms_" + kind]["properties"]) == fields
