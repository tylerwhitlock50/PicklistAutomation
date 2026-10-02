import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-orders-routes-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
os.environ.pop("OPERATOR_ROSTER_JSON", None)
os.environ.pop("TEAMS_WEBHOOK_URL", None)

from picklist import app as app_module  # noqa: E402
from picklist import db  # noqa: E402
from picklist.services import readiness_service  # noqa: E402
from tests.test_readiness import row  # noqa: E402


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


class OrderRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        db.delete_setting("operator_roster_json")
        db.set_setting("feature_orders_enabled", "true")
        self.rows = [
            row(order="SO-1", ORDER_STATUS="F", CUSTOMER_ID="DEALER1", CUSTOMER_NAME="Dealer One"),
            row(order="SO-2", CREDIT_STATUS="H", CUSTOMER_ID="DEALER2", CUSTOMER_NAME="Dealer Two"),
            row(order="SO-3", CUSTOMER_ID="DEALER3", CUSTOMER_NAME="Dealer Three"),
        ]
        self.notifications = []
        readiness_service._last_refresh.update({"at": None, "payload": None})
        readiness_service.configure(
            fetch_candidates=lambda: list(self.rows),
            fetch_order_rows=lambda so: [r for r in self.rows if r["CUST_ORDER_ID"] == so],
            fetch_order_locations=lambda so: [
                {"PART_ID": "801-06486-00", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "R03S03", "QTY": 3}
            ],
            picklist_orders_today=lambda: None,
            gate_decisions=lambda: {},
            today=lambda: date(2026, 10, 1),
            notify=lambda event, **card: self.notifications.append(event) or True,
            cache_seconds=0,
        )
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        self.headers = {"X-CSRF-Token": "test-token"}

    def tearDown(self):
        db.set_setting("feature_orders_enabled", "true")

    def _refresh(self):
        response = self.client.post("/api/readiness/refresh", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_orders_page_before_any_evaluation(self):
        # a fresh DB may already have snapshots from other tests; just make sure it renders
        response = self.client.get("/orders")
        self.assertEqual(response.status_code, 200)
        self.assertIn("What is holding orders up?", response.get_data(as_text=True))

    def test_refresh_then_list_filter_and_detail(self):
        summary = self._refresh()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["summary"]["orders"], 3)
        self.assertIn("hold_created", self.notifications)

        html = self.client.get("/orders?owner=").get_data(as_text=True)
        self.assertIn("SO-1", html)
        self.assertIn("Firmed, not released", html)
        self.assertIn("state-BLOCKED", html)

        finance = self.client.get("/api/orders?owner=finance").get_json()
        self.assertEqual([o["order_id"] for o in finance["orders"]], ["SO-2"])
        ready = self.client.get("/api/orders?owner=&state=READY").get_json()
        self.assertEqual([o["order_id"] for o in ready["orders"]], ["SO-3"])

        # cookie from the picker selects the viewer's team by default
        self.client.set_cookie("ops_operator", "Holly")
        self.client.set_cookie("ops_operator_team", "sales")
        mine = self.client.get("/api/orders").get_json()
        self.assertEqual(mine["filters"]["owner"], "sales")
        self.assertEqual([o["order_id"] for o in mine["orders"]], ["SO-1"])

        detail_html = self.client.get("/orders/so-1").get_data(as_text=True)
        self.assertIn("SO-1", detail_html)
        self.assertIn("Firmed, not released", detail_html)
        self.assertIn("R03S03", detail_html)
        self.assertIn("Acknowledge", detail_html)

        detail = self.client.get("/api/orders/SO-1").get_json()
        self.assertTrue(detail["found"])
        self.assertEqual(detail["state"], "BLOCKED")
        hold_id = detail["holds"][0]["id"]
        self.assertIsNotNone(hold_id)

        # acknowledge: needs an operator (none on the roster, so any name is accepted)
        anonymous = self.client.post(
            f"/api/orders/SO-1/holds/{hold_id}/ack", json={"note": "x"},
            headers={"X-CSRF-Token": "test-token"},
            environ_overrides={"HTTP_COOKIE": ""},
        )
        self.assertIn(anonymous.status_code, (200, 400))
        self.client.delete_cookie("ops_operator")
        self.client.delete_cookie("ops_operator_team")
        missing = self.client.post(f"/api/orders/SO-1/holds/{hold_id}/ack", json={"note": "x"}, headers=self.headers)
        self.assertEqual(missing.status_code, 400)
        acked = self.client.post(
            f"/api/orders/SO-1/holds/{hold_id}/ack",
            json={"note": "Asked Lunden to release"},
            headers={**self.headers, "X-Operator": "Holly", "X-Operator-Team": "sales"},
        )
        self.assertEqual(acked.status_code, 200, acked.get_data(as_text=True))
        self.assertEqual(acked.get_json()["hold"]["acknowledged_by"], "Holly")
        wrong_order = self.client.post(
            f"/api/orders/SO-2/holds/{hold_id}/ack", json={},
            headers={**self.headers, "X-Operator": "Holly"},
        )
        self.assertEqual(wrong_order.status_code, 400)

        self.assertEqual(self.client.get("/api/orders/SO-404").status_code, 404)
        self.assertEqual(self.client.get("/orders/SO-404").status_code, 404)

    def test_shipping_holds_tab_redirects_to_orders(self):
        self._refresh()
        response = self.client.get("/shipping?view=holds")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/orders"))
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("What is holding orders up?", html)
        self.assertIn("SO-2", html)

    def test_feature_flag_off(self):
        db.set_setting("feature_orders_enabled", "false")
        self.assertEqual(self.client.get("/orders").status_code, 302)
        self.assertEqual(self.client.get("/api/orders").status_code, 404)
        self.assertEqual(self.client.post("/api/readiness/refresh", json={}, headers=self.headers).status_code, 404)
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(">Orders<", html)

    def test_refresh_reports_erp_failure(self):
        def boom():
            raise RuntimeError("VISUAL unreachable")

        readiness_service.configure(fetch_candidates=boom)
        response = self.client.post("/api/readiness/refresh", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertIn("VISUAL unreachable", response.get_json()["error"])
        page = self.client.get("/orders?owner=").get_data(as_text=True)
        self.assertIn("Last refresh failed", page)


if __name__ == "__main__":
    unittest.main()
