import unittest
from datetime import date, datetime

import shipping_metrics


START = date(2026, 8, 1)
END = date(2026, 8, 5)


def order_line(order, line, qty, promise, customer="CUST"):
    return {
        "RECORD_TYPE": "ORDER_LINE",
        "CUST_ORDER_ID": order,
        "CUST_ORDER_LINE_NO": line,
        "CUSTOMER_ID": customer,
        "CUSTOMER_NAME": customer + " NAME",
        "PART_ID": f"PART-{line}",
        "PRODUCT_CODE": "FG-GUN",
        "ORDER_QTY": qty,
        "PROMISE_SHIP_DATE": promise,
    }


def shipment(packlist, shipper_line, order, order_line_no, shipped, qty, guns, status="C"):
    return {
        "RECORD_TYPE": "SHIPMENT_LINE",
        "PACKLIST_ID": packlist,
        "SHIPPER_LINE_NO": shipper_line,
        "CUST_ORDER_ID": order,
        "CUST_ORDER_LINE_NO": order_line_no,
        "CUSTOMER_ID": "CUST",
        "CUSTOMER_NAME": "CUST NAME",
        "PART_ID": f"PART-{order_line_no}",
        "SHIPPED_DATE": shipped,
        "SHIPPER_STATUS": status,
        "SHIPPED_QTY": qty,
        "FIREARM_QTY": guns,
    }


class ShippingMetricTests(unittest.TestCase):
    def test_six_headline_metrics_use_documented_grains(self):
        rows = [
            order_line("SO-1", 1, 2, date(2026, 8, 2)),
            order_line("SO-2", 1, 2, date(2026, 8, 2)),
            shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 2, 10), 2, 2),
            shipment("P2", 1, "SO-2", 1, datetime(2026, 8, 3, 10), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        cards = result["cards"]
        self.assertEqual(cards["ship_on_time"]["value"], 50.0)
        self.assertEqual(cards["ship_complete"]["value"], 50.0)
        self.assertEqual(cards["total_guns_shipped"]["value"], 3)
        self.assertEqual(cards["total_shipments"]["value"], 2)
        self.assertEqual(cards["average_guns_per_shipment"]["value"], 1.5)
        self.assertEqual(cards["single_gun_shipments"]["value"], 1)
        self.assertEqual(cards["single_gun_shipments"]["companion"]["rate_pct"], 50.0)

    def test_packlist_is_counted_once_and_trace_units_are_summed(self):
        rows = [
            order_line("SO-1", 1, 1, date(2026, 8, 2)),
            order_line("SO-1", 2, 1, date(2026, 8, 2)),
            shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 2, 9), 1, 1),
            shipment("P1", 2, "SO-1", 2, datetime(2026, 8, 2, 9), 1, 1),
        ]
        cards = shipping_metrics.build_shipping_metrics(rows, START, END)["cards"]
        self.assertEqual(cards["total_shipments"]["value"], 1)
        self.assertEqual(cards["total_guns_shipped"]["value"], 2)
        self.assertEqual(cards["average_guns_per_shipment"]["value"], 2.0)

    def test_voids_and_duplicate_join_rows_do_not_inflate_metrics(self):
        live = shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 2), 1, 1)
        rows = [
            order_line("SO-1", 1, 1, date(2026, 8, 2)),
            live,
            dict(live),
            shipment("VOID", 1, "SO-1", 1, datetime(2026, 8, 2), 9, 9, status="V"),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        self.assertEqual(result["cards"]["total_guns_shipped"]["value"], 1)
        self.assertEqual(result["cards"]["total_shipments"]["value"], 1)
        self.assertEqual(result["coverage"]["duplicate_shipment_rows_ignored"], 1)

    def test_missing_promise_is_visible_and_excluded_from_rate(self):
        rows = [
            order_line("SO-DATED", 1, 1, date(2026, 8, 2)),
            order_line("SO-BLANK", 1, 1, None),
            shipment("P1", 1, "SO-DATED", 1, datetime(2026, 8, 2), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        self.assertEqual(result["cards"]["ship_on_time"]["denominator"], 1)
        self.assertEqual(result["cards"]["ship_on_time"]["value"], 100.0)
        self.assertEqual(result["coverage"]["missing_promise_orders"], 1)

    def test_order_with_any_missing_line_promise_is_not_silently_measured(self):
        rows = [
            order_line("SO-1", 1, 1, date(2026, 8, 2)),
            order_line("SO-1", 2, 1, None),
            shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 2), 1, 1),
            shipment("P1", 2, "SO-1", 2, datetime(2026, 8, 2), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        self.assertIsNone(result["cards"]["ship_on_time"]["value"])
        self.assertEqual(result["coverage"]["missing_promise_orders"], 1)

    def test_first_shipment_day_can_include_multiple_packlists(self):
        rows = [
            order_line("SO-1", 1, 2, date(2026, 8, 2)),
            shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 2, 9), 1, 1),
            shipment("P2", 1, "SO-1", 1, datetime(2026, 8, 2, 15), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        self.assertEqual(result["cards"]["ship_complete"]["value"], 100.0)

    def test_server_precomputed_history_supports_bounded_shipment_extract(self):
        line = order_line("SO-1", 1, 2, date(2026, 8, 2))
        line.update(
            {
                "ORDER_FIRST_SHIP_DATE": date(2026, 8, 2),
                "ORDER_SHIPPED_BY_PROMISE_QTY": 2,
                "ORDER_FIRST_DAY_SHIPPED_QTY": 2,
            }
        )
        result = shipping_metrics.build_shipping_metrics([line], START, END)
        self.assertEqual(result["cards"]["ship_on_time"]["value"], 100.0)
        self.assertEqual(result["cards"]["ship_complete"]["value"], 100.0)
        self.assertEqual(result["cards"]["total_shipments"]["value"], 0)

    def test_end_boundary_is_exclusive(self):
        rows = [
            order_line("SO-1", 1, 1, END),
            shipment("P1", 1, "SO-1", 1, datetime(2026, 8, 5), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        self.assertEqual(result["cards"]["total_shipments"]["value"], 0)
        self.assertIsNone(result["cards"]["ship_on_time"]["value"])

    def test_zero_denominators_are_none_not_zero_percent(self):
        result = shipping_metrics.build_shipping_metrics([], START, END)
        self.assertIsNone(result["cards"]["ship_on_time"]["value"])
        self.assertIsNone(result["cards"]["ship_complete"]["value"])
        self.assertIsNone(result["cards"]["average_guns_per_shipment"]["value"])

    def test_single_gun_diagnostic_separates_true_single_from_partial(self):
        rows = [
            order_line("SO-ONE", 1, 1, date(2026, 8, 2)),
            order_line("SO-MANY", 1, 3, date(2026, 8, 2)),
            shipment("P1", 1, "SO-ONE", 1, datetime(2026, 8, 2), 1, 1),
            shipment("P2", 1, "SO-MANY", 1, datetime(2026, 8, 2), 1, 1),
        ]
        result = shipping_metrics.build_shipping_metrics(rows, START, END)
        classifications = {
            row["packlist_id"]: row["classification"]
            for row in result["actions"]["single_shipments"]
        }
        self.assertEqual(classifications["P1"], "complete one-unit order")
        self.assertEqual(classifications["P2"], "partial / consolidation review")

    def test_daily_trend_covers_every_calendar_day(self):
        result = shipping_metrics.build_shipping_metrics([], START, END)
        self.assertEqual([row["date"] for row in result["trends"]], [
            "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"
        ])


if __name__ == "__main__":
    unittest.main()
