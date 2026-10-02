import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import readiness_service
import readiness_store


def hold(order="SO-1", code="ffl_expired", line=None, owner="sales", blocking=True, **detail):
    return {
        "order_id": order,
        "line_no": line,
        "reason_code": code,
        "label": code.replace("_", " "),
        "owner_team": owner,
        "blocking": blocking,
        "customer_id": "DEALER1",
        "customer_name": "Dealer One",
        "detail": detail,
    }


class ReadinessStoreTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.gettempdir()) / f"picklist-readiness-{os.getpid()}-{id(self)}.db"
        readiness_store.initialize(lambda: sqlite3.connect(self.db_path))

    def tearDown(self):
        try:
            self.db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_reconcile_new_kept_cleared(self):
        first = readiness_store.reconcile_holds([hold(), hold(code="credit_status_hold", owner="finance")], evaluated_at="2026-10-01T10:00:00+00:00", seen_orders=["SO-1"])
        self.assertEqual(len(first["new"]), 2)
        self.assertEqual(first["kept"], 0)
        ids = first["ids"]
        self.assertIn(("SO-1", "", "ffl_expired"), ids)

        second = readiness_store.reconcile_holds([hold(expiry="1/1/2020")], evaluated_at="2026-10-01T11:00:00+00:00", seen_orders=["SO-1"])
        self.assertEqual(second["new"], [])
        self.assertEqual(second["kept"], 1)
        self.assertEqual(len(second["cleared"]), 1)
        self.assertEqual(second["cleared"][0]["reason_code"], "credit_status_hold")
        self.assertEqual(second["cleared"][0]["cleared_how"], "erp_resolved")
        self.assertEqual(second["ids"][("SO-1", "", "ffl_expired")], ids[("SO-1", "", "ffl_expired")])

        open_rows = readiness_store.open_holds()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["first_seen_at"], "2026-10-01T10:00:00+00:00")
        self.assertEqual(open_rows[0]["last_seen_at"], "2026-10-01T11:00:00+00:00")
        self.assertEqual(open_rows[0]["detail"], {"expiry": "1/1/2020"})

        third = readiness_store.reconcile_holds([], evaluated_at="2026-10-01T12:00:00+00:00", seen_orders=[])
        self.assertEqual(third["cleared"][0]["cleared_how"], "order_closed")
        self.assertEqual(readiness_store.open_holds(), [])
        self.assertEqual(len(readiness_store.hold_history("so-1")), 2)

    def test_reopened_hold_is_a_new_row(self):
        readiness_store.reconcile_holds([hold()], evaluated_at="2026-10-01T10:00:00+00:00")
        readiness_store.reconcile_holds([], evaluated_at="2026-10-01T11:00:00+00:00")
        again = readiness_store.reconcile_holds([hold()], evaluated_at="2026-10-02T10:00:00+00:00")
        self.assertEqual(len(again["new"]), 1)
        self.assertEqual(len(readiness_store.hold_history("SO-1")), 2)

    def test_line_level_holds_are_distinct(self):
        outcome = readiness_store.reconcile_holds(
            [hold(code="no_supply", line="1", owner="production"), hold(code="no_supply", line="2", owner="production")],
            evaluated_at="2026-10-01T10:00:00+00:00",
        )
        self.assertEqual(len(outcome["new"]), 2)

    def test_acknowledge_and_events(self):
        outcome = readiness_store.reconcile_holds([hold()], evaluated_at="2026-10-01T10:00:00+00:00")
        hold_id = outcome["new"][0]["id"]
        updated = readiness_store.acknowledge_hold(hold_id, actor="Holly", actor_team="sales", note="Requested new FFL")
        self.assertEqual(updated["acknowledged_by"], "Holly")
        events = readiness_store.hold_events(hold_id)
        self.assertEqual(events[0]["event_type"], "ack")
        self.assertEqual(events[0]["note"], "Requested new FFL")
        self.assertEqual(readiness_store.events_for_order("SO-1")[0]["reason_code"], "ffl_expired")
        with self.assertRaises(ValueError):
            readiness_store.acknowledge_hold(hold_id, actor="", actor_team=None, note=None)
        with self.assertRaises(LookupError):
            readiness_store.acknowledge_hold(999, actor="Holly", actor_team=None, note=None)
        readiness_store.reconcile_holds([], evaluated_at="2026-10-01T11:00:00+00:00")
        with self.assertRaises(ValueError):
            readiness_store.acknowledge_hold(hold_id, actor="Holly", actor_team=None, note=None)

    def test_snapshots(self):
        payload = {"evaluated_at": "2026-10-01T10:00:00+00:00", "summary": {"orders": 2, "holds": 3}, "orders": [{"order_id": "SO-1"}]}
        snapshot_id = readiness_store.save_snapshot(payload, trigger="manual")
        latest = readiness_store.latest_snapshot()
        self.assertEqual(latest["id"], snapshot_id)
        self.assertEqual(latest["orders"], [{"order_id": "SO-1"}])
        self.assertEqual(latest["summary"]["holds"], 3)
        for index in range(5):
            readiness_store.save_snapshot({**payload, "evaluated_at": f"2026-10-01T1{index}:00:00+00:00"}, trigger="schedule")
        self.assertEqual(readiness_store.prune_snapshots(keep=2), 4)
        self.assertEqual(len(readiness_store.recent_snapshots()), 2)

    def test_hold_durations(self):
        readiness_store.reconcile_holds([hold(), hold(code="no_supply", owner="production")], evaluated_at="2026-09-30T10:00:00+00:00")
        readiness_store.reconcile_holds([hold()], evaluated_at="2026-09-30T14:00:00+00:00")
        stats = readiness_store.hold_durations(days=3650)
        self.assertEqual(stats["open"]["total"]["count"], 1)
        self.assertEqual(stats["cleared"]["total"]["count"], 1)
        self.assertAlmostEqual(stats["cleared"]["by_owner"]["production"]["mean_hours"], 4.0)

    def test_doc_cache_roundtrip(self):
        self.assertIsNone(readiness_store.get_doc_cache("k"))
        readiness_store.put_doc_cache("k", document_id="FFL.pdf", doc_path="/mnt/x", method="pypdf", text="hello", parsed={"premise": "1 Main"}, error=None)
        cached = readiness_store.get_doc_cache("k")
        self.assertEqual(cached["parsed"]["premise"], "1 Main")


class ReadinessServiceTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.gettempdir()) / f"picklist-readiness-svc-{os.getpid()}-{id(self)}.db"
        readiness_store.initialize(lambda: sqlite3.connect(self.db_path))
        self.rows = []
        self.notifications = []
        readiness_service._last_refresh.update({"at": None, "payload": None})
        readiness_service.configure(
            fetch_candidates=lambda: list(self.rows),
            fetch_order_rows=lambda so: [r for r in self.rows if r["CUST_ORDER_ID"] == so],
            fetch_order_locations=lambda so: [{"PART_ID": "801-1", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "R03S03", "QTY": 2}],
            picklist_orders_today=lambda: None,
            picklist_horizon=lambda: None,
            gate_decisions=lambda: {},
            manual_holds=lambda: [],
            doc_findings=None,
            pick_status=lambda so: {"claimed": False},
            today=lambda: __import__("datetime").date(2026, 10, 1),
            config=lambda: {},
            notify=lambda event, **card: self.notifications.append((event, card)) or True,
            public_url=lambda path: f"http://ops{path}",
            cache_seconds=0,
        )

    def tearDown(self):
        readiness_service.configure(
            fetch_candidates=None, fetch_order_rows=None, fetch_order_locations=None,
            picklist_orders_today=None, picklist_horizon=None, gate_decisions=None,
            manual_holds=None, doc_findings=None, pick_status=None, today=None,
            config=None, notify=None, public_url=None, cache_seconds=300,
        )
        try:
            self.db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def _row(self, **overrides):
        from tests.test_readiness import row  # noqa: WPS433 - reuse fixture

        return row(**overrides)

    def test_refresh_persists_notifies_and_overlays(self):
        self.rows = [self._row(ORDER_STATUS="F"), self._row(order="SO-2", SHIP_VIA=None)]
        payload = readiness_service.refresh("manual")
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["reconcile"]["new"], 2)
        self.assertEqual(len(self.notifications), 1)
        event, card = self.notifications[0]
        self.assertEqual(event, "hold_created")
        self.assertEqual(card["link"], "http://ops/orders")
        self.assertEqual(len(card["rows"]), 2)

        current = readiness_service.current_payload()
        self.assertEqual(current["summary"]["orders"], 2)
        first_hold = current["orders"][0]["holds"][0]
        self.assertIsNotNone(first_hold["id"])
        self.assertIn("age_hours", first_hold)

        readiness_service.acknowledge(first_hold["id"], actor="Holly", actor_team="sales", note="on it")
        current = readiness_service.current_payload()
        self.assertEqual(current["orders"][0]["holds"][0]["acknowledged_by"], "Holly")

        self.rows = [self._row()]
        payload = readiness_service.refresh("schedule")
        self.assertEqual(payload["reconcile"]["cleared"], 2)
        self.assertEqual(self.notifications[-1][0], "hold_resolved")

    def test_refresh_handles_erp_failure(self):
        def boom():
            raise RuntimeError("db down")

        readiness_service.configure(fetch_candidates=boom)
        payload = readiness_service.refresh("manual")
        self.assertEqual(payload["error"], "db down")
        self.assertEqual(readiness_service.current_payload()["error"], "db down")

    def test_order_detail(self):
        self.rows = [self._row(ORDER_STATUS="F", PART_ID="801-1")]
        readiness_service.refresh("manual")
        detail = readiness_service.order_detail("so-1")
        self.assertTrue(detail["found"])
        self.assertEqual(detail["state"], "BLOCKED")
        self.assertEqual(detail["lines"][0]["locations"]["pickable_qty"], 2.0)
        self.assertIsNotNone(detail["holds"][0]["id"])
        self.assertEqual(detail["pick"], {"claimed": False})
        self.assertEqual(len(detail["hold_history"]), 1)
        missing = readiness_service.order_detail("SO-NOPE")
        self.assertFalse(missing["found"])

    def test_filter_orders(self):
        self.rows = [self._row(ORDER_STATUS="F"), self._row(order="SO-2", CREDIT_STATUS="H"), self._row(order="SO-3")]
        payload = readiness_service.refresh("manual")
        orders = payload["orders"]
        self.assertEqual([o["order_id"] for o in readiness_service.filter_orders(orders, owner="finance")], ["SO-2"])
        self.assertEqual([o["order_id"] for o in readiness_service.filter_orders(orders, state="READY")], ["SO-3"])
        self.assertEqual(len(readiness_service.filter_orders(orders, query="dealer one")), 3)
        self.assertEqual([o["order_id"] for o in readiness_service.filter_orders(orders, reason="order_not_released")], ["SO-1"])
        self.assertEqual(len(readiness_service.filter_orders(orders, blocking_only=True)), 2)
        self.assertEqual([o["order_id"] for o in readiness_service.filter_orders(orders, owner="sales", blocking_only=True)], ["SO-1"])
        self.assertEqual([o["order_id"] for o in readiness_service.filter_orders(orders, due_before="2026-10-01")], [])
        self.assertEqual(len(readiness_service.filter_orders(orders, due_before="2026-10-31", due_after="2026-09-01")), 3)


if __name__ == "__main__":
    unittest.main()
