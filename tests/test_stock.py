import unittest

import stock


def loc(warehouse, location, qty, serials="", serial_count=None, part="801-06486-00"):
    return {
        "PART_ID": part,
        "PART_DESCRIPTION": "Ridgeline FFT 6.5 CM",
        "PRODUCT_CODE": "RIDGELINE FFT",
        "WAREHOUSE_ID": warehouse,
        "LOCATION_ID": location,
        "QTY": qty,
        "SERIALS": serials,
        "SERIAL_COUNT": serial_count if serial_count is not None else len([s for s in serials.split(",") if s.strip()]),
        "OLDEST_TRANSACTION_AT": "2026-09-01T10:00:00",
    }


class StockTests(unittest.TestCase):
    def test_bins_classified_and_sorted(self):
        rows = [
            loc("MAIN", "C2-SERIALIZED", 5, "A1, A2, A3, A4, A5"),
            loc("SHIPPING", "R03S03", 2, "14M1, 14M2"),
            loc("SHIPPING", "STAGE-1", 1, "14M3"),
            loc("SHIPPING", "R10", 1, "14M4"),
            loc("SHIPPING", "INTERNATIONAL", 1, "14M5"),
        ]
        payload = stock.build_stock_payload("801-06486-00", location_rows=rows)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["description"], "Ridgeline FFT 6.5 CM")
        self.assertEqual([b["class"] for b in payload["bins"]], ["pickable", "stage", "rack10", "international", "main"])
        self.assertEqual(payload["pickable_qty"], 2.0)
        self.assertEqual(payload["total_qty"], 10.0)
        self.assertEqual(payload["bins"][0]["serials"], ["14M1", "14M2"])
        self.assertEqual(payload["free_qty"], 2.0)
        self.assertEqual(payload["classes"][0]["label"], "Pickable racks (R01-R09)")

    def test_allocation_reservation_and_holds(self):
        rows = [loc("SHIPPING", "R03S03", 3, "14M1, 14M2, 14M3")]
        allocation = {
            "error": None,
            "demand": {
                "lines": [
                    {"so": "SO-1", "line_no": 1, "customer_id": "DEALER1", "customer_name": "Dealer One", "position": 1,
                     "dates": {"eff_promise_del": "2026-10-03"}, "supply_status": "ALLOCATED",
                     "allocations": [{"class": "ON_HAND", "qty": 2}]},
                    {"so": "SO-2", "line_no": 1, "customer_id": "DEALER2", "position": 2,
                     "allocations": [{"class": "WO_RELEASED", "qty": 1}]},
                ]
            },
        }
        reservations = [
            {"serial_no": "14M1", "part_id": "801-06486-00", "cust_order_id": "SO-9", "customer_id": "LIPSEYS", "status": "active", "first_assigned_at": "2026-09-30T12:00:00"},
            {"serial_no": "ZZZ", "part_id": "OTHER", "cust_order_id": "SO-8", "status": "active"},
            {"serial_no": "14M2", "part_id": "801-06486-00", "cust_order_id": "SO-7", "status": "released"},
        ]
        payload = stock.build_stock_payload(
            "801-06486-00", location_rows=rows, allocation=allocation, reservations=reservations,
            manual_holds=[{"cust_order_id": "so-1", "hold_kind": "vip"}],
        )
        self.assertEqual(payload["allocated_qty"], 2.0)
        self.assertEqual(payload["allocated_to"][0]["order_id"], "SO-1")
        self.assertEqual(len(payload["allocated_to"]), 1)
        self.assertEqual(payload["free_qty"], 1.0)
        self.assertEqual([r["serial_no"] for r in payload["reserved_serials"]], ["14M1"])
        self.assertEqual(payload["held_for"][0]["order_id"], "SO-1")

    def test_serial_truncation_and_not_found(self):
        serials = ", ".join(f"S{i}" for i in range(80))
        payload = stock.build_stock_payload("P", location_rows=[loc("SHIPPING", "R01S01", 80, serials, serial_count=80, part="P")])
        self.assertEqual(len(payload["bins"][0]["serials"]), stock.SERIALS_PER_BIN)
        self.assertTrue(payload["bins"][0]["serials_truncated"])
        empty = stock.build_stock_payload("NOPE", location_rows=[])
        self.assertFalse(empty["found"])
        self.assertEqual(empty["free_qty"], 0.0)

    def test_serial_locations_payload(self):
        found = stock.serial_locations_payload(
            "14m23235",
            {"14M23235": [{"PART_ID": "801-06159-00", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "INTERNATIONAL", "QTY": 1}]},
        )
        self.assertTrue(found["found"])
        self.assertEqual(found["locations"][0]["class"], "international")
        self.assertFalse(stock.serial_locations_payload("X", {})["found"])


if __name__ == "__main__":
    unittest.main()
