"""The four-tab nav (Work / Reports / Lookup / Requests), the Today page, the
Lookup router page, run history, and the redirects for retired Shipping
sub-views."""

import os
import tempfile
import unittest
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-nav-routes-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
os.environ.pop("OPERATOR_ROSTER_JSON", None)
os.environ.pop("TEAMS_WEBHOOK_URL", None)

from picklist import app as app_module  # noqa: E402
from picklist import db  # noqa: E402
from picklist import features  # noqa: E402


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


class NavRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        for feature_def in features.FEATURE_FLAGS.values():
            db.delete_setting(feature_def["setting_key"])

    def test_four_tabs_and_work_subnav_on_run_page(self):
        html = self.client.get("/").get_data(as_text=True)
        for label in (">Work<", ">Reports<", ">Lookup<", "Requests"):
            self.assertIn(label, html)
        for retired in (">Dashboard</a>", ">Shipping</a>", ">Serial Lookup</a>", ">Allocation</a>"):
            self.assertNotIn(retired, html)
        # Work group is active, with Run picklist highlighted in the sub-nav.
        self.assertIn('class="topnav-sub is-active" href="/"', html)
        self.assertIn(">Today<", html)
        self.assertIn(">Pick orders<", html)
        self.assertIn(">Verify boxes<", html)
        self.assertIn(">Audit<", html)
        # Settings moved into the header, and the report banners are gone.
        self.assertIn('class="topnav-settings"', html)
        self.assertNotIn("data-shipping-scorecard-banner", html)
        self.assertNotIn("data-excess-banner", html)

    def test_today_page(self):
        response = self.client.get("/work")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("What needs doing today?", html)
        self.assertIn("Run picklist", html)
        self.assertIn("Pick orders", html)
        self.assertIn("Verify boxes", html)
        self.assertIn("Open audit", html)
        self.assertIn('class="topnav-sub is-active" href="/work"', html)

    def test_reports_group_subnav(self):
        html = self.client.get("/shipping?view=scorecard").get_data(as_text=True)
        self.assertIn('class="topnav-tab is-active" href="/shipping?view=scorecard"', html)
        for label in (">Scorecard<", ">Holds<", ">Shortages<", ">Reconciliation<", ">Excess packlists<", ">Staged shipments<", ">Audit analytics<", ">Run history<"):
            self.assertIn(label, html)
        # The in-page view toolbar is gone; the sub-nav replaces it.
        self.assertNotIn('aria-label="Shipping views"', html)

    def test_run_history_page(self):
        response = self.client.get("/runs")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Picklist run history", html)
        self.assertIn('class="topnav-sub is-active" href="/runs"', html)

    def test_lookup_page_and_subnav(self):
        response = self.client.get("/lookup")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Paste what you have", html)
        for label in (">Search<", ">Orders<", ">Shipments<", ">Stock<", ">Serial history<", ">Allocation<"):
            self.assertIn(label, html)
        self.assertIn('data-as="serial"', html)
        self.assertIn("/orders/__ID__", html)

    def test_retired_shipping_views_redirect(self):
        for view, target in (("work", "/work"), ("holds", "/orders?from_report=1"), ("requests", "/requests")):
            response = self.client.get(f"/shipping?view={view}")
            self.assertEqual(response.status_code, 302, view)
            self.assertTrue(response.headers["Location"].endswith(target), (view, response.headers["Location"]))
        # Unknown views fall back to the scorecard instead of the old overview.
        html = self.client.get("/shipping?view=nope").get_data(as_text=True)
        self.assertIn("Management scorecard", html)

    def test_feature_flags_trim_nav(self):
        db.set_setting("feature_orders_enabled", "false")
        db.set_setting("feature_audit_enabled", "false")
        db.set_setting("feature_requests_enabled", "false")
        try:
            html = self.client.get("/work").get_data(as_text=True)
            self.assertNotIn(">Requests", html)
            self.assertNotIn(">Audit<", html)
            self.assertNotIn("Open audit", html)
            lookup = self.client.get("/lookup").get_data(as_text=True)
            self.assertNotIn(">Orders<", lookup)
            self.assertIn(">Serial history<", lookup)
            # Requests is its own switch: blocked page and API, nav tab gone.
            self.assertEqual(self.client.get("/requests").status_code, 302)
            self.assertEqual(self.client.get("/api/requests").status_code, 404)
        finally:
            db.set_setting("feature_requests_enabled", "true")

    def test_requests_flag_independent_of_orders(self):
        db.set_setting("feature_orders_enabled", "true")
        db.set_setting("feature_requests_enabled", "false")
        try:
            html = self.client.get("/work").get_data(as_text=True)
            self.assertNotIn(">Requests", html)
            lookup = self.client.get("/lookup").get_data(as_text=True)
            self.assertIn(">Orders<", lookup)
        finally:
            db.set_setting("feature_requests_enabled", "true")


if __name__ == "__main__":
    unittest.main()
