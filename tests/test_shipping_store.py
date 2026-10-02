import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from picklist.stores import shipping_store


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

    def test_sticky_serial_reservation_reconciliation(self):
        first = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc).isoformat()
        desired = [{
            "serial_no": "SER-1",
            "part_id": "GUN-A",
            "customer_id": "MAJOR",
            "cust_order_id": "SO-1",
            "line_no": "1",
            "first_assigned_at": first,
            "accumulation_started_at": first,
        }]
        live = [{
            "SERIAL_NO": "SER-1",
            "PART_ID": "GUN-A",
            "WAREHOUSE_ID": "SHIPPING",
            "LOCATION_ID": "R01-A",
        }]
        assigned = shipping_store.sync_serial_reservations(
            desired=desired,
            live_serials=live,
            evaluated_at=first,
            policy_version=5,
        )
        self.assertEqual(assigned["assigned"], 1)
        self.assertEqual(shipping_store.serial_reservations_for_gate()[0]["serial_no"], "SER-1")

        second = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc).isoformat()
        kept = shipping_store.sync_serial_reservations(
            desired=desired,
            live_serials=live,
            evaluated_at=second,
            policy_version=5,
        )
        row = shipping_store.serial_reservations_for_gate()[0]
        self.assertEqual(kept["kept"], 1)
        self.assertEqual(row["first_assigned_at"], first)
        self.assertEqual(row["last_verified_at"], second)

        third = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc).isoformat()
        fulfilled = shipping_store.sync_serial_reservations(
            desired=[],
            live_serials=[],
            evaluated_at=third,
            policy_version=5,
        )
        self.assertEqual(fulfilled["fulfilled"], 1)
        self.assertEqual(shipping_store.serial_reservations_for_gate(), [])
        self.assertEqual(shipping_store.recent_serial_reservations()[0]["status"], "fulfilled")

    def _evaluation_payload(self, order="SO-1"):
        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "advisory",
            "policy_version": 1,
            "summary": {"orders": 1},
            "decisions": [{
                "order_id": order, "customer_id": "CUST", "decision": "RELEASE",
                "reason_code": "complete", "label": "SHIP NOW - complete",
                "open_qty": 1, "ready_qty": 1,
            }],
        }

    def test_evaluation_history_is_pruned_with_cascade(self):
        for index in range(7):
            shipping_store.record_evaluation(
                self._evaluation_payload(f"SO-{index}"), source_as_of="now"
            )
        shipping_store.prune_evaluations(keep=3)
        conn = sqlite3.connect(self.db_path)
        try:
            evaluations = conn.execute(
                "SELECT COUNT(*) FROM release_gate_evaluations"
            ).fetchone()[0]
            decisions = conn.execute(
                "SELECT COUNT(*) FROM release_gate_decisions"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(evaluations, 3)
        self.assertEqual(decisions, 3)
        self.assertEqual(
            shipping_store.latest_evaluation()["decisions"][0]["order_id"], "SO-6"
        )

    def test_record_evaluation_applies_retention_cap(self):
        original = shipping_store.EVALUATION_HISTORY_KEEP
        shipping_store.EVALUATION_HISTORY_KEEP = 2
        try:
            for index in range(4):
                shipping_store.record_evaluation(
                    self._evaluation_payload(f"SO-{index}"), source_as_of="now"
                )
        finally:
            shipping_store.EVALUATION_HISTORY_KEEP = original
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM release_gate_evaluations"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)

    def test_release_ledger_roundtrip_and_idempotency(self):
        released = [
            {"order_id": "SO-1", "customer_id": "STORE", "ship_to_id": "MAIN"},
            {"order_id": "SO-2", "customer_id": "STORE", "ship_to_id": ""},
            {"order_id": "", "customer_id": "STORE", "ship_to_id": "MAIN"},
        ]
        written = shipping_store.record_released_ship_tos(
            run_id=7, released_decisions=released, released_date=date.today()
        )
        self.assertEqual(written, 2)
        # Re-recording the same run must not raise or duplicate.
        shipping_store.record_released_ship_tos(
            run_id=7, released_decisions=released, released_date=date.today()
        )
        rows = shipping_store.recent_released_ship_tos()
        self.assertEqual(len(rows), 2)
        by_ship_to = {row["SHIP_TO_ID"]: row for row in rows}
        self.assertEqual(set(by_ship_to), {"MAIN", "DEFAULT"})
        self.assertEqual(by_ship_to["MAIN"]["CUSTOMER_ID"], "STORE")
        self.assertEqual(
            by_ship_to["MAIN"]["LAST_SHIPPED_DATE"], date.today().isoformat()
        )
        self.assertEqual(by_ship_to["MAIN"]["SOURCE"], "picklist")

    def test_release_ledger_prunes_old_rows(self):
        old_day = date.today() - timedelta(days=45)
        shipping_store.record_released_ship_tos(
            run_id=1,
            released_decisions=[
                {"order_id": "SO-OLD", "customer_id": "STORE", "ship_to_id": "MAIN"}
            ],
            released_date=old_day,
        )
        shipping_store.record_released_ship_tos(
            run_id=2,
            released_decisions=[
                {"order_id": "SO-NEW", "customer_id": "STORE", "ship_to_id": "MAIN"}
            ],
            released_date=date.today(),
        )
        conn = sqlite3.connect(self.db_path)
        try:
            remaining = conn.execute(
                "SELECT cust_order_id FROM release_gate_release_log"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row[0] for row in remaining], ["SO-NEW"])


if __name__ == "__main__":
    unittest.main()
