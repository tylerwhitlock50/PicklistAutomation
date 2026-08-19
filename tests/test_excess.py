"""Unit tests for excess.py — pure fixtures, no DB or Flask."""
import unittest
from datetime import date, datetime, timedelta

from excess import build_excess

TODAY = date(2026, 8, 19)  # a Wednesday mid-month
COST = 51.0

_NAT = float("nan")  # pandas hands NULL dates over as NaT/NaN, not None


def shipper(packlist_id, order, shipped=None, created=None, customer="CUST1",
            lines=1, serials=1, ship_via="UPS GROUND"):
    return {
        "PACKLIST_ID": packlist_id,
        "CUST_ORDER_ID": order,
        "CREATE_DATE": datetime.combine(created or shipped or TODAY, datetime.min.time()),
        "SHIPPED_DATE": (
            datetime.combine(shipped, datetime.min.time()) if shipped else _NAT
        ),
        "SHIPPER_STATUS": "C",
        "SHIP_VIA": ship_via,
        "CUSTOMER_ID": customer,
        "CUSTOMER_NAME": customer + " NAME",
        "LINE_COUNT": lines,
        "SERIAL_COUNT": serials,
    }


class BuildExcessTests(unittest.TestCase):
    def test_single_packlist_never_excess(self):
        payload = build_excess([shipper("P1", "SO1", shipped=TODAY)], TODAY, COST)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["summary"]["today"]["excess_count"], 0)
        self.assertEqual(payload["summary"]["fixable_count"], 0)

    def test_three_same_day_is_two_excess(self):
        rows = [shipper(f"P{i}", "SO1", shipped=TODAY) for i in range(3)]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertEqual(group["excess_count"], 2)
        self.assertEqual(group["excess_cost"], 102.0)
        self.assertEqual(payload["summary"]["today"]["excess_count"], 2)
        self.assertEqual(payload["summary"]["today"]["excess_cost"], 102.0)
        self.assertEqual(payload["summary"]["week"]["excess_count"], 2)
        self.assertEqual(payload["summary"]["month"]["excess_count"], 2)

    def test_cross_day_shipments_not_excess(self):
        rows = [
            shipper("P1", "SO1", shipped=TODAY - timedelta(days=1)),
            shipper("P2", "SO1", shipped=TODAY),
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["groups"], [])

    def test_null_order_ignored_but_counted(self):
        rows = [
            shipper("P1", None, shipped=TODAY),
            shipper("P2", None, shipped=TODAY),
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["summary"]["ignored_no_order"], 2)

    def test_fixable_unshipped_duplicate_today(self):
        rows = [
            shipper("P1", "SO1", shipped=TODAY),
            shipper("P2", "SO1", created=TODAY),  # not shipped yet
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["summary"]["fixable_count"], 1)
        self.assertEqual(payload["summary"]["fixable_savings"], 51.0)
        self.assertEqual(len(payload["fixable"]), 1)
        self.assertEqual(payload["fixable"][0]["fixable_count"], 1)
        # Not yet a shipped excess — only one box actually shipped today.
        self.assertEqual(payload["summary"]["today"]["excess_count"], 0)

    def test_two_unshipped_duplicates_alone_are_fixable(self):
        rows = [
            shipper("P1", "SO1", created=TODAY),
            shipper("P2", "SO1", created=TODAY),
        ]
        payload = build_excess(rows, TODAY, COST)
        # min(unshipped=2, n-1=1) — merging both into one box saves one charge
        self.assertEqual(payload["summary"]["fixable_count"], 1)

    def test_already_shipped_duplicates_not_fixable(self):
        rows = [
            shipper("P1", "SO1", shipped=TODAY),
            shipper("P2", "SO1", shipped=TODAY),
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["summary"]["fixable_count"], 0)
        self.assertEqual(payload["summary"]["today"]["excess_count"], 1)

    def test_week_boundary_inclusive(self):
        edge = TODAY - timedelta(days=6)
        outside = TODAY - timedelta(days=7)
        rows = [
            shipper("P1", "SO1", shipped=edge),
            shipper("P2", "SO1", shipped=edge),
            shipper("P3", "SO2", shipped=outside),
            shipper("P4", "SO2", shipped=outside),
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["summary"]["week"]["excess_count"], 1)
        self.assertEqual(payload["summary"]["month"]["excess_count"], 2)

    def test_month_bucket_excludes_prior_month(self):
        prior = TODAY.replace(day=1) - timedelta(days=1)
        rows = [
            shipper("P1", "SO1", shipped=prior),
            shipper("P2", "SO1", shipped=prior),
        ]
        payload = build_excess(rows, TODAY, COST)
        self.assertEqual(payload["summary"]["month"]["excess_count"], 0)
        # Still listed for the consolidation table (it's inside the window).
        self.assertEqual(len(payload["groups"]), 1)

    def test_payload_is_json_safe_iso_dates(self):
        rows = [shipper(f"P{i}", "SO1", shipped=TODAY) for i in range(2)]
        payload = build_excess(rows, TODAY, COST)
        group = payload["groups"][0]
        self.assertEqual(group["ship_date"], TODAY.isoformat())
        self.assertEqual(group["packlists"][0]["shipped"], TODAY.isoformat())
        self.assertEqual(payload["window"]["today"], TODAY.isoformat())


if __name__ == "__main__":
    unittest.main()
