import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from picklist.services.unfinished_work import build_unfinished_work


class UnfinishedWorkTests(unittest.TestCase):
    def test_idle_threshold_and_sort_use_activity_instead_of_start(self):
        base = {"operator": "PICKER", "done_units": 1, "planned_units": 3}
        picks = [{**base, "id": 1, "order_ids": "SO-1", "query_type": "guns", "assigned_operators": "NEW PICKER",
                  "started_at": "2026-10-01T00:00:00Z", "last_activity": "2026-10-05T11:30:00Z"}]
        verifies = [{**base, "id": 2, "packlist_id": "PL-2", "last_activity": "2026-10-05T02:00:00"},
                    {**base, "id": 3, "packlist_id": "PL-3", "last_activity": "2026-10-04T18:00:00-06:00"}]
        with patch("picklist.services.unfinished_work.pick_store.unfinished_sessions", return_value=picks), \
             patch("picklist.services.unfinished_work.verify_store.unfinished_sessions", return_value=verifies):
            result = build_unfinished_work(datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
        self.assertEqual([row["id"] for row in result], [3, 2, 1])
        self.assertEqual([row["stale"] for row in result], [True, True, False])
        self.assertEqual(result[-1]["operator_display"], "NEW PICKER")
        self.assertEqual(result[-1]["idle_hours"], 0.5)

    def test_audits_use_datetime_activity_and_exact_idle_boundary(self):
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        audits = [{"id": 337, "scope": "SHIPPING / INTERNATIONAL", "operator": "AUDITOR",
                   "last_activity": datetime(2026, 10, 5, 2, tzinfo=timezone.utc),
                   "done_units": 4, "planned_units": 9},
                  {"id": 338, "label": "New audit", "last_activity": now,
                   "done_units": 0, "planned_units": 3}]
        with patch("picklist.services.unfinished_work.pick_store.unfinished_sessions", return_value=[]), \
             patch("picklist.services.unfinished_work.verify_store.unfinished_sessions", return_value=[]), \
             patch("picklist.services.unfinished_work.audit_store.unfinished_sessions", return_value=audits):
            result = build_unfinished_work(now)
        self.assertEqual([row["id"] for row in result], [337, 338])
        self.assertEqual([row["stale"] for row in result], [True, False])
        self.assertEqual(result[0]["label"], "SHIPPING / INTERNATIONAL")
        self.assertEqual(result[0]["kind"], "audit")
        self.assertEqual(result[1]["idle_hours"], 0)

    def test_optional_audit_failure_retains_local_work_and_warns(self):
        picks = [{"id": 1, "order_ids": "SO-1", "last_activity": "2026-10-05T12:00:00Z"}]
        with patch("picklist.services.unfinished_work.pick_store.unfinished_sessions", return_value=picks), \
             patch("picklist.services.unfinished_work.verify_store.unfinished_sessions", return_value=[]), \
             patch("picklist.services.unfinished_work.audit_store.unfinished_sessions", side_effect=ConnectionError("offline")):
            result = build_unfinished_work(datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
        self.assertEqual([row["id"] for row in result], [1])
        self.assertTrue(result.audit_unavailable)
