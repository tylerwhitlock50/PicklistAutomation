import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["DATABASE_URL"] = ""
os.environ["RUN_HISTORY_DB_PATH"] = str(Path(tempfile.gettempdir()) / f"unfinished-work-{os.getpid()}.db")

from picklist.app import app
from picklist import db
from picklist.services import shipping_service
from picklist.stores import pick_store, verify_store


class UnfinishedWorkIntegrationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        db.set_setting("feature_shipping_enabled", "true")
        pick_store.initialize(db.get_sqlite_conn)
        verify_store.initialize(db.get_sqlite_conn)
        self.packlist = "PL-" + uuid.uuid4().hex[:10].upper()
        self.rows = [{"TRACE_ID": "SERIAL-1", "PART_ID": "GUN", "PACKLIST_ID": self.packlist},
                     {"TRACE_ID": "SERIAL-2", "PART_ID": "GUN", "PACKLIST_ID": self.packlist}]
        self.session = verify_store.start_session(self.packlist, {}, self.rows, operator="PICKER")
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "token"
        self.headers = {"X-CSRF-Token": "token", "X-Operator": "LEAD", "X-Operator-Team": "shipping"}

    def test_cancel_api_preserves_scans_and_blocks_stale_clients(self):
        scan = f"/api/verify/session/{self.session}/scan"
        self.assertEqual(self.client.post(scan, json={"scan": "SERIAL-1"}, headers=self.headers).status_code, 200)
        cancel = f"/api/verify/session/{self.session}/cancel"
        self.assertEqual(self.client.post(cancel, json={"reason": "wrong box"}).status_code, 400)
        self.assertEqual(self.client.post(cancel, json={}, headers=self.headers).status_code, 409)
        response = self.client.post(cancel, json={"reason": "Box moved"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelled")
        self.assertEqual(verify_store.get_session(self.session)["closed_by"], "LEAD")
        self.assertEqual(verify_store.compute_counts(self.session)["missing"], 0)
        self.assertEqual(len(verify_store.get_scans(self.session)), 1)
        self.assertEqual(self.client.post(scan, json={"scan": "SERIAL-2"}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(f"/api/verify/session/{self.session}/complete", json={}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(cancel, json={"reason": "again"}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/api/verify/session/999999/cancel", json={"reason": "bad"}, headers=self.headers).status_code, 404)
        html = self.client.get(response.get_json()["redirect"]).get_data(as_text=True)
        self.assertIn("Verification cancelled", html)
        self.assertIn("Box moved", html)
        self.assertIn("Restart verification", html)
        self.assertNotIn('id="scan-input"', html)

    def test_cancelled_packlist_needs_verification_and_has_restart_action(self):
        response = self.client.post(f"/verify/session/{self.session}/cancel", data={
            "csrf_token": "token", "operator": "LEAD", "reason": "Stopped", "return_to": "work"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/work"))
        with patch.object(shipping_service, "_fetch_packlists_created_on", return_value=[
                {"PACKLIST_ID": self.packlist, "SERIAL_COUNT": 2, "LINE_COUNT": 1, "SHIPPER_STATUS": "S"}]):
            payload = shipping_service.build_verify_daily_payload(None)
            self.assertEqual(payload["summary"]["in_progress"], 0)
            self.assertEqual(payload["summary"]["not_verified"], 1)
            html = self.client.get("/shipping?view=verify").get_data(as_text=True)
        self.assertIn("Restart verification", html)
        self.assertIn("Cancelled attempt", html)

    def test_scan_packlist_resumes_active_attempt_and_cancelled_attempt_restarts(self):
        with patch("picklist.routes.verify.run_erp_query_file", return_value=pd.DataFrame(self.rows)), \
             patch("picklist.routes.verify.build_pick_order_queue", return_value={"plan_rows": []}):
            def start():
                return self.client.post("/verify/session/start", data={
                    "csrf_token": "token", "packlist_id": self.packlist, "operator": "PICKER"})
            self.assertTrue(start().location.endswith(f"/verify/session/{self.session}"))
            verify_store.cancel_session(self.session, operator="LEAD", reason="Reset")
            response = start()
            self.assertEqual(response.status_code, 302)
            self.assertFalse(response.location.endswith(f"/verify/session/{self.session}"))

    def test_old_work_is_visible_beyond_recent_history_and_can_be_closed(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=11)).isoformat()
        with db.get_sqlite_conn() as connection:
            connection.execute("UPDATE verify_sessions SET started_at = ? WHERE id = ?", (old, self.session))
        for index in range(12):
            verify_store.start_session(f"{self.packlist}-{index}", {}, self.rows, operator="OTHER")
        html = self.client.get("/work").get_data(as_text=True)
        self.assertIn(self.packlist, html)
        self.assertIn("Needs review", html)
        self.assertIn("Last activity", html)
        self.assertIn(f'/verify/session/{self.session}/cancel', html)
        verify_store.cancel_session(self.session, operator="LEAD", reason="Cleanup")
        html = self.client.get("/work").get_data(as_text=True)
        self.assertNotIn(f'/verify/session/{self.session}/cancel', html)


if __name__ == "__main__":
    unittest.main()
