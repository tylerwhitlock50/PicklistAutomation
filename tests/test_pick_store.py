import sqlite3
import tempfile
import unittest
from pathlib import Path

import pick_store


class PickStoreOrderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db_path = Path(self.tempdir.name) / "pick.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

        def connection():
            return self.connection

        pick_store.initialize(connection)

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    @staticmethod
    def row(order, part, item_type, location="A01", qty=1, upc=None):
        return {
            "Cust Order ID": order,
            "Customer ID": "CUSTOMER",
            "Part Id": part,
            "Location": location,
            "SO Qty": qty,
            "UPC": upc,
            "_query_type": item_type,
        }

    def test_wave_is_limited_to_three_orders(self):
        rows = [self.row(f"SO-{i}", f"P-{i}", "components") for i in range(4)]
        with self.assertRaisesRegex(ValueError, "at most 3"):
            pick_store.start_order_session(
                plan_rows=rows,
                selected_orders=[f"SO-{i}" for i in range(4)],
                source_runs={"components": 1},
                operator="TEST",
            )

    def test_component_upc_counts_and_incomplete_order_cannot_close(self):
        session_id = pick_store.start_order_session(
            plan_rows=[self.row("SO-1", "PART-1", "components", qty=2, upc="012345678905")],
            selected_orders=["SO-1"],
            source_runs={"components": 1},
            operator="TEST",
        )
        first = pick_store.record_scan(
            session_id,
            "012345678905",
            target_order="SO-1",
            part_candidates=[{"part_id": "PART-1", "locations": []}],
            operator="TEST",
            scanned_tote=f"PICK-{session_id}-A",
            scanned_location="A01",
            request_id="component-1",
        )
        self.assertEqual(first["result"], "ok")
        with self.assertRaisesRegex(ValueError, "1 unit"):
            pick_store.complete_order(session_id, "SO-1")

        pick_store.record_scan(
            session_id,
            "012345678905",
            target_order="SO-1",
            part_candidates=[{"part_id": "PART-1", "locations": []}],
            operator="TEST",
            scanned_tote=f"PICK-{session_id}-A",
            scanned_location="A01",
            request_id="component-2",
        )
        completed = pick_store.complete_order(session_id, "SO-1")
        self.assertTrue(completed["session_complete"])
        self.assertEqual(pick_store.ready_for_pack_orders()[0]["cust_order_id"], "SO-1")
        self.assertTrue(pick_store.attach_packlist("SO-1", "PL-100"))
        self.assertEqual(pick_store.ready_for_pack_orders(), [])

    def test_firearm_serial_is_global_and_location_is_enforced(self):
        first_session = pick_store.start_order_session(
            plan_rows=[self.row("SO-1", "GUN-1", "guns", location="R01")],
            selected_orders=["SO-1"],
            source_runs={"guns": 1},
            operator="TEST",
        )
        wrong_location = pick_store.record_scan(
            first_session,
            "SERIAL-1",
            target_order="SO-1",
            serial="SERIAL-1",
            part_candidates=[{"part_id": "GUN-1", "locations": ["R99"]}],
            operator="TEST",
            scanned_tote=f"PICK-{first_session}-A",
            scanned_location="R01",
            request_id="gun-wrong-location",
        )
        self.assertEqual(wrong_location["result"], "wrong_location")

        accepted = pick_store.record_scan(
            first_session,
            "SERIAL-1",
            target_order="SO-1",
            serial="SERIAL-1",
            part_candidates=[{"part_id": "GUN-1", "locations": ["R01"]}],
            operator="TEST",
            scanned_tote=f"PICK-{first_session}-A",
            scanned_location="R01",
            request_id="gun-accepted",
        )
        self.assertEqual(accepted["result"], "ok")

        second_session = pick_store.start_order_session(
            plan_rows=[self.row("SO-2", "GUN-1", "guns", location="R01")],
            selected_orders=["SO-2"],
            source_runs={"guns": 2},
            operator="OTHER",
        )
        duplicate = pick_store.record_scan(
            second_session,
            "SERIAL-1",
            target_order="SO-2",
            serial="SERIAL-1",
            part_candidates=[{"part_id": "GUN-1", "locations": ["R01"]}],
            operator="OTHER",
            scanned_tote=f"PICK-{second_session}-A",
            scanned_location="R01",
            request_id="gun-duplicate",
        )
        self.assertEqual(duplicate["result"], "duplicate")

    def test_active_order_cannot_be_claimed_twice(self):
        rows = [self.row("SO-1", "PART-1", "components")]
        pick_store.start_order_session(
            plan_rows=rows,
            selected_orders=["SO-1"],
            source_runs={"components": 1},
            operator="TEST",
        )
        with self.assertRaisesRegex(ValueError, "Already claimed"):
            pick_store.start_order_session(
                plan_rows=rows,
                selected_orders=["SO-1"],
                source_runs={"components": 1},
                operator="OTHER",
            )

    def test_operator_cannot_exceed_three_orders_across_sessions(self):
        rows = [self.row(f"SO-{i}", f"P-{i}", "components") for i in range(4)]
        pick_store.start_order_session(
            plan_rows=rows,
            selected_orders=["SO-0", "SO-1"],
            source_runs={"components": 1},
            operator="PICKER",
        )
        with self.assertRaisesRegex(ValueError, "claim 1 more"):
            pick_store.start_order_session(
                plan_rows=rows,
                selected_orders=["SO-2", "SO-3"],
                source_runs={"components": 1},
                operator="picker",
            )

    def test_component_scan_request_is_idempotent(self):
        session_id = pick_store.start_order_session(
            plan_rows=[self.row("SO-1", "PART-1", "components", qty=2, upc="111")],
            selected_orders=["SO-1"],
            source_runs={"components": 1},
            operator="TEST",
        )
        kwargs = {
            "target_order": "SO-1",
            "part_candidates": [{"part_id": "PART-1", "locations": []}],
            "operator": "TEST",
            "scanned_tote": f"PICK-{session_id}-A",
            "scanned_location": "A01",
            "request_id": "same-physical-scan",
        }
        first = pick_store.record_scan(session_id, "111", **kwargs)
        replay = pick_store.record_scan(session_id, "111", **kwargs)
        self.assertEqual(first["counts"]["picked_units"], 1)
        self.assertEqual(replay["counts"]["picked_units"], 1)
        self.assertTrue(replay["idempotent_replay"])

    def test_tote_and_location_are_enforced(self):
        session_id = pick_store.start_order_session(
            plan_rows=[self.row("SO-1", "PART-1", "components", location="A01")],
            selected_orders=["SO-1"],
            source_runs={"components": 1},
            operator="TEST",
        )
        common = {
            "target_order": "SO-1",
            "part_candidates": [{"part_id": "PART-1", "locations": []}],
            "operator": "TEST",
        }
        wrong_tote = pick_store.record_scan(
            session_id, "PART-1", request_id="bad-tote",
            scanned_tote="PICK-999-Z", scanned_location="A01", **common
        )
        wrong_location = pick_store.record_scan(
            session_id, "PART-1", request_id="bad-location",
            scanned_tote=f"PICK-{session_id}-A", scanned_location="B99", **common
        )
        self.assertEqual(wrong_tote["result"], "wrong_tote")
        self.assertEqual(wrong_location["result"], "wrong_location")
        self.assertEqual(pick_store.compute_counts(session_id)["picked_units"], 0)

    def test_release_transfer_and_exception_are_audited(self):
        rows = [
            self.row("SO-1", "P-1", "components"),
            self.row("SO-2", "P-2", "components", qty=2),
        ]
        session_id = pick_store.start_order_session(
            plan_rows=rows,
            selected_orders=["SO-1", "SO-2"],
            source_runs={"components": 1},
            operator="TEST",
        )
        released = pick_store.order_action(
            session_id, "SO-1", "release", operator="TEST", reason="Wrong priority"
        )
        transferred = pick_store.order_action(
            session_id, "SO-2", "transfer", operator="TEST", to_operator="OTHER"
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(transferred["assigned_operator"], "OTHER")

        pick_store.record_scan(
            session_id, "P-2", target_order="SO-2",
            part_candidates=[{"part_id": "P-2", "locations": []}],
            operator="OTHER", scanned_tote=f"PICK-{session_id}-B",
            scanned_location="A01", request_id="partial-before-exception",
        )
        with self.assertRaisesRegex(ValueError, "cannot be released"):
            pick_store.order_action(
                session_id, "SO-2", "release", operator="OTHER", reason="Cannot finish"
            )
        exception = pick_store.order_action(
            session_id, "SO-2", "exception", operator="OTHER", reason="Damaged carton"
        )
        self.assertEqual(exception["status"], "exception")
        resumed = pick_store.order_action(
            session_id, "SO-2", "resume", operator="SUPERVISOR"
        )
        self.assertEqual(resumed["status"], "picking")
        event_types = {event["event_type"] for event in pick_store.get_order_events(session_id)}
        self.assertTrue({"claimed", "release", "transfer", "exception", "resume"}.issubset(event_types))

    def test_abandon_session_frees_orders_and_serials(self):
        rows = [
            self.row("SO-1", "G-1", "guns"),
            self.row("SO-2", "P-2", "components", qty=2),
        ]
        session_id = pick_store.start_order_session(
            plan_rows=rows, selected_orders=["SO-1", "SO-2"], source_runs={"guns": 1, "components": 1}, operator="TEST",
        )
        pick_store.record_scan(
            session_id, "SER-1", target_order="SO-1", serial="SER-1",
            part_candidates=[{"part_id": "G-1", "locations": ["A01"]}],
            operator="TEST", scanned_tote=f"PICK-{session_id}-A", scanned_location="A01",
            request_id="serial-1",
        )
        pick_store.order_action(session_id, "SO-2", "exception", operator="TEST", reason="Damaged")
        self.assertEqual(pick_store.claimed_orders(), {"SO-1", "SO-2"})

        with self.assertRaisesRegex(ValueError, "reason"):
            pick_store.abandon_session(session_id, operator="LEAD", reason="")
        result = pick_store.abandon_session(session_id, operator="LEAD", reason="Operator went home")
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual({o["cust_order_id"] for o in result["orders"]}, {"SO-1", "SO-2"})
        self.assertEqual(result["picked_units"], 1)
        self.assertEqual(pick_store.claimed_orders(), set())
        session = pick_store.get_session(session_id)
        self.assertEqual((session["status"], session["closed_by"], session["closed_reason"]), ("abandoned", "LEAD", "Operator went home"))
        events = [e for e in pick_store.get_order_events(session_id) if e["event_type"] == "abandoned"]
        self.assertEqual(len(events), 2)
        # Scan history survives for the audit trail ...
        self.assertTrue(any(s["serial"] == "SER-1" for s in pick_store.get_scans(session_id)))
        # ... but the serial can be picked again in a fresh session.
        second = pick_store.start_order_session(
            plan_rows=rows, selected_orders=["SO-1"], source_runs={"guns": 1}, operator="OTHER",
        )
        scan = pick_store.record_scan(
            second, "SER-1", target_order="SO-1", serial="SER-1",
            part_candidates=[{"part_id": "G-1", "locations": ["A01"]}],
            operator="OTHER", scanned_tote=f"PICK-{second}-A", scanned_location="A01",
            request_id="serial-1-again",
        )
        self.assertEqual(scan["result"], "ok", scan)
        with self.assertRaisesRegex(ValueError, "already abandoned"):
            pick_store.abandon_session(session_id, operator="LEAD", reason="again")


if __name__ == "__main__":
    unittest.main()
