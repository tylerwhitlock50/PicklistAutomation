import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-lookup-routes-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
os.environ.pop("OPERATOR_ROSTER_JSON", None)
os.environ.pop("TEAMS_WEBHOOK_URL", None)
if os.name == "nt":
    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
    )

import app as app_module  # noqa: E402
import notifier  # noqa: E402
import readiness_service  # noqa: E402
from tests.test_readiness import row as order_row  # noqa: E402
from tests.test_shipments import row as ship_row  # noqa: E402
from tests.test_stock import loc  # noqa: E402


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


class LookupRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        app_module.set_setting("feature_orders_enabled", "true")
        app_module.delete_setting("teams_webhook_url")
        self.ship_rows = [
            ship_row(packlist="PL-288871", order="SO-132000", SHIPPED_DATE=datetime(2026, 10, 1)),
            ship_row(packlist="PL-288872", order="SO-132001", line=1, SHIPPED_DATE=datetime(2026, 10, 1), TRACKING_NUMBERS=None, UDF_TRACKING_NUMBER=None),
        ]
        readiness_service._last_refresh.update({"at": None, "payload": None})
        readiness_service.configure(
            fetch_candidates=lambda: [order_row(order="SO-132000")],
            fetch_order_rows=lambda so: [order_row(order=so)] if so in ("SO-132000",) else [],
            fetch_order_locations=lambda so: [],
            fetch_order_shipments=lambda so: [r for r in self.ship_rows if r["CUST_ORDER_ID"] == so],
            picklist_orders_today=lambda: None,
            gate_decisions=lambda: {},
            today=lambda: date(2026, 10, 1),
            notify=lambda event, **card: True,
            cache_seconds=0,
        )
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        self.headers = {"X-CSRF-Token": "test-token"}

    def tearDown(self):
        notifier.configure(get_config_value=app_module.get_config_value, transport=None)

    def test_order_detail_shows_shipments(self):
        html = self.client.get("/orders/SO-132000").get_data(as_text=True)
        self.assertIn("PL-288871", html)
        self.assertIn("1Z61E14WA844450768", html)
        self.assertIn("tracknum=1Z61E14WA844450768", html)
        data = self.client.get("/api/orders/SO-132000").get_json()
        self.assertEqual(data["shipment_summary"]["shipped_packlists"], 1)

    def test_archived_order_with_shipments_only(self):
        html = self.client.get("/orders/SO-132001").get_data(as_text=True)
        self.assertIn("PL-288872", html)
        self.assertIn("none yet", html)

    def test_api_order_shipments(self):
        with patch.object(app_module, "fetch_order_shipment_rows", return_value=self.ship_rows[:1]):
            data = self.client.get("/api/orders/SO-132000/shipments").get_json()
        self.assertEqual(data["packlists"][0]["packlist_id"], "PL-288871")
        self.assertEqual(data["summary"]["units_shipped"], 1.0)

    def test_shipments_page_and_api(self):
        self.assertIn("Look up", self.client.get("/shipments").get_data(as_text=True))
        with patch.object(app_module, "fetch_shipment_lookup_rows", return_value=self.ship_rows) as lookup:
            html = self.client.get("/shipments?customer=dealer&start=2026-09-25&end=2026-10-01").get_data(as_text=True)
            self.assertIn("PL-288871", html)
            self.assertIn("1 without tracking", html)
            args = lookup.call_args[0]
            self.assertEqual(args[:2], ("2026-09-25", "2026-10-02"))
            self.assertEqual(args[2], "dealer")
            data = self.client.get("/api/shipments?customer=dealer").get_json()
            self.assertEqual(data["summary"]["packlists"], 2)
        with patch.object(app_module, "fetch_order_shipment_rows", return_value=self.ship_rows[:1]):
            html = self.client.get("/shipments?so=so-132000").get_data(as_text=True)
            self.assertIn("PL-288871", html)
        serial = self.client.get("/shipments?serial=CV1")
        self.assertEqual(serial.status_code, 302)
        self.assertIn("serial", serial.headers["Location"])

    def test_digest_preview_and_send(self):
        sent = []
        app_module.set_setting("teams_webhook_url", "https://example.test/hook")
        notifier.configure(
            get_config_value=app_module.get_config_value,
            transport=lambda url, payload: sent.append(payload),
        )
        with patch.object(app_module, "fetch_shipment_lookup_rows", return_value=self.ship_rows):
            preview = self.client.get("/api/shipping/digest/preview?date=2026-10-01").get_json()
            self.assertEqual(preview["packlist_count"], 2)
            self.assertEqual(preview["missing_tracking"], ["PL-288872"])
            response = self.client.post("/api/shipping/digest/send?date=2026-10-01", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertTrue(response.get_json()["sent"])
            self.assertEqual(len(sent), 1)
            card = sent[0]["attachments"][0]["content"]
            self.assertIn("Shipped today", card["body"][0]["text"])
            # scheduled check is idempotent for the day
            app_module.set_setting("teams_digest_time", "00:00")
            with patch.object(app_module, "_today_local", return_value=date(2026, 10, 1)):
                app_module.scheduled_shipped_digest_check()
            self.assertEqual(len(sent), 1)
        app_module.delete_setting("teams_digest_time")

    def test_stock_page_and_api(self):
        rows = [loc("SHIPPING", "R03S04", 8, "CV1, CV2"), loc("SHIPPING", "R10S04", 1, "CV9")]
        allocation = {"error": None, "demand": {"lines": [
            {"so": "SO-132000", "line_no": 1, "customer_id": "DEALER1", "customer_name": "Dealer One", "position": 1,
             "dates": {"eff_promise_del": "2026-10-03"}, "supply_status": "ALLOCATED", "allocations": [{"class": "ON_HAND", "qty": 1}]},
        ]}}
        with patch.object(app_module, "fetch_stock_rows", return_value=rows), patch.object(app_module, "_build_allocation_payload", return_value=allocation):
            html = self.client.get("/stock?part=801-06486-00").get_data(as_text=True)
            self.assertIn("R03S04", html)
            self.assertIn("Rack 10", html)
            self.assertIn("SO-132000", html)
            data = self.client.get("/api/stock?part=801-06486-00").get_json()
            self.assertEqual(data["pickable_qty"], 8.0)
            self.assertEqual(data["allocated_qty"], 1.0)
            self.assertEqual(data["free_qty"], 7.0)
        self.assertEqual(self.client.get("/api/stock").status_code, 400)
        with patch.object(app_module, "fetch_serial_onhand_locations", return_value={"CV9": [{"PART_ID": "801-06486-00", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "R10S04", "QTY": 1}]}):
            html = self.client.get("/stock?serial=cv9").get_data(as_text=True)
            self.assertIn("R10S04", html)
        with patch.object(app_module, "_search_parts", return_value=[{"part_id": "801-06486-00", "description": "RL", "on_hand": 8, "open_demand": 2}]):
            self.assertEqual(self.client.get("/api/stock/parts?q=801").get_json()["parts"][0]["part_id"], "801-06486-00")

    def test_feature_flag_gates_lookups(self):
        app_module.set_setting("feature_orders_enabled", "false")
        try:
            self.assertEqual(self.client.get("/shipments").status_code, 302)
            self.assertEqual(self.client.get("/api/stock?part=X").status_code, 404)
        finally:
            app_module.set_setting("feature_orders_enabled", "true")


if __name__ == "__main__":
    unittest.main()
