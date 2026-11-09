"""Public inventory connector contract stays distinct from operator actions."""
from fulfillment.openapi import specification


def test_connector_routes_require_machine_auth_and_fenced_capture():
    spec = specification()
    paths = spec["paths"]
    for route, method in (("state", "get"), ("batches", "post"), ("health", "post")):
        operation = paths["/integrations/inventory/" + route][method]
        assert operation["security"] == [{"integrationBearer": []}]
        headers = {p["name"] for p in operation.get("parameters", [])}
        assert "X-CSRF-Token" not in headers
        if method == "post":
            assert "Idempotency-Key" in headers
    batch = paths["/integrations/inventory/batches"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"expected_stock_version", "stream_id", "sequence", "balances", "observed_at"} <= set(batch["required"])
    assert batch["properties"]["balances"]["maxItems"] == 5000
    assert batch["properties"]["source"] == {"const": "visual"}
    health = paths["/integrations/inventory/health"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"stream_id", "sequence", "code", "event_id"} <= set(health["required"])
    assert health["properties"]["code"]["enum"] == ["capture_failed", "delivery_rejected", "delivery_pending", "delivery_confirmed"]
    assert spec["components"]["schemas"]["InventorySyncState"]["properties"]["freshness_seconds"]["const"] == 300


def test_operator_sync_recovery_requires_csrf_reason_and_current_supply_revision():
    paths = specification()["paths"]
    for action in ("sync-ack", "link-receipt"):
        operation = paths["/inventory/" + action]["post"]
        assert operation["security"] == [{"operatorCookie": []}]
        assert {"Idempotency-Key", "X-CSRF-Token"} <= {p["name"] for p in operation["parameters"]}
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert "reason" in schema["required"]
        if action == "link-receipt":
            assert {"event_id", "work_order_id", "version"} <= set(schema["required"])
