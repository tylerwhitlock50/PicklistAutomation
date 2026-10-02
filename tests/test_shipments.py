import unittest
from datetime import date, datetime

from picklist.domain import shipments


def row(packlist="PL-288871", order="SO-132000", line=1, **overrides):
    base = {
        "PACKLIST_ID": packlist,
        "LINE_NO": line,
        "PACKLIST_CREATED": datetime(2026, 9, 29, 13, 5),
        "SHIPPED_DATE": datetime(2026, 9, 29, 0, 0),
        "SHIPPER_STATUS": "S",
        "SHIP_VIA": "UPS GROUND",
        "WAYBILL_NUMBER": None,
        "CUST_ORDER_ID": order,
        "CUST_ORDER_LINE_NO": line,
        "PART_ID": "801-06566-00",
        "PRODUCT_CODE": "RIDGELINE FFT",
        "PART_DESCRIPTION": "Ridgeline FFT",
        "SHIPPED_QTY": 1,
        "CUSTOMER_ID": "DEALER1",
        "CUSTOMER_NAME": "Dealer One",
        "INVOICE_ID": "INV-1",
        "TRACKING_NUMBERS": "1Z61E14WA844450768",
        "UDF_TRACKING_NUMBER": None,
        "SERIALS": "14M23235",
    }
    base.update(overrides)
    return base


class ShipmentsTests(unittest.TestCase):
    def test_group_packlists_merges_lines_and_tracking(self):
        rows = [row(line=1), row(line=2, PART_ID="801-06513-00", SERIALS="14M23236, 14M23237", SHIPPED_QTY=2)]
        packlists = shipments.group_packlists(rows)
        self.assertEqual(len(packlists), 1)
        pl = packlists[0]
        self.assertEqual(pl["packlist_id"], "PL-288871")
        self.assertEqual(pl["units"], 3.0)
        self.assertEqual(pl["serials"], ["14M23235", "14M23236", "14M23237"])
        self.assertEqual(pl["tracking"][0]["carrier"], "UPS")
        self.assertIn("tracknum=1Z61E14WA844450768", pl["tracking"][0]["url"])
        self.assertEqual(pl["tracking_source"], "ups")
        self.assertTrue(pl["shipped"])
        self.assertEqual(pl["shipped_date"], "2026-09-29")

    def test_udf_fallback_and_void(self):
        fallback = shipments.group_packlists([row(TRACKING_NUMBERS=None, UDF_TRACKING_NUMBER="42299025")])[0]
        self.assertEqual(fallback["tracking"][0]["number"], "42299025")
        self.assertIsNone(fallback["tracking"][0]["carrier"])
        self.assertEqual(fallback["tracking_source"], "udf")
        voided = shipments.group_packlists([row(SHIPPER_STATUS="V")])[0]
        self.assertTrue(voided["voided"])
        self.assertFalse(voided["shipped"])
        none = shipments.group_packlists([row(TRACKING_NUMBERS=None, UDF_TRACKING_NUMBER="0")])[0]
        self.assertEqual(none["tracking"], [])

    def test_sort_newest_first(self):
        rows = [row(packlist="PL-1", SHIPPED_DATE=datetime(2026, 9, 1)), row(packlist="PL-2", SHIPPED_DATE=datetime(2026, 9, 30))]
        ids = [p["packlist_id"] for p in shipments.group_packlists(rows)]
        self.assertEqual(ids, ["PL-2", "PL-1"])

    def test_summary(self):
        rows = [row(packlist="PL-1"), row(packlist="PL-2", SHIPPER_STATUS="X"), row(packlist="PL-3", TRACKING_NUMBERS=None)]
        summary = shipments.summarize(shipments.group_packlists(rows))
        self.assertEqual(summary["packlists"], 3)
        self.assertEqual(summary["voided"], 1)
        self.assertEqual(summary["shipped_packlists"], 2)
        self.assertEqual(summary["missing_tracking"], ["PL-3"])
        self.assertEqual(summary["last_shipped"], "2026-09-29")

    def test_carrier_detection(self):
        self.assertEqual(shipments.carrier_for("1Z61E14WAB43745273"), "UPS")
        self.assertEqual(shipments.carrier_for("1Z 61E 14W AB 4400 6195"), "UPS")
        self.assertEqual(shipments.carrier_for("794644790132"), "FedEx")
        self.assertEqual(shipments.carrier_for("9400111899223197428490"), "USPS")
        self.assertIsNone(shipments.carrier_for("42453492"))
        self.assertIsNone(shipments.tracking_url(""))

    def test_digest(self):
        rows = [
            row(packlist="PL-1", order="SO-1", CUSTOMER_NAME="Zed Guns"),
            row(packlist="PL-2", order="SO-2", CUSTOMER_NAME="Alpha Arms", TRACKING_NUMBERS=None, UDF_TRACKING_NUMBER=None),
            row(packlist="PL-3", order="SO-3", SHIPPER_STATUS="V"),
        ]
        digest = shipments.build_shipped_digest(rows, day=date(2026, 10, 1))
        self.assertEqual(digest["packlist_count"], 2)
        self.assertEqual(digest["order_count"], 2)
        self.assertEqual(digest["rows"][0][1], "Alpha Arms")
        self.assertEqual(digest["rows"][0][3], "(no tracking yet)")
        self.assertEqual(digest["missing_tracking"], ["PL-2"])
        self.assertEqual(digest["event_key"], "shipped_digest:2026-10-01")
        self.assertIn("2 packlists", digest["title"])
        empty = shipments.build_shipped_digest([], day=date(2026, 10, 1))
        self.assertEqual(empty["text"], "Nothing shipped today.")


if __name__ == "__main__":
    unittest.main()
