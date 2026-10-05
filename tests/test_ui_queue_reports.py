"""Queue/report UI regressions; all external data is supplied by fixtures."""
from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import patch

from picklist.app import app
from picklist.routes.shipping import paginate_decisions
from picklist.routes.orders import safe_return_path
from picklist.services import shipping_service


def test_decision_search_happens_before_pagination():
    rows = [{"order_id": f"SO-{i}", "customer_name": "Acme"} for i in range(1207)]
    page = paginate_decisions(rows, "SO-1206", 1)
    assert page["rows"] == [rows[-1]]
    assert page["total"] == 1207
    assert page["filtered"] == 1
    assert paginate_decisions(rows, None, 2)["rows"][0] == rows[100]
    assert paginate_decisions(rows, "missing", 99)["start"] == 0
    assert paginate_decisions(rows, None, "invalid")["page"] == 1


def test_report_return_links_are_internal():
    with app.test_request_context():
        assert safe_return_path("/shipping?view=scorecard&decision_q=SO-123") == "/shipping?view=scorecard&decision_q=SO-123"
        assert safe_return_path("https://evil.example/shipping") == "/orders"
        assert safe_return_path("//evil.example/orders") == "/orders"


def test_queue_marks_age_urgency_and_preserves_claimability():
    timestamp = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [{"Cust Order ID": "SO-OVERDUE", "Customer ID": "C1", "SO Qty": 2,
             "Desired Ship Date": "2020-01-01", "Location": "A1"}]
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT 7 AS id, ? AS run_timestamp", (timestamp,)).fetchone()
    with patch.object(shipping_service, "get_latest_successful_run", return_value=(run, rows)), patch.object(shipping_service.pick_store, "claimed_orders", return_value=set()):
        queue = shipping_service.build_pick_order_queue("guns")
    assert queue["sources"][0]["stale"] is True
    assert queue["orders"][0]["urgency"].startswith("Overdue")
    assert queue["orders"][0]["claimed"] is False


def test_queue_renders_when_readiness_unavailable():
    queue = {"orders": [{"order_id": "SO-1", "customer_id": "CUSTOMER", "customer_name": "", "guns": 2, "components": 0, "units": 2, "locations": ["A1"], "desired_ship_date": "2020-01-01", "claimed": False, "urgency": "Overdue 1 days"}],
             "sources": [{"type": "guns", "id": 7, "timestamp": "2020-01-01T00:00:00+00:00", "age_hours": 48, "stale": True}]}
    with patch("picklist.routes.shipping.build_pick_order_queue", return_value=queue), patch("picklist.routes.shipping.readiness_store.latest_snapshot", side_effect=RuntimeError("unavailable")), patch("picklist.routes.shipping._recent_sessions_for_display", return_value=[]), patch("picklist.routes.shipping._latest_success_by_type", return_value={}), patch("picklist.routes.shipping.pick_store.ready_for_pack_orders", return_value=[]), patch("picklist.routes.shipping.build_unfinished_work", return_value=[]):
        response = app.test_client().get("/shipping?view=pick")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Saved readiness information is unavailable" in html
    assert 'value="SO-1"' in html
    assert "data-selected-orders" in html
    assert "No matching orders" in html
    assert "Stale plan" in html
    assert 'event.key !== "Enter"' in html
    assert "form.requestSubmit(button)" not in html


def test_all_changed_templates_parse():
    for name in ("shipping.html", "orders.html", "order_detail.html"):
        app.jinja_env.get_template(name)

def test_scorecard_search_and_disabled_requests_render():
    rows = [{"order_id": f"SO-{i}", "decision": "SHIP_NOW", "label": "Available", "customer_id": "C1", "ready_qty": 1, "open_qty": 1} for i in range(125)]
    payload = {"decisions": rows, "summary": {}, "policy": {}, "mode": "advisory", "active_exceptions": []}
    with patch("picklist.routes.shipping.build_shipping_scorecard_payload", return_value=shipping_service._empty_scorecard_payload(30, "fixture")), patch("picklist.routes.shipping.build_release_gate_payload", return_value=payload), patch("picklist.routes.shipping.readiness_store.hold_durations", return_value=None), patch("picklist.routes.shipping.request_store.queue_summary", return_value=None):
        response = app.test_client().get("/shipping?view=scorecard&decision_q=SO-124")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "1 matching decisions; 125 total candidates" in html
    assert "/orders/SO-124?return_to=" in html


def test_orders_tiles_preserve_search_and_window():
    payload = {"orders": [], "summary": {"blocked": 0, "attention": 0, "ready": 0}, "evaluated_at": "2026-10-05T10:00:00Z", "never_run": False, "stock_holds_hidden": True, "stale_minutes": 0}
    with patch("picklist.routes.orders.readiness_service.current_payload", return_value=payload):
        response = app.test_client().get("/orders?q=ABC&window=30&owner=&from_report=1")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Snapshot totals below" in html
    assert "q=ABC&amp;window=30&amp;owner=&amp;from_report=1&amp;state=BLOCKED" in html
    assert "Back to scorecard" in html

def test_staging_boundary_is_shared_48_hours():
    import pandas as pd
    from picklist.config import STAGING_TARGET_HOURS, STAGE_AGING_TARGET_HOURS
    from picklist.services.audit_service import AUDIT_DWELL_TARGET_HOURS
    assert STAGING_TARGET_HOURS == AUDIT_DWELL_TARGET_HOURS == STAGE_AGING_TARGET_HOURS == 48
    now = pd.Timestamp('2026-10-05T12:00:00')
    rows = [{"WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "STAGE", "dwell_hours": hours,
             "ERP_NOW": now, "ARRIVED_AT": now - pd.Timedelta(hours=hours), "SERIAL_NO": str(hours),
             "PART_ID": "PART", "PART_DESCRIPTION": "Fixture"} for hours in (47.99, 48, 48.01)]
    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=pd.DataFrame(rows)):
        result = shipping_service.build_stage_aging()
    assert result["target_hours"] == 48
    assert result["summary"]["over_target"] == 1
    assert result["summary"]["within_target"] == 2
    assert result["aged_serials"][0]["serial"] == "48.01"
