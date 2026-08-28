import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import shipping_store


class ShippingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "shipping.db"

        def connection():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        shipping_store.initialize(connection)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_policy_versions_roundtrip(self):
        version = shipping_store.save_policy_config(
            mode="advisory",
            due_override_days=1,
            customer_policies={
                "big": {"accumulate": True, "min_guns": 24, "sweep_weekday": 3}
            },
            changed_by="TYLER",
        )
        loaded = shipping_store.latest_policy_config()
        self.assertEqual(loaded["version"], version)
        self.assertEqual(loaded["customer_policies"]["BIG"]["min_guns"], 24)
        self.assertTrue(loaded["customer_policies"]["BIG"]["accumulate"])

    def test_evaluation_and_decision_audit_roundtrip(self):
        payload = {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "advisory",
            "policy_version": 3,
            "summary": {"orders": 1, "release": 1},
            "decisions": [{
                "order_id": "SO-1", "customer_id": "CUST", "decision": "RELEASE",
                "reason_code": "complete", "label": "SHIP NOW - complete",
                "open_qty": 2, "ready_qty": 2,
            }],
        }
        evaluation_id = shipping_store.record_evaluation(payload, source_as_of="now")
        loaded = shipping_store.latest_evaluation()
        self.assertEqual(loaded["id"], evaluation_id)
        self.assertEqual(loaded["decisions"][0]["reason_code"], "complete")

    def test_accumulating_decision_evidence_roundtrip(self):
        payload = {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "advisory",
            "policy_version": 4,
            "summary": {"orders": 1, "accumulating": 1, "protected_guns": 12},
            "decisions": [{
                "order_id": "SO-100", "customer_id": "LIPSEYS",
                "decision": "ACCUMULATING",
                "reason_code": "accumulating_for_customer_release",
                "label": "ACCUMULATING - 12/100 customer guns protected",
                "open_qty": 100, "ready_qty": 12,
                "protected_qty": 12, "protected_guns": 12,
            }],
        }
        shipping_store.record_evaluation(payload, source_as_of="now")
        loaded = shipping_store.latest_evaluation()
        row = loaded["decisions"][0]
        self.assertEqual(row["decision"], "ACCUMULATING")
        self.assertEqual(row["protected_guns"], 12)

    def test_exception_lifecycle(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        exception_id = shipping_store.add_exception(
            cust_order_id="so-1", reason="Customer expedite",
            created_by="JP", expires_at=expires.isoformat(),
        )
        self.assertEqual(shipping_store.active_exceptions()[0]["cust_order_id"], "SO-1")
        self.assertTrue(shipping_store.revoke_exception(exception_id, "JP"))
        self.assertEqual(shipping_store.active_exceptions(), [])

    def test_metric_snapshot_roundtrip(self):
        payload = {"cards": {"total_shipments": {"value": 10}}}
        shipping_store.save_metric_snapshot(
            snapshot_date=date(2026, 8, 27), period_days=30,
            payload=payload, source_as_of="now",
        )
        self.assertEqual(
            shipping_store.get_metric_snapshot(date(2026, 8, 27), 30), payload
        )


if __name__ == "__main__":
    unittest.main()
