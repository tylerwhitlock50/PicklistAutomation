import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-pick-abandon-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)

from picklist import app as app_module  # noqa: E402
from picklist import db  # noqa: E402
from picklist.stores import pick_store  # noqa: E402


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
        db.set_setting("feature_shipping_enabled", "true")
        pick_store.initialize(db.get_sqlite_conn)
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        self.session_id = pick_store.start_order_session(
            plan_rows=[_row("SO-9001", "P-1", "components")],
            selected_orders=["SO-9001"], source_runs={"components": 1}, operator="PICKER",
        )

    def tearDown(self):
        if pick_store.get_session(self.session_id)['status'] == 'active':
            pick_store.abandon_session(self.session_id, operator='TEST', reason='Fixture cleanup')

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

    def test_single_gun_api_requires_matching_upc_serial_without_tote(self):
        row = _row("SO-GUN-UI", "GUN-1", "guns", qty=2)
        row["UPC"] = "123456789"
        session = pick_store.start_order_session(plan_rows=[row], selected_orders=["SO-GUN-UI"],
            source_runs={"guns": 1}, operator="GUNNER", pick_type="guns")
        url = f"/api/pick/session/{session}/scan"
        payload = {"scan": "SERIAL-UI", "order": "SO-GUN-UI", "location": "A01",
                   "request_id": "ui-pair", "operator": "GUNNER"}
        headers = {"X-CSRF-Token": "test-token", "X-Operator": "GUNNER"}
        with patch("picklist.routes.pick._resolve_pick_candidates",
                   return_value=("SERIAL-UI", [{"part_id": "GUN-1", "locations": ["A01"]}], False)):
            self.assertEqual(self.client.post(url, json=payload, headers=headers).status_code, 400)
            payload["upc"] = "123456789"
            result = self.client.post(url, json=payload, headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["result"], "ok")
            replay = self.client.post(url, json=payload, headers=headers)
            self.assertTrue(replay.get_json()["idempotent_replay"])
        page = self.client.get(f"/pick/session/{session}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="upc-input"', page.get_data(as_text=True))

    def test_part_number_pairing_and_visible_sorted_lines(self):
        rows = [_row('SO-PATH', 'LOW', 'guns'), _row('SO-PATH', 'HIGH', 'guns')]
        rows[0]['Location'] = 'R01S01'
        rows[1]['Location'] = 'R09S05'
        sid = pick_store.start_order_session(plan_rows=rows, selected_orders=['SO-PATH'],
            source_runs={'guns': 1}, operator='PATH PICKER', pick_type='guns')
        lines = pick_store.get_lines(sid)
        high = next(line for line in lines if line['part_id'] == 'HIGH')
        page = self.client.get(f'/pick/session/{sid}').get_data(as_text=True)
        self.assertIn('Pick these lines in order', page)
        self.assertNotIn('<summary>Picklist lines', page)
        self.assertLess(page.index('data-part="HIGH"'), page.index('data-part="LOW"'))
        headers = {'X-CSRF-Token': 'test-token', 'X-Operator': 'PATH PICKER'}
        with patch('picklist.routes.pick._resolve_pick_candidates',
                   return_value=('SERIAL-PATH', [{'part_id': 'HIGH', 'locations': ['R09S05']}], False)), \
             patch('picklist.routes.pick.run_erp_query_file') as lookup:
            response = self.client.post(f'/api/pick/session/{sid}/scan', headers=headers,
                json={'scan': 'SERIAL-PATH', 'order': 'SO-PATH', 'location': 'R09S05',
                      'item': 'HIGH', 'line_id': high['id'], 'request_id': 'path-pair'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['result'], 'ok')
            lookup.assert_not_called()
        self.assertEqual(next(line for line in pick_store.get_lines(sid) if line['id'] == high['id'])['picked_qty'], 1)
        self.assertIn('class="pick-line-done"', self.client.get(f'/pick/session/{sid}').get_data(as_text=True))

    def test_form_abandon_from_shipping_page(self):
        response = self.client.post(
            f"/pick/session/{self.session_id}/abandon",
            data={"csrf_token": "test-token", "reason": "Wrong wave", "operator": "Lead"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(pick_store.get_session(self.session_id)["status"], "abandoned")
        listing = self.client.get("/shipping?view=pick&pick_type=components").get_data(as_text=True)
        self.assertIn("closed short", listing)


if __name__ == "__main__":
    unittest.main()
