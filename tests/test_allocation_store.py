"""Unit tests for allocation_store.py and the save path's SQL semantics."""
import sqlite3
import unittest

import allocation_store


def _memory_conn_factory():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    def get_conn():
        return conn

    return conn, get_conn


class AllocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn, get_conn = _memory_conn_factory()
        allocation_store.initialize(get_conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _record(self, **overrides):
        kwargs = dict(
            changed_by="TYLER",
            cust_order_id="SO-118293",
            line_no=171,
            part_id="801-06531-00",
            old_value=None,
            new_value="2026-08-15",
            reason="customer expedite",
            position_before=10,
        )
        kwargs.update(overrides)
        return allocation_store.record_change(**kwargs)

    def test_record_change_roundtrip_and_recent_changes_filters(self):
        audit_id = self._record()
        self._record(cust_order_id="SO-999", part_id="OTHER-PART")

        rows = allocation_store.recent_changes(part_id="801-06531-00")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], audit_id)
        self.assertEqual(row["changed_by"], "TYLER")
        self.assertEqual(row["cust_order_id"], "SO-118293")
        self.assertEqual(row["line_no"], 171)
        self.assertIsNone(row["old_value"])
        self.assertEqual(row["new_value"], "2026-08-15")
        self.assertEqual(row["position_before"], 10)
        self.assertIsNone(row["position_after"])

        by_order = allocation_store.recent_changes(cust_order_id="SO-999")
        self.assertEqual(len(by_order), 1)
        self.assertEqual(allocation_store.recent_changes()[0]["cust_order_id"], "SO-999")

    def test_record_change_requires_changed_by(self):
        with self.assertRaisesRegex(ValueError, "changed_by"):
            self._record(changed_by="   ")

    def test_set_position_after_and_compensating_delete(self):
        audit_id = self._record()
        allocation_store.set_position_after(audit_id, 3)
        self.assertEqual(allocation_store.recent_changes()[0]["position_after"], 3)

        allocation_store.delete_change(audit_id)
        self.assertEqual(allocation_store.recent_changes(), [])

    def test_audit_insert_failure_propagates(self):
        # Design-doc case 17 (store half): a broken store must raise, so the
        # save handler rolls the ERP transaction back.
        self.conn.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self._record()

    def test_uninitialized_store_raises(self):
        allocation_store._get_conn = None
        with self.assertRaisesRegex(RuntimeError, "initialize"):
            allocation_store.recent_changes()


class ConcurrencyPredicateTests(unittest.TestCase):
    """Design-doc case 12: the optimistic-concurrency UPDATE predicate.

    Exercised on a SQLite stand-in of CUST_ORDER_LINE (same WHERE shape as
    ALLOC_UPDATE_SQL in app.py, minus the SQL Server CAST).
    """

    UPDATE = (
        "UPDATE cust_order_line SET promise_del_date = ? "
        "WHERE cust_order_id = ? AND line_no = ? "
        "AND ((promise_del_date = ?) OR (promise_del_date IS NULL AND ? IS NULL))"
    )

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE cust_order_line ("
            "cust_order_id TEXT, line_no INTEGER, promise_del_date TEXT)"
        )

    def _seed(self, value):
        self.conn.execute("DELETE FROM cust_order_line")
        self.conn.execute(
            "INSERT INTO cust_order_line VALUES ('SO-1', 1, ?)", (value,)
        )

    def _update(self, new_value, expected_old):
        cursor = self.conn.execute(
            self.UPDATE, (new_value, "SO-1", 1, expected_old, expected_old)
        )
        return cursor.rowcount

    def test_matching_old_value_updates_one_row(self):
        self._seed("2026-08-01")
        self.assertEqual(self._update("2026-09-01", "2026-08-01"), 1)

    def test_null_expected_matches_null_current(self):
        self._seed(None)
        self.assertEqual(self._update("2026-09-01", None), 1)

    def test_stale_expected_value_updates_zero_rows(self):
        self._seed("2026-08-05")  # someone else changed it
        self.assertEqual(self._update("2026-09-01", "2026-08-01"), 0)

    def test_null_expected_but_value_present_updates_zero_rows(self):
        self._seed("2026-08-05")
        self.assertEqual(self._update("2026-09-01", None), 0)


if __name__ == "__main__":
    unittest.main()
