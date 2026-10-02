import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-identity-routes-{os.getpid()}.db"
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


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


ROSTER = [
    {"name": "Holly", "team": "sales", "email": "holly@example.com"},
    {"name": "Richard", "team": "shipping", "email": ""},
]


class IdentityRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        app_module.set_setting("operator_roster_json", json.dumps(ROSTER))

    def tearDown(self):
        app_module.delete_setting("operator_roster_json")
        app_module.delete_setting("teams_webhook_url")
        for feature_def in app_module.FEATURE_FLAGS.values():
            app_module.delete_setting(feature_def["setting_key"])
        notifier.configure(get_config_value=app_module.get_config_value, transport=None)

    @staticmethod
    def _all_features_on():
        return {f"feature_{name}": "on" for name in app_module.FEATURE_FLAGS}

    def _unlock_settings(self):
        with self.client.session_transaction() as sess:
            sess[app_module.SETTINGS_SESSION_KEY] = True
            sess["_csrf_token"] = "test-token"

    def test_dashboard_renders_operator_picker(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Who are you?", html)
        self.assertIn('data-team="sales"', html)
        self.assertIn("X-Operator", html)

    def test_dashboard_without_roster_offers_free_text_name(self):
        app_module.delete_setting("operator_roster_json")
        html = self.client.get("/").get_data(as_text=True)
        # No roster: a typed-name box and team picker replace the roster select.
        self.assertNotIn('<select class="topnav-operator-select" data-operator-picker', html)
        self.assertIn("data-operator-input", html)
        self.assertIn("data-operator-team-picker", html)
        self.assertIn('<option value="sales">Inside Sales</option>', html)

    def test_api_me(self):
        anonymous = self.client.get("/api/me").get_json()
        self.assertIsNone(anonymous["operator"])
        self.assertEqual(anonymous["roster_size"], 2)

        known = self.client.get("/api/me", headers={"X-Operator": "holly"}).get_json()
        self.assertEqual(known["operator"]["name"], "Holly")
        self.assertEqual(known["operator"]["team"], "sales")
        self.assertTrue(known["operator"]["known"])

    def test_settings_page_renders_teams_and_roster_sections(self):
        self._unlock_settings()
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Microsoft Teams", html)
        self.assertIn("Operator roster", html)
        self.assertIn('value="Holly"', html)
        self.assertIn("teams_event_hold_created", html)
        self.assertIn("api/settings/test-teams", html)

    def test_settings_save_roster_and_teams(self):
        self._unlock_settings()
        payload = {
            **self._all_features_on(),
            "csrf_token": "test-token",
            "action": "save",
            "operator_roster_json": json.dumps(
                [{"name": "Noah", "team": "shipping", "email": ""}, {"name": "Lunden", "team": "sales"}]
            ),
            "teams_webhook_url": "https://prod-00.westus.logic.azure.com/workflows/abc",
            "teams_event_hold_created": "on",
            "teams_event_shipped_digest": "on",
            "teams_digest_time": "16:45",
            "app_public_url": "http://ops.local:8081/",
            "release_gate_mode": "advisory",
            "release_gate_due_override_days": "1",
            "release_gate_customer_policies_json": "{}",
        }
        response = self.client.post("/settings", data=payload)
        self.assertEqual(response.status_code, 302)
        roster = app_module.get_operator_roster()
        self.assertEqual([m["name"] for m in roster], ["Lunden", "Noah"])
        self.assertEqual(app_module.get_setting("teams_enabled_events"), "hold_created,shipped_digest")
        self.assertEqual(app_module.get_teams_digest_time(), "16:45")
        self.assertEqual(app_module.get_setting("app_public_url"), "http://ops.local:8081")
        self.assertTrue(notifier.webhook_url().startswith("https://prod-00"))

    def test_settings_save_rejects_bad_roster(self):
        self._unlock_settings()
        payload = {
            **self._all_features_on(),
            "csrf_token": "test-token",
            "action": "save",
            "operator_roster_json": json.dumps([{"name": "Pat", "team": "warehouse"}]),
            "release_gate_mode": "advisory",
            "release_gate_due_override_days": "1",
            "release_gate_customer_policies_json": "{}",
        }
        response = self.client.post("/settings", data=payload, follow_redirects=True)
        self.assertIn("Operator roster", response.get_data(as_text=True))
        self.assertEqual([m["name"] for m in app_module.get_operator_roster()], ["Holly", "Richard"])

    def test_test_teams_endpoint(self):
        self._unlock_settings()
        sent = []
        notifier.configure(
            get_config_value=app_module.get_config_value,
            transport=lambda url, payload: sent.append(url),
        )
        headers = {"X-CSRF-Token": "test-token"}
        missing = self.client.post("/api/settings/test-teams", json={}, headers=headers)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("required", missing.get_json()["message"])

        ok = self.client.post(
            "/api/settings/test-teams",
            json={"webhook_url": "https://example.test/hook"},
            headers=headers,
        )
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertEqual(sent, ["https://example.test/hook"])

    def test_test_teams_requires_unlock(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        response = self.client.post(
            "/api/settings/test-teams", json={}, headers={"X-CSRF-Token": "test-token"}
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
