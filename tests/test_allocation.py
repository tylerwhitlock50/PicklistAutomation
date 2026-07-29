"""Unit tests for allocation.py — pure fixtures, no DB or Flask.

Test cases follow docs/allocation-visibility-design.md §10.
"""
import copy
import unittest
from datetime import date, timedelta

import allocation
from allocation import build_allocation, preview_change, suggest_promise_del

TODAY = date(2026, 7, 29)
LOOKAHEAD = 10


def supply(cls, qty, supply_date=None, supply_id=None, detail=None):
    return {
        "SUPPLY_CLASS": cls,
        "SUPPLY_ID": supply_id or f"{cls}-{qty}-{supply_date}",
        "QTY": qty,
        "SUPPLY_DATE": supply_date,
        "ELIGIBLE": 0 if cls == "ON_HAND_OTHER" else 1,
        "DETAIL": detail,
    }


def demand(so, line_no=1, order_qty=1, shipped_qty=0, customer="CUST",
           order_date=date(2026, 1, 15), hdr_desired=None, line_desired=None,
           hdr_promise_ship=None, line_promise_ship=None,
           hdr_promise_del=None, line_promise_del=None,
           order_status="R", line_status="A", credit_status="A",
           salesrep_id=None, customer_po_ref=None, discount_code=None):
    return {
        "CUST_ORDER_ID": so,
        "LINE_NO": line_no,
        "CUSTOMER_ID": customer,
        "CUSTOMER_NAME": customer + " NAME",
        "ORDER_DATE": order_date,
        "ORDER_QTY": order_qty,
        "SHIPPED_QTY": shipped_qty,
        "OPEN_QTY": order_qty - shipped_qty,
        "HDR_DESIRED_SHIP_DATE": hdr_desired,
        "LINE_DESIRED_SHIP_DATE": line_desired,
        "HDR_PROMISE_SHIP_DATE": hdr_promise_ship,
        "LINE_PROMISE_SHIP_DATE": line_promise_ship,
        "HDR_PROMISE_DEL_DATE": hdr_promise_del,
        "LINE_PROMISE_DEL_DATE": line_promise_del,
        "ORDER_STATUS": order_status,
        "LINE_STATUS": line_status,
        "CREDIT_STATUS": credit_status,
        "SALESREP_ID": salesrep_id,
        "CUSTOMER_PO_REF": customer_po_ref,
        "DISCOUNT_CODE": discount_code,
    }


def build(supply_rows, demand_rows, **kwargs):
    return build_allocation(supply_rows, demand_rows, TODAY, LOOKAHEAD, **kwargs)


def line_of(result, so, line_no=1):
    for line in result["demand"]["lines"]:
        if line["so"] == so and line["line_no"] == line_no:
            return line
    raise AssertionError(f"line {so}/{line_no} not in result")


def positions(result):
    return {(l["so"], l["line_no"]): l["position"] for l in result["demand"]["lines"]}


class SingleUnitTests(unittest.TestCase):
    def test_single_order_single_onhand_unit_position_1_available_today(self):
        # Case 1
        result = build([supply("ON_HAND", 1, supply_id="SHIPPING/R01-A")],
                       [demand("SO-1", hdr_promise_del=date(2026, 8, 1))])
        line = line_of(result, "SO-1")
        self.assertEqual(line["position"], 1)
        self.assertEqual(line["supply_status"], "ALLOCATED")
        self.assertEqual(line["est_available"], TODAY.isoformat())
        self.assertEqual(line["est_certainty"], "AVAILABLE")
        self.assertEqual(result["demand"]["unallocated_units"], 0)


