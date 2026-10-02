import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-pick-abandon-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
if os.name == "nt":
    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
    )

import app as app_module  # noqa: E402
import pick_store  # noqa: E402


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


def _row(order, part, item_type, qty=1):
    return {
        "Cust Order ID": order, "Customer ID": "CUSTOMER", "Part Id": part,
        "Location": "A01", "SO Qty": qty, "UPC": None, "_query_type": item_type,
    }


class PickAbandonRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        app_module.set_setting("feature_shipping_enabled", "true")
        pick_store.initialize(app_module.get_sqlite_conn)
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        self.session_id = pick_store.start_order_session(
            plan_rows=[_row("SO-9001", "P-1", "components")],
            selected_orders=["SO-9001"], source_runs={"components": 1}, operator="PICKER",
        )

    def test_api_abandon_requires_reason_then_closes(self):
        headers = {"X-CSRF-Token": "test-token", "X-Operator": "Lead", "X-Operator-Team": "shipping"}
        refused = self.client.post(f"/api/pick/session/{self.session_id}/abandon", json={}, headers=headers)
        self.assertEqual(refused.status_code, 409)
        done = self.client.post(
            f"/api/pick/session/{self.session_id}/abandon",
            json={"reason": "Shift change"}, headers=headers,
        )
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))
        body = done.get_json()
        self.assertEqual(body["status"], "abandoned")
        self.assertEqual(body["closed_by"], "Lead")
        self.assertNotIn("SO-9001", pick_store.claimed_orders())
        page = self.client.get(f"/pick/session/{self.session_id}").get_data(as_text=True)
        self.assertIn("Pick session closed short", page)
        self.assertIn("Shift change", page)
        self.assertEqual(self.client.post("/api/pick/session/999999/abandon", json={"reason": "x"}, headers=headers).status_code, 404)

    def test_form_abandon_from_shipping_page(self):
        response = self.client.post(
            f"/pick/session/{self.session_id}/abandon",
            data={"csrf_token": "test-token", "reason": "Wrong wave", "operator": "Lead"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(pick_store.get_session(self.session_id)["status"], "abandoned")
        listing = self.client.get("/shipping?view=pick").get_data(as_text=True)
        self.assertIn("closed short", listing)


if __name__ == "__main__":
    unittest.main()
