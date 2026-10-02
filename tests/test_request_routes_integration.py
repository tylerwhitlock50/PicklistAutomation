import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-request-routes-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
os.environ.pop("OPERATOR_ROSTER_JSON", None)
os.environ.pop("TEAMS_WEBHOOK_URL", None)

from picklist import app as app_module  # noqa: E402
from picklist import db  # noqa: E402
from picklist.domain import notifier  # noqa: E402
from picklist.services import orders_service  # noqa: E402
from picklist.services import shipping_service  # noqa: E402
from picklist.stores import readiness_store  # noqa: E402
from picklist.domain import release_gate  # noqa: E402
from picklist.services import request_service  # noqa: E402
from picklist.stores import request_store  # noqa: E402
from picklist.stores import shipping_store  # noqa: E402


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


SALES = {"X-Operator": "Holly", "X-Operator-Team": "sales"}
SHIPPING = {"X-Operator": "Robert", "X-Operator-Team": "shipping"}


class RequestRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        db.set_setting("feature_orders_enabled", "true")
        db.delete_setting("operator_roster_json")
        # Store unit tests rebind these modules to their own temp DBs; point
        # them back at the app database so routes and stores agree.
        readiness_store.initialize(db.get_sqlite_conn)
        request_store.initialize(db.get_sqlite_conn)
        shipping_store.initialize(db.get_sqlite_conn)
        self.notifications = []
        request_service.configure(
            notify=lambda event, **card: self.notifications.append(event) or True,
            refresh_readiness=lambda: None,
        )
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"

    def tearDown(self):
        request_service.configure(notify=notifier.send_teams_notification)

    def _post(self, path, data, who):
        payload = {"csrf_token": "test-token", **data}
        return self.client.post(path, data=payload, headers=who)

    def test_create_ship_request_and_accept_expedite(self):
        response = self._post("/requests", {
            "request_type": "ship_request", "cust_order_id": "so-131597", "needed_by": "2026-10-06",
            "expedite": "1", "service_level": "2day", "body": "Dealer show on Monday",
        }, SALES)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        request_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        self.assertIn("request_created", self.notifications)

        detail = self.client.get(f"/requests/{request_id}").get_data(as_text=True)
        self.assertIn("Expedite SO-131597", detail)
        self.assertIn("Accept as expedite", detail)

        listing = self.client.get("/requests?scope=all", headers=SALES).get_data(as_text=True)
        self.assertIn("Expedite SO-131597", listing)
        mine = self.client.get("/api/requests?scope=mine", headers=SALES).get_json()
        self.assertEqual([r["id"] for r in mine["requests"]], [request_id])

        # Sales cannot accept an expedite
        refused = self._post(f"/requests/{request_id}/transition", {"to_status": "acknowledged", "accept_expedite": "1"}, SALES)
        self.assertEqual(refused.status_code, 302)
        self.assertEqual(request_store.get_request(request_id)["status"], "open")

        accepted = self._post(f"/requests/{request_id}/transition", {"to_status": "acknowledged", "accept_expedite": "1", "note": "pulling now"}, SHIPPING)
        self.assertEqual(accepted.status_code, 302)
        req = request_store.get_request(request_id)
        self.assertEqual(req["status"], "acknowledged")
        self.assertIsNotNone(req["linked_exception_id"])
        active = {row["cust_order_id"]: row for row in shipping_store.active_exceptions()}
        self.assertIn("SO-131597", active)
        self.assertIn("Ship request", active["SO-131597"]["reason"])

        done = self._post(f"/requests/{request_id}/transition", {"to_status": "done", "resolution": "Shipped 2nd day"}, SHIPPING)
        self.assertEqual(done.status_code, 302)
        self.assertNotIn("SO-131597", {row["cust_order_id"] for row in shipping_store.active_exceptions()})
        self.assertIn("request_done", self.notifications)

    def test_expedite_blocked_by_readiness_hold(self):
        readiness_store.reconcile_holds(
            [{"order_id": "SO-777", "reason_code": "credit_status_hold", "label": "Credit status not approved", "owner_team": "finance", "blocking": True, "detail": {}}],
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            response = self._post("/requests", {"request_type": "ship_request", "cust_order_id": "SO-777", "expedite": "1"}, SALES)
            request_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
            page = self.client.get(f"/requests/{request_id}").get_data(as_text=True)
            self.assertIn("blocking holds that a request cannot override", page)
            self._post(f"/requests/{request_id}/transition", {"to_status": "in_progress", "accept_expedite": "1"}, SHIPPING)
            self.assertEqual(request_store.get_request(request_id)["status"], "open")
            self.assertNotIn("SO-777", {row["cust_order_id"] for row in shipping_store.active_exceptions()})
        finally:
            readiness_store.reconcile_holds([], evaluated_at=datetime.now(timezone.utc).isoformat(), seen_orders=[])

    def test_hold_request_excludes_order_from_picklist_and_gate(self):
        no_so = self._post("/requests", {"request_type": "hold_exception", "exception_kind": "marketing"}, SALES, )
        self.assertEqual(no_so.status_code, 302)
        self.assertEqual(request_store.list_requests(request_type="hold_exception"), [])

        until = (date.today() + timedelta(days=5)).isoformat()
        response = self._post("/requests", {"request_type": "hold_exception", "cust_order_id": "SO-555", "exception_kind": "marketing", "expires_at": until, "body": "SHOT show build"}, SALES)
        request_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(shipping_service._active_manual_holds(), [])
        self._post(f"/requests/{request_id}/transition", {"to_status": "acknowledged"}, SHIPPING)
        holds = shipping_service._active_manual_holds()
        self.assertEqual([h["cust_order_id"] for h in holds], ["SO-555"])

        df = pd.DataFrame([
            {"Cust Order ID": "SO-555", "Part ID": "801-1", "SO Qty": 1},
            {"Cust Order ID": "so-556", "Part ID": "801-2", "SO Qty": 1},
        ])
        trimmed = orders_service._apply_manual_hold_exclusions(df)
        self.assertEqual(list(trimmed["Cust Order ID"]), ["so-556"])
        self.assertEqual(trimmed.attrs["manual_hold_exclusions"][0]["order_id"], "SO-555")
        self.assertEqual(trimmed.attrs["manual_hold_exclusions"][0]["hold_kind"], "marketing")

        rows = [{
            "CUST_ORDER_ID": "SO-555", "LINE_NO": 1, "CUSTOMER_ID": "C", "CUSTOMER_NAME": "C", "ORDER_DATE": date(2026, 9, 1),
            "PART_ID": "801-1", "ITEM_TYPE": "guns", "OPEN_QTY": 1, "AVAILABLE_QTY": 1, "PROMISE_SHIP_DATE": date(2026, 10, 2),
            "ORDER_RELEASED": 1, "LINE_AVAILABLE": 1, "CREDIT_APPROVED": 1, "SHIP_TO_PRESENT": 1,
            "IS_RMA": 0, "IS_INTERNATIONAL": 0, "IS_EMPLOYEE": 0, "EXCLUDED_CUSTOMER": 0,
        }]
        decision = release_gate.evaluate_release_gate(rows, today=date(2026, 10, 1), manual_holds=holds)["decisions"][0]
        self.assertEqual((decision["decision"], decision["reason_code"]), ("HOLD", "manual_hold"))
        self.assertIn("marketing", decision["label"])

        html = self.client.get("/orders/SO-555").get_data(as_text=True)
        self.assertIn("Manual holds", html)
        self.assertIn("Marketing build", html)

        released = self.client.post(f"/api/holds/{holds[0]['id']}/release", json={}, headers={"X-CSRF-Token": "test-token", **SALES})
        self.assertEqual(released.status_code, 403)
        released = self.client.post(f"/api/holds/{holds[0]['id']}/release", json={}, headers={"X-CSRF-Token": "test-token", **SHIPPING})
        self.assertEqual(released.status_code, 200, released.get_data(as_text=True))
        self.assertEqual(shipping_service._active_manual_holds(), [])

    def test_shipping_requests_tab_and_order_buttons(self):
        self._post("/requests", {"request_type": "inventory_discrepancy", "serial_no": "14M23235", "expected_location": "R03S03", "actual_location": "INTERNATIONAL"}, SALES)
        redirect = self.client.get("/shipping?view=requests")
        self.assertEqual(redirect.status_code, 302)
        self.assertTrue(redirect.headers["Location"].endswith("/requests"))
        tab = self.client.get("/requests?scope=all").get_data(as_text=True)
        self.assertIn("14M23235", tab)
        self.assertIn("Can&#39;t reconcile", tab)
        self.assertIn("Active manual holds", tab)
        # The nav badge counts every open request.
        self.assertIn('data-request-badge', tab)
        form = self.client.get("/requests?new=ship_request&so=SO-1").get_data(as_text=True)
        self.assertIn('value="SO-1"', form)
        self.assertIn('value="ship_request"', form)

    def test_requires_operator(self):
        response = self.client.post("/requests", data={"csrf_token": "test-token", "request_type": "order_problem"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/requests/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