class OrderingTests(unittest.TestCase):
    def test_distinct_promise_del_dates_strict_order(self):
        # Case 2
        rows = [
            demand("SO-LATE", hdr_promise_del=date(2026, 9, 1)),
            demand("SO-EARLY", hdr_promise_del=date(2026, 8, 1)),
            demand("SO-MID", hdr_promise_del=date(2026, 8, 15)),
        ]
        result = build([], rows)
        pos = positions(result)
        self.assertEqual(pos[("SO-EARLY", 1)], 1)
        self.assertEqual(pos[("SO-MID", 1)], 2)
        self.assertEqual(pos[("SO-LATE", 1)], 3)

    def test_tied_promise_del_falls_to_ship_then_order_date_so_line_and_stable(self):
        # Case 3 — same Promise Del everywhere; tie-break chain decides.
        d = date(2026, 8, 1)
        rows = [
            demand("SO-B", hdr_promise_del=d, hdr_promise_ship=date(2026, 8, 5),
                   order_date=date(2026, 1, 2)),
            demand("SO-A", hdr_promise_del=d, hdr_promise_ship=date(2026, 8, 5),
                   order_date=date(2026, 1, 2)),
            demand("SO-C", hdr_promise_del=d, hdr_promise_ship=date(2026, 8, 1)),
            demand("SO-D", hdr_promise_del=d, hdr_promise_ship=date(2026, 8, 5),
                   order_date=date(2026, 1, 1)),
        ]
        result1 = build([], copy.deepcopy(rows))
        result2 = build([], copy.deepcopy(list(reversed(rows))))
        pos1, pos2 = positions(result1), positions(result2)
        self.assertEqual(pos1, pos2)  # stable regardless of input order
        self.assertEqual(pos1[("SO-C", 1)], 1)  # earlier promise ship wins tie
        self.assertEqual(pos1[("SO-D", 1)], 2)  # then earlier order date
        self.assertEqual(pos1[("SO-A", 1)], 3)  # then SO id
        self.assertEqual(pos1[("SO-B", 1)], 4)

    def test_blank_promise_del_sorts_after_all_dated_lines(self):
        # Case 4
        rows = [
            demand("SO-BLANK"),
            demand("SO-DATED", hdr_promise_del=date(2027, 12, 31)),
        ]
        pos = positions(build([], rows))
        self.assertEqual(pos[("SO-DATED", 1)], 1)
        self.assertEqual(pos[("SO-BLANK", 1)], 2)

    def test_promise_del_wins_over_earlier_promise_ship(self):
        # Case 14
        rows = [
            demand("SO-EARLYSHIP", hdr_promise_ship=date(2026, 1, 1),
                   hdr_promise_del=date(2026, 9, 1)),
            demand("SO-LATESHIP", hdr_promise_ship=date(2026, 12, 1),
                   hdr_promise_del=date(2026, 8, 1)),
        ]
        pos = positions(build([], rows))
        self.assertEqual(pos[("SO-LATESHIP", 1)], 1)
        self.assertEqual(pos[("SO-EARLYSHIP", 1)], 2)

    def test_line_promise_del_overrides_header(self):
        rows = [
            demand("SO-HDRONLY", hdr_promise_del=date(2026, 8, 1)),
            demand("SO-LINE", hdr_promise_del=date(2026, 9, 1),
                   line_promise_del=date(2026, 7, 30)),
        ]
        result = build([], rows)
        self.assertEqual(positions(result)[("SO-LINE", 1)], 1)
        line = line_of(result, "SO-LINE")
        self.assertEqual(line["dates"]["promise_del_source"], "line")
        self.assertEqual(line["dates"]["eff_promise_del"], "2026-07-30")


class EligibilityTests(unittest.TestCase):
    def test_credit_hold_badged_unallocated_supply_flows_to_next_line(self):
        # Case 5
        rows = [
            demand("SO-HELD", hdr_promise_del=date(2026, 8, 1), credit_status="H"),
            demand("SO-OK", hdr_promise_del=date(2026, 8, 15)),
        ]
        result = build([supply("ON_HAND", 1, supply_id="SHIPPING/R01-A")], rows)
        held = line_of(result, "SO-HELD")
        ok = line_of(result, "SO-OK")
        self.assertIsNone(held["position"])
        self.assertIn("credit_hold", held["reasons"])
        self.assertEqual(held["allocations"], [])
        self.assertEqual(ok["position"], 1)
        self.assertEqual(ok["supply_status"], "ALLOCATED")

    def test_order_not_released_rma_and_excluded_badges(self):
        rows = [
            demand("SO-HOLD", order_status="H"),
            demand("SO-UNREL", order_status="F"),
            demand("SO-RMA1", salesrep_id="RMA"),
            demand("SO-RMA2", customer_po_ref="Re: RMA 123"),
            demand("SO-INTL", discount_code="International Dealers"),
            demand("SO-EMP", discount_code="Employee Purchase"),
            demand("SO-EXCL", customer="BIGBOX STORES"),
        ]
        result = build([], rows, excluded_customer_terms=("BIGBOX",))
        self.assertIn("order_hold", line_of(result, "SO-HOLD")["reasons"])
        self.assertIn("order_not_released", line_of(result, "SO-UNREL")["reasons"])
        self.assertIn("rma", line_of(result, "SO-RMA1")["reasons"])
        self.assertIn("rma", line_of(result, "SO-RMA2")["reasons"])
        self.assertIn("excluded_class", line_of(result, "SO-INTL")["reasons"])
        self.assertIn("excluded_class", line_of(result, "SO-EMP")["reasons"])
        self.assertIn("excluded_customer", line_of(result, "SO-EXCL")["reasons"])
        for so in ("SO-HOLD", "SO-UNREL", "SO-RMA1", "SO-RMA2", "SO-INTL", "SO-EMP", "SO-EXCL"):
            self.assertIsNone(line_of(result, so)["position"])

    def test_partially_shipped_line_allocates_open_qty_only(self):
        # Case 6 — 5 ordered / 3 shipped -> open 2.
        result = build([supply("ON_HAND", 10, supply_id="SHIPPING/R01-A")],
                       [demand("SO-PART", order_qty=5, shipped_qty=3)])
        line = line_of(result, "SO-PART")
        self.assertEqual(line["open_qty"], 2)
        self.assertEqual(line["covered_qty"], 2)
        self.assertEqual(sum(a["qty"] for a in line["allocations"]), 2)


