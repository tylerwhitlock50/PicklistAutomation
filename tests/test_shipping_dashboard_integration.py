import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_DB = Path(tempfile.gettempdir()) / f"picklist-shipping-dashboard-{os.getpid()}.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
os.environ["RUN_HISTORY_DB_PATH"] = str(_TEST_DB)
if os.name == "nt":
    # app.py's production scheduler uses the Linux-only fcntl module. The
    # scheduler is disabled above; this shim only lets route tests import it.
    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
    )

import app as app_module  # noqa: E402  (environment must be set before import)


def tearDownModule():
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        # Windows may retain a transient SQLite handle until interpreter exit.
        pass


def _scorecard_payload():
    payload = app_module._empty_scorecard_payload(30, "")
    payload.update({"error": None, "as_of": "2026-08-27T18:00:00+00:00"})
    values = {
        "ship_on_time": (96.2, 25, 26),
        "ship_complete": (84.0, 21, 25),
        "average_guns_per_shipment": (2.4, 60, 25),
        "single_gun_shipments": (7, 7, 25),
        "total_guns_shipped": (60, None, None),
        "total_shipments": (25, None, None),
    }
    for key, (value, numerator, denominator) in values.items():
        payload["cards"][key].update(
            {
                "value": value,
                "prior_value": value,
                "delta": 0,
                "numerator": numerator,
                "denominator": denominator,
            }
        )
    payload["cards"]["single_gun_shipments"]["companion"] = {"rate_pct": 28.0}
    return payload


def _gate_payload():
    return {
        "mode": "advisory",
        "policy_version": 2,
        "due_override_days": 1,
        "source_as_of": "2026-08-27T18:00:00+00:00",
        "error": None,
        "summary": {
            "orders": 2,
            "release": 1,
            "accumulating": 1,
            "hold": 0,
            "blocked": 0,
            "release_units": 4,
            "protected_units": 2,
            "protected_guns": 2,
            "held_ready_units": 0,
        },
        "released_orders": ["SO-1"],
        "decisions": [
            {
                "decision": "RELEASE",
                "order_id": "SO-1",
                "customer_id": "CUST-1",
                "customer_name": "Customer One",
                "label": "SHIP NOW - complete",
                "promise_ship": "2026-08-28",
                "ready_qty": 4,
                "open_qty": 4,
                "ready_guns": 4,
                "open_guns": 4,
                "next_release_date": None,
            },
            {
                "decision": "ACCUMULATING",
                "order_id": "SO-2",
                "customer_id": "LIPSEYS",
                "customer_name": "Lipsey's",
                "label": "ACCUMULATING - 2/100 customer guns protected",
                "promise_ship": "2026-09-02",
                "ready_qty": 2,
                "protected_qty": 2,
                "open_qty": 100,
                "ready_guns": 2,
                "open_guns": 100,
                "next_release_date": "2026-09-03",
            },
        ],
        "active_exceptions": [],
    }


class ReleaseFilterIntegrationTests(unittest.TestCase):
    def test_advisory_removes_token_without_filtering(self):
        query = "WHERE co.STATUS = 'R'\n__RELEASE_GATE_FILTER__\nAND col.LINE_STATUS = 'A'"
        rendered = app_module.apply_release_gate_filter(
            query, {"mode": "advisory", "released_orders": ["SO-1"]}
        )
        self.assertNotIn("__RELEASE_GATE_FILTER__", rendered)
        self.assertNotIn("co.ID IN", rendered)

    def test_enforced_filter_quotes_orders_before_allocation(self):
        query = "WHERE co.STATUS = 'R'\n__RELEASE_GATE_FILTER__\nAND col.LINE_STATUS = 'A'"
        rendered = app_module.apply_release_gate_filter(
            query,
            {
                "mode": "enforced",
                "policy_version": 4,
                "released_orders": ["so-2", "SO'1"],
            },
        )
        self.assertIn("AND co.ID IN ('SO''1', 'SO-2')", rendered)
        self.assertIn("policy v4", rendered)

    def test_enforced_empty_release_set_fails_closed(self):
        rendered = app_module.apply_release_gate_filter(
            "WHERE 1=1\n__RELEASE_GATE_FILTER__",
            {"mode": "enforced", "released_orders": []},
        )
        self.assertIn("AND 1 = 0", rendered)


class ShippingScorecardRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config.update(TESTING=True, SECRET_KEY="shipping-dashboard-test")
        cls.client = app_module.app.test_client()

    def test_scorecard_renders_all_six_metrics_and_release_queue(self):
        with (
            patch.object(app_module, "build_shipping_scorecard_payload", return_value=_scorecard_payload()),
            patch.object(app_module, "build_release_gate_payload", return_value=_gate_payload()),
        ):
            response = self.client.get("/shipping?view=scorecard&days=30")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in (
            "Ship on time",
            "Ship complete",
            "Average guns per shipment",
            "Single-gun shipments",
            "Total guns shipped",
            "Total shipments",
        ):
            self.assertIn(label, html)
        self.assertIn("Release gate", html)
        self.assertIn("SO-1", html)
        self.assertIn("ACCUMULATING", html)
        self.assertIn("Protected guns", html)
        self.assertIn("Advisory only", html)

    def test_metrics_api_exposes_scorecard_contract(self):
        with patch.object(
            app_module, "build_shipping_scorecard_payload", return_value=_scorecard_payload()
        ):
            response = self.client.get("/api/shipping/metrics?days=30")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload["cards"]), set(app_module.shipping_metrics.METRIC_DEFINITIONS))
        self.assertEqual(payload["cards"]["total_guns_shipped"]["value"], 60)


class ReleasePolicyParsingTests(unittest.TestCase):
    def test_accumulation_policy_is_normalized(self):
        result = app_module.parse_release_gate_customer_policies(
            '{"lipseys":{"accumulate":true,"min_guns":100,"sweep_weekday":3}}'
        )
        self.assertEqual(result, {
            "LIPSEYS": {"accumulate": True, "min_guns": 100, "sweep_weekday": 3}
        })

    def test_accumulate_requires_a_json_boolean(self):
        with self.assertRaisesRegex(ValueError, "accumulate must be true or false"):
            app_module.parse_release_gate_customer_policies(
                '{"LIPSEYS":{"accumulate":"yes"}}'
            )


if __name__ == "__main__":
    unittest.main()
