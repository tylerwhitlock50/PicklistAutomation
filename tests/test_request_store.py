import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import request_service
import request_store


class RequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.gettempdir()) / f"picklist-requests-{os.getpid()}-{id(self)}.db"
        request_store.initialize(lambda: sqlite3.connect(self.db_path))
        request_store.configure(sla_overrides={})

    def tearDown(self):
        try:
            self.db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_ship_request_defaults(self):
        req = request_store.create_request(
            request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="so-131597",
            fields={"needed_by": "2026-10-06", "expedite": "on", "service_level": "2day"},
            body="Dealer needs it for a show",
        )
        self.assertEqual(req["status"], "open")
        self.assertEqual(req["owner_team"], "shipping")
        self.assertEqual(req["cust_order_id"], "SO-131597")
        self.assertEqual(req["title"], "Expedite SO-131597 (2nd day air)")
        self.assertTrue(req["fields"]["expedite"])
        self.assertIsNotNone(req["sla_due_at"])
        self.assertEqual(req["events"][0]["event_type"], "created")
        self.assertFalse(req["overdue"])

    def test_ship_request_requires_order(self):
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="ship_request", actor="Holly", fields={})

    def test_hold_requires_order_or_wo_and_kind_and_expiry_bounds(self):
        with self.assertRaises(ValueError) as ctx:
            request_store.create_request(request_type="hold_exception", actor="Lunden", fields={"exception_kind": "marketing"})
        self.assertIn("No SO, no set-aside", str(ctx.exception))
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="hold_exception", actor="Lunden", cust_order_id="SO-1", fields={"exception_kind": "because"})
        far = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="hold_exception", actor="Lunden", cust_order_id="SO-1", fields={"exception_kind": "vip", "expires_at": far})
        req = request_store.create_request(request_type="hold_exception", actor="Lunden", actor_team="sales", work_order_id="716513", fields={"exception_kind": "marketing"})
        self.assertEqual(req["work_order_id"], "716513")
        self.assertIn("Marketing", req["title"])
        self.assertTrue(req["fields"]["expires_at"])

    def test_discrepancy_and_problem(self):
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="inventory_discrepancy", actor="Holly", fields={})
        disc = request_store.create_request(request_type="inventory_discrepancy", actor="Holly", serial_no="14m23235", fields={"expected_location": "r03s03", "actual_location": "international", "qty": "1"})
        self.assertEqual(disc["serial_no"], "14M23235")
        self.assertEqual(disc["fields"]["actual_location"], "INTERNATIONAL")
        prob = request_store.create_request(request_type="order_problem", actor="Harmony", cust_order_id="SO-2", fields={"problem_kind": "wrong_tracking"})
        self.assertEqual(prob["owner_team"], "sales")
        self.assertIn("tracking", prob["title"].lower())
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="order_problem", actor="Harmony", fields={"problem_kind": "nope"})
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="bogus", actor="Harmony")
        with self.assertRaises(ValueError):
            request_store.create_request(request_type="order_problem", actor="")

    def test_transitions_and_stamps(self):
        req = request_store.create_request(request_type="order_problem", actor="Harmony", fields={})
        rid = req["id"]
        with self.assertRaises(ValueError):
            request_store.transition(rid, "bogus", actor="Noah")
        acked = request_store.transition(rid, "acknowledged", actor="Noah", actor_team="shipping", note="looking")
        self.assertIsNotNone(acked["acknowledged_at"])
        started = request_store.transition(rid, "in_progress", actor="Noah")
        self.assertIsNotNone(started["started_at"])
        with self.assertRaises(ValueError):
            request_store.transition(rid, "acknowledged", actor="Noah")
        done = request_store.transition(rid, "done", actor="Noah", resolution="Fixed the tracking")
        self.assertIsNotNone(done["closed_at"])
        self.assertEqual(done["resolution"], "Fixed the tracking")
        self.assertFalse(done["is_open"])
        with self.assertRaises(ValueError):
            request_store.transition(rid, "in_progress", actor="Noah")
        reopened = request_store.transition(rid, "open", actor="Harmony", note="still wrong")
        self.assertIsNone(reopened["closed_at"])
        self.assertEqual([e["event_type"] for e in reopened["events"]], ["created", "transition", "transition", "transition", "transition"])
        with self.assertRaises(LookupError):
            request_store.transition(999, "done", actor="Noah")

    def test_assign_comment_and_list_filters(self):
        a = request_store.create_request(request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="SO-A", fields={})
        b = request_store.create_request(request_type="order_problem", actor="Noah", actor_team="shipping", fields={}, priority="urgent")
        request_store.assign(a["id"], "Richard", actor="Robert", actor_team="shipping")
        request_store.comment(a["id"], "pulled, waiting on label", actor="Richard", actor_team="shipping")
        with self.assertRaises(ValueError):
            request_store.comment(a["id"], "   ", actor="Richard")
        self.assertEqual([r["id"] for r in request_store.list_requests(owner_team="shipping")], [a["id"]])
        self.assertEqual([r["id"] for r in request_store.list_requests(owner_team="sales")], [b["id"]])
        self.assertEqual([r["id"] for r in request_store.list_requests(assigned_to="richard")], [a["id"]])
        self.assertEqual([r["id"] for r in request_store.list_requests(cust_order_id="so-a")], [a["id"]])
        self.assertEqual(len(request_store.list_requests(open_only=True)), 2)
        request_store.transition(b["id"], "declined", actor="Holly")
        self.assertEqual(len(request_store.list_requests(open_only=True)), 1)
        self.assertEqual(request_store.list_requests(status="declined")[0]["id"], b["id"])
        self.assertEqual(request_store.get_request(a["id"])["events"][-1]["event_type"], "comment")

    def test_sla_overdue_and_summary(self):
        request_store.configure(sla_overrides={"ship_request": 0})
        fast = request_store.create_request(request_type="ship_request", actor="Holly", cust_order_id="SO-1", fields={})
        self.assertIsNone(fast["sla_due_at"])
        request_store.configure(sla_overrides={})
        req = request_store.create_request(request_type="ship_request", actor="Holly", cust_order_id="SO-2", fields={})
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE requests SET sla_due_at = ? WHERE id = ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), req["id"]))
        conn.commit()
        conn.close()
        self.assertTrue(request_store.get_request(req["id"])["overdue"])
        self.assertEqual([r["id"] for r in request_store.list_requests(overdue_only=True)], [req["id"]])
        request_store.transition(req["id"], "done", actor="Noah")
        summary = request_store.queue_summary()
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["created_in_window"], 2)
        self.assertEqual(summary["by_team"], {"shipping": 1})
        self.assertIsNotNone(summary["median_hours_to_ack"])

    def test_manual_holds_lifecycle(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        with self.assertRaises(ValueError):
            request_store.add_manual_hold(cust_order_id="", hold_kind="vip", reason="x", expires_at=future, created_by="Robert")
        with self.assertRaises(ValueError):
            request_store.add_manual_hold(cust_order_id="SO-1", hold_kind="whatever", reason="x", expires_at=future, created_by="Robert")
        with self.assertRaises(ValueError):
            request_store.add_manual_hold(cust_order_id="SO-1", hold_kind="vip", reason="x", expires_at="2020-01-01T00:00:00+00:00", created_by="Robert")
        hold_id = request_store.add_manual_hold(cust_order_id="so-1", hold_kind="vip", reason="Jeff to review", expires_at=future, created_by="Robert")
        active = request_store.active_manual_holds()
        self.assertEqual(active[0]["cust_order_id"], "SO-1")
        self.assertEqual(active[0]["kind_label"], "VIP or executive review")
        self.assertEqual(request_store.manual_holds_for_order("SO-1")[0]["id"], hold_id)
        self.assertTrue(request_store.release_manual_hold(hold_id, actor="Robert"))
        self.assertFalse(request_store.release_manual_hold(hold_id, actor="Robert"))
        self.assertEqual(request_store.active_manual_holds(), [])
        expiring = request_store.add_manual_hold(cust_order_id="SO-2", hold_kind="marketing", reason="", expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), created_by="Robert")
        expired = request_store.expire_manual_holds(datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual([h["id"] for h in expired], [expiring])
        self.assertEqual(request_store.get_manual_hold(expiring)["released_by"], "expired")


class RequestServiceTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.gettempdir()) / f"picklist-requests-svc-{os.getpid()}-{id(self)}.db"
        request_store.initialize(lambda: sqlite3.connect(self.db_path))
        request_store.configure(sla_overrides={})
        self.exceptions = {}
        self.notifications = []
        self.blocking = []
        self.refreshes = 0
        self.invalidated = 0
        self._next = 100

        def add_exception(order_id, reason, actor, expires_at):
            self._next += 1
            self.exceptions[self._next] = {"order": order_id, "reason": reason, "actor": actor, "expires": expires_at, "active": True}
            return self._next

        def revoke(exception_id, actor):
            row = self.exceptions.get(exception_id)
            if row and row["active"]:
                row["active"] = False
                return True
            return False

        def refresh():
            self.refreshes += 1

        def invalidate():
            self.invalidated += 1

        request_service.configure(
            add_exception=add_exception,
            revoke_exception=revoke,
            invalidate_gate_cache=invalidate,
            refresh_readiness=refresh,
            blocking_holds=lambda so: list(self.blocking),
            notify=lambda event, **card: self.notifications.append((event, card)) or True,
            public_url=lambda path: f"http://ops{path}",
            now=lambda: datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        )

    def tearDown(self):
        request_service.configure(
            add_exception=None, revoke_exception=None, invalidate_gate_cache=None, refresh_readiness=None,
            blocking_holds=None, notify=None, public_url=None, now=None,
        )
        try:
            self.db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_create_notifies_owner_team(self):
        req = request_service.create(request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="SO-1", fields={"needed_by": "2026-10-02"})
        event, card = self.notifications[-1]
        self.assertEqual(event, "request_created")
        self.assertIn("Shipping", card["title"])
        self.assertEqual(card["link"], f"http://ops/requests/{req['id']}")
        self.assertIn(("Needed by", "2026-10-02"), card["facts"])

    def test_expedite_creates_exception_capped_by_needed_by(self):
        req = request_service.create(request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="SO-1", fields={"needed_by": "2026-10-02", "expedite": True})
        with self.assertRaises(ValueError):
            request_service.transition(req["id"], "acknowledged", actor="Holly", actor_team="sales")
        updated = request_service.transition(req["id"], "acknowledged", actor="Robert", actor_team="shipping")
        self.assertEqual(updated["linked_exception_id"], 101)
        self.assertEqual(self.exceptions[101]["order"], "SO-1")
        self.assertTrue(self.exceptions[101]["expires"].startswith("2026-10-02T23:59"))
        self.assertEqual(self.invalidated, 1)
        self.assertIn("exception_added", [e["event_type"] for e in updated["events"]])
        # done -> exception revoked, creator notified
        done = request_service.transition(req["id"], "done", actor="Robert", actor_team="shipping", resolution="Shipped NDA")
        self.assertFalse(self.exceptions[101]["active"])
        self.assertIn("exception_revoked", [e["event_type"] for e in done["events"]])
        self.assertEqual(self.notifications[-1][0], "request_done")

    def test_expedite_refused_while_blocking_hold_open(self):
        self.blocking = [{"reason_code": "credit_status_hold", "label": "Credit status not approved", "owner_team": "finance", "blocking": True}]
        req = request_service.create(request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="SO-1", fields={"expedite": True})
        with self.assertRaises(ValueError) as ctx:
            request_service.transition(req["id"], "in_progress", actor="Robert", actor_team="shipping")
        self.assertIn("Finance", str(ctx.exception))
        self.assertEqual(self.exceptions, {})
        # non-expedite ship requests do not touch the gate
        plain = request_service.create(request_type="ship_request", actor="Holly", actor_team="sales", cust_order_id="SO-2", fields={})
        request_service.transition(plain["id"], "in_progress", actor="Robert", actor_team="shipping")
        self.assertEqual(self.exceptions, {})

    def test_hold_request_becomes_manual_hold_and_releases_on_close(self):
        req = request_service.create(request_type="hold_exception", actor="Harmony", actor_team="sales", cust_order_id="SO-5", fields={"exception_kind": "marketing", "expires_at": "2026-10-08"})
        self.assertEqual(request_store.active_manual_holds(), [])
        approved = request_service.transition(req["id"], "acknowledged", actor="Robert", actor_team="shipping")
        holds = request_store.active_manual_holds(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(holds[0]["cust_order_id"], "SO-5")
        self.assertEqual(holds[0]["request_id"], req["id"])
        self.assertEqual(approved["linked_manual_hold_id"], holds[0]["id"])
        self.assertEqual(self.refreshes, 1)
        request_service.transition(req["id"], "done", actor="Robert", actor_team="shipping")
        self.assertEqual(request_store.active_manual_holds(datetime(2026, 10, 1, tzinfo=timezone.utc)), [])
        self.assertEqual(self.refreshes, 2)

    def test_expire_holds_notifies(self):
        request_store.add_manual_hold(cust_order_id="SO-9", hold_kind="vip", reason="x", expires_at="2026-10-01T08:00:00+00:00", created_by="Robert") if False else None
        # expires_at must be future at insert; insert then expire with a later clock
        hold_id = request_store.add_manual_hold(cust_order_id="SO-9", hold_kind="vip", reason="x", expires_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), created_by="Robert")
        request_service.configure(now=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
        expired = request_service.expire_holds()
        self.assertEqual([h["id"] for h in expired], [hold_id])
        self.assertEqual(self.notifications[-1][0], "hold_resolved")
        self.assertEqual(request_service.expire_holds(), [])

    def test_assign_notifies(self):
        req = request_service.create(request_type="order_problem", actor="Noah", actor_team="shipping", fields={"problem_kind": "rma_on_picklist"})
        request_service.assign(req["id"], "Lunden", actor="Noah", actor_team="shipping")
        self.assertEqual(self.notifications[-1][0], "request_assigned")
        self.assertIn("Lunden", self.notifications[-1][1]["title"])


if __name__ == "__main__":
    unittest.main()