class SupplyTests(unittest.TestCase):
    def test_multiunit_line_spans_events_last_date_weakest_certainty(self):
        # Case 7 — 3 units across on-hand + released WO: est date = last event,
        # certainty = weakest contributor.
        rows = [supply("ON_HAND", 1, supply_id="SHIPPING/R01-A"),
                supply("WO_RELEASED", 2, date(2026, 8, 5), "WO 1/1/0")]
        result = build(rows, [demand("SO-1", order_qty=3)])
        line = line_of(result, "SO-1")
        self.assertEqual(line["supply_status"], "ALLOCATED")
        self.assertEqual(len(line["allocations"]), 2)
        self.assertEqual(line["est_available"], "2026-08-05")
        self.assertEqual(line["est_certainty"], "RELEASED")

    def test_released_wo_remaining_is_desired_minus_received(self):
        # Case 8 — the SQL sends remaining qty; engine must not re-derive.
        result = build([supply("WO_RELEASED", 4, date(2026, 8, 5), "WO 2/1/0",
                               detail="received 6 of 10")],
                       [demand("SO-1", order_qty=10)])
        line = line_of(result, "SO-1")
        self.assertEqual(line["covered_qty"], 4)
        self.assertEqual(line["supply_status"], "PARTIAL")

    def test_firmed_ranks_behind_released_on_same_date(self):
        # Case 9
        rows = [supply("WO_FIRMED", 1, date(2026, 8, 5), "WO F/1/0"),
                supply("WO_RELEASED", 1, date(2026, 8, 5), "WO R/1/0")]
        result = build(rows, [demand("SO-1", order_qty=1)])
        line = line_of(result, "SO-1")
        self.assertEqual(line["allocations"][0]["class"], "WO_RELEASED")

    def test_mps_only_beyond_netting_fence_labeled_planned(self):
        # Case 10 — WO want 8/5 fences off MPS buckets on/before 8/5.
        rows = [
            supply("WO_RELEASED", 2, date(2026, 8, 5), "WO 1/1/0"),
            supply("MPS", 3, date(2026, 8, 3), "MPS 2026-08-03"),
            supply("MPS", 3, date(2026, 8, 5), "MPS 2026-08-05"),
            supply("MPS", 5, date(2026, 8, 20), "MPS 2026-08-20"),
        ]
        result = build(rows, [demand("SO-1", order_qty=7)])
        classes = [e["class"] for e in result["supply"]["events"]]
        self.assertEqual(classes, ["WO_RELEASED", "MPS"])
        self.assertEqual(result["supply"]["netting_fence"], "2026-08-05")
        self.assertEqual(result["supply"]["excluded_mps_qty"], 6)
        line = line_of(result, "SO-1")
        self.assertEqual(line["est_available"], "2026-08-20")
        self.assertEqual(line["est_certainty"], "PLANNED")

    def test_mps_all_included_when_no_open_wos(self):
        rows = [supply("MPS", 3, date(2026, 8, 3), "MPS 2026-08-03")]
        result = build(rows, [demand("SO-1")])
        self.assertEqual(len(result["supply"]["events"]), 1)
        self.assertIsNone(result["supply"]["netting_fence"])

    def test_wo_date_slip_moves_est_dates_not_positions(self):
        # Case 11
        rows = [demand("SO-A", hdr_promise_del=date(2026, 8, 1)),
                demand("SO-B", hdr_promise_del=date(2026, 8, 15))]
        before = build([supply("WO_RELEASED", 2, date(2026, 8, 5), "WO 1/1/0")],
                       copy.deepcopy(rows))
        after = build([supply("WO_RELEASED", 2, date(2026, 9, 5), "WO 1/1/0")],
                      copy.deepcopy(rows))
        self.assertEqual(positions(before), positions(after))
        self.assertEqual(line_of(before, "SO-A")["est_available"], "2026-08-05")
        self.assertEqual(line_of(after, "SO-A")["est_available"], "2026-09-05")

    def test_insufficient_supply_tail_flagged_no_supply_no_fabricated_date(self):
        # Case 15
        rows = [demand("SO-A", hdr_promise_del=date(2026, 8, 1)),
                demand("SO-B", hdr_promise_del=date(2026, 8, 2)),
                demand("SO-C", hdr_promise_del=date(2026, 8, 3))]
        result = build([supply("ON_HAND", 1, supply_id="SHIPPING/R01-A")], rows)
        self.assertEqual(line_of(result, "SO-A")["supply_status"], "ALLOCATED")
        for so in ("SO-B", "SO-C"):
            line = line_of(result, so)
            self.assertEqual(line["supply_status"], "NO_SUPPLY")
            self.assertIsNone(line["est_available"])
        self.assertEqual(result["demand"]["unallocated_units"], 2)

    def test_partial_coverage_flagged_partial(self):
        result = build([supply("ON_HAND", 1, supply_id="SHIPPING/R01-A")],
                       [demand("SO-A", order_qty=3)])
        line = line_of(result, "SO-A")
        self.assertEqual(line["supply_status"], "PARTIAL")
        self.assertEqual(line["covered_qty"], 1)
        self.assertIsNone(line["est_available"])

    def test_informational_stock_never_allocated(self):
        # Case 16 (DSL-linked WOs are filtered in SQL; the engine must not
        # allocate ON_HAND_OTHER either).
        rows = [supply("ON_HAND_OTHER", 50, supply_id="MAIN/P-ASSY")]
        result = build(rows, [demand("SO-A")])
        self.assertEqual(result["supply"]["events"], [])
        self.assertEqual(result["supply"]["informational"][0]["qty"], 50)
        self.assertEqual(line_of(result, "SO-A")["supply_status"], "NO_SUPPLY")

    def test_no_unit_allocated_twice(self):
        rows = [demand(f"SO-{i}", hdr_promise_del=date(2026, 8, i + 1), order_qty=3)
                for i in range(5)]
        result = build([supply("ON_HAND", 4, supply_id="SHIPPING/R01-A"),
                        supply("WO_RELEASED", 6, date(2026, 8, 10), "WO 1/1/0")], rows)
        total_allocated = sum(l["covered_qty"] for l in result["demand"]["lines"])
        self.assertEqual(total_allocated, 10)  # never more than supply
        for event in result["supply"]["events"]:
            self.assertLessEqual(event["allocated_qty"], event["qty"])


