import sqlite3
import unittest
from unittest.mock import patch

from picklist.stores import verify_store


class VerificationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.original = verify_store._get_conn
        verify_store.initialize(lambda: self.connection)

    def tearDown(self):
        verify_store._get_conn = self.original
        self.connection.close()

    def start(self, packlist="PL-1"):
        return verify_store.start_session(packlist, {}, [
            {"TRACE_ID": "SERIAL-1", "PART_ID": "GUN"},
            {"TRACE_ID": "SERIAL-2", "PART_ID": "GUN"},
        ], operator="PICKER")

    def test_cancel_preserves_history_without_a_verdict_and_restart_is_fresh(self):
        session = self.start()
        verify_store.record_scan(session, "SERIAL-1", operator="PICKER")
        cancelled = verify_store.cancel_session(session, operator="LEAD", reason="Box moved")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["closed_by"], "LEAD")
        self.assertEqual(cancelled["closed_reason"], "Box moved")
        self.assertIsNone(cancelled["outcome"])
        counts = verify_store.compute_counts(session)
        self.assertEqual((counts["verified"], counts["pending"], counts["missing"]), (1, 1, 0))
        self.assertEqual(len(verify_store.get_scans(session)), 1)
        self.assertEqual(verify_store.unfinished_sessions(), [])
        for action in (lambda: verify_store.record_scan(session, "SERIAL-2"),
                       lambda: verify_store.complete_session(session),
                       lambda: verify_store.cancel_session(session, operator="LEAD", reason="Again")):
            with self.assertRaises(ValueError):
                action()
        new_session = self.start()
        self.assertNotEqual(new_session, session)
        self.assertEqual(verify_store.compute_counts(new_session)["verified"], 0)
        self.assertEqual(verify_store.compute_counts(new_session)["remaining"], 2)

    def test_repeated_start_resumes_active_snapshot(self):
        session = self.start()
        verify_store.record_scan(session, "SERIAL-1")
        self.assertEqual(self.start(), session)
        self.assertEqual(verify_store.compute_counts(session)["verified"], 1)
        self.assertEqual(len(verify_store.unfinished_sessions()), 1)

    def test_reason_operator_and_active_status_are_required(self):
        session = self.start()
        for name, reason in (("", "reason"), ("LEAD", ""), ("LEAD", "  ")):
            with self.assertRaises(ValueError):
                verify_store.cancel_session(session, operator=name, reason=reason)
        verify_store.complete_session(session)
        with self.assertRaises(ValueError):
            verify_store.cancel_session(session, operator="LEAD", reason="reason")
        self.assertEqual(verify_store.get_session(session)["outcome"], "issues")
        with self.assertRaises(LookupError):
            verify_store.cancel_session(9999, operator="LEAD", reason="reason")

    def test_unfinished_work_has_no_recent_history_cap_and_uses_latest_scan(self):
        with patch.object(verify_store, "_now_iso", return_value="2026-10-04T12:00:00+00:00"):
            sessions = [self.start(f"PL-{i}") for i in range(12)]
        with patch.object(verify_store, "_now_iso", return_value="2026-10-05T12:00:00+00:00"):
            verify_store.record_scan(sessions[0], "SERIAL-1")
        unfinished = verify_store.unfinished_sessions()
        self.assertEqual(len(unfinished), 12)
        self.assertEqual(next(row for row in unfinished if row["id"] == sessions[0])["last_activity"], "2026-10-05T12:00:00+00:00")

    def test_existing_schema_gets_cancellation_columns(self):
        session = self.start()
        with self.connection:
            self.connection.execute("ALTER TABLE verify_sessions DROP COLUMN closed_by")
            self.connection.execute("ALTER TABLE verify_sessions DROP COLUMN closed_reason")
        verify_store.initialize(lambda: self.connection)
        cancelled = verify_store.cancel_session(session, operator="LEAD", reason="Migrated")
        self.assertEqual(cancelled["closed_reason"], "Migrated")


if __name__ == "__main__":
    unittest.main()