class WindowMarkerTests(unittest.TestCase):
    def test_in_picklist_window_marker_uses_coalesce_chain(self):
        rows = [
            demand("SO-DEL-OUT", hdr_promise_del=TODAY + timedelta(days=30),
                   hdr_desired=TODAY),                       # del beyond window
            demand("SO-DEL-IN", hdr_promise_del=TODAY + timedelta(days=3),
                   hdr_desired=TODAY + timedelta(days=60)),  # del pulls it in
            demand("SO-SHIP-IN", hdr_promise_ship=TODAY),    # no del: ship gates
            demand("SO-DESIRED", hdr_desired=TODAY + timedelta(days=5)),
            demand("SO-NODATES"),                            # all blank -> today
        ]
        result = build([], rows)
        self.assertFalse(line_of(result, "SO-DEL-OUT")["in_picklist_window"])
        self.assertTrue(line_of(result, "SO-DEL-IN")["in_picklist_window"])
        self.assertTrue(line_of(result, "SO-SHIP-IN")["in_picklist_window"])
        self.assertTrue(line_of(result, "SO-DESIRED")["in_picklist_window"])
        self.assertTrue(line_of(result, "SO-NODATES")["in_picklist_window"])


class PreviewSuggestTests(unittest.TestCase):
    def _ten_orders(self):
        return [demand(f"SO-{i:02d}", hdr_promise_del=date(2026, 8, i))
                for i in range(1, 11)]

    def test_preview_override_changes_position_without_mutating_inputs(self):
        supply_rows = [supply("ON_HAND", 3, supply_id="SHIPPING/R01-A")]
        demand_rows = self._ten_orders()
        snapshot = copy.deepcopy(demand_rows)
        preview = preview_change(supply_rows, demand_rows, TODAY, LOOKAHEAD, (),
                                 "SO-10", 1, date(2026, 8, 2))
        self.assertEqual(demand_rows, snapshot)  # inputs untouched
        self.assertEqual(preview["baseline_position"], 10)
        # 8/2 ties SO-02; SO-10 sorts after on SO id -> position 3.
        self.assertEqual(preview["new_position"], 3)
        displaced_keys = {(d["so"], d["line_no"]) for d in preview["displaced"]}
        self.assertIn(("SO-03", 1), displaced_keys)

    def test_position_10_to_3_roundtrip_via_override(self):
        # Case 13 — the headline workflow.
        demand_rows = self._ten_orders()
        suggestion = suggest_promise_del([], demand_rows, TODAY, LOOKAHEAD, (),
                                         "SO-10", 1, 3)
        self.assertNotIn("error", suggestion)
        self.assertEqual(suggestion["predicted_position"], 3)
        new_date = date.fromisoformat(suggestion["suggested_date"])
        result = build([], demand_rows, overrides={("SO-10", 1): new_date})
        self.assertEqual(positions(result)[("SO-10", 1)], 3)

    def test_suggest_lands_target_when_tie_breakers_already_win(self):
        rows = [demand("SO-AAA", hdr_promise_del=date(2026, 8, 5)),
                demand("SO-ZZZ", hdr_promise_del=date(2026, 8, 10))]
        suggestion = suggest_promise_del([], rows, TODAY, LOOKAHEAD, (),
                                         "SO-AAA", 1, 2)
        self.assertEqual(suggestion["predicted_position"], 2)

    def test_suggest_rejects_out_of_range_target(self):
        rows = [demand("SO-1"), demand("SO-2")]
        suggestion = suggest_promise_del([], rows, TODAY, LOOKAHEAD, (),
                                         "SO-1", 1, 9)
        self.assertEqual(suggestion.get("error"), "position_unreachable")

    def test_preview_clear_line_value_falls_back_to_header(self):
        rows = [demand("SO-1", hdr_promise_del=date(2026, 9, 1),
                       line_promise_del=date(2026, 8, 1)),
                demand("SO-2", hdr_promise_del=date(2026, 8, 15))]
        preview = preview_change([], rows, TODAY, LOOKAHEAD, (), "SO-1", 1, None)
        self.assertEqual(preview["baseline_position"], 1)
        self.assertEqual(preview["new_position"], 2)  # falls back to 9/1 header
        line = line_of(preview["result"], "SO-1")
        self.assertEqual(line["dates"]["eff_promise_del"], "2026-09-01")
        self.assertEqual(line["dates"]["promise_del_source"], "header")


class RobustnessTests(unittest.TestCase):
    def test_null_handling_nan_nat_inputs(self):
        nan = float("nan")
        row = demand("SO-1")
        row["HDR_PROMISE_DEL_DATE"] = nan
        row["ORDER_DATE"] = nan
        row["SHIPPED_QTY"] = nan
        row["CUSTOMER_NAME"] = nan
        result = build([supply("ON_HAND", nan, supply_id="SHIPPING/R01-A")], [row])
        line = line_of(result, "SO-1")
        self.assertIsNone(line["dates"]["eff_promise_del"])
        self.assertIsNone(line["customer_name"])
        # NaN supply qty coerces to 0 -> no event at all.
        self.assertEqual(result["supply"]["events"], [])

    def test_zero_open_qty_rows_dropped(self):
        result = build([], [demand("SO-1", order_qty=2, shipped_qty=2),
                            demand("SO-2")])
        self.assertEqual(len(result["demand"]["lines"]), 1)

    def test_priority_key_matches_guns_sql_shape(self):
        # Guard: the documented key order must not drift.
        line = {
            "eff_promise_del": None, "eff_promise_ship": date(2026, 8, 1),
            "hdr_desired": None, "order_date": date(2026, 1, 1),
            "so": "SO-1", "line_no": 2,
        }
        key = allocation._priority_key(line, TODAY)
        self.assertEqual(key, (True, date.min, False, date(2026, 8, 1),
                               TODAY, date(2026, 1, 1), "SO-1", 2))


if __name__ == "__main__":
    unittest.main()
