import unittest
from datetime import date, datetime, timezone

from picklist.domain import readiness

TODAY = date(2026, 10, 1)


def row(order="SO-1", line=1, **overrides):
    base = {
        "CUST_ORDER_ID": order,
        "LINE_NO": line,
        "CUSTOMER_ID": "DEALER1",
        "CUSTOMER_NAME": "Dealer One",
        "ORDER_STATUS": "R",
        "LINE_STATUS": "A",
        "ORDER_DATE": date(2026, 9, 20),
        "SHIP_TO_ID": "001",
        "SHIP_TO_ADDR_NO": 1,
        "PART_ID": "801-06486-00",
        "PRODUCT_CODE": "RIDGELINE FFT",
        "PART_DESCRIPTION": "Ridgeline FFT 6.5 CM",
        "ITEM_TYPE": "guns",
        "ORDER_QTY": 1,
        "SHIPPED_QTY": 0,
        "OPEN_QTY": 1,
        "OPEN_VALUE": 1500.0,
        "AVAILABLE_QTY": 3,
        "PROMISE_SHIP_DATE": date(2026, 10, 5),
        "PROMISE_DEL_DATE": None,
        "DESIRED_SHIP_DATE": date(2026, 10, 3),
        "SHIP_VIA": "UPS GROUND",
        "FREE_ON_BOARD": "ORIGIN",
        "SALESREP_ID": "LUNN",
        "CUSTOMER_PO_REF": "PO123",
        "DISCOUNT_CODE": "DEALER",
        "SHIPTO_NAME": "Dealer One Guns",
        "SHIPTO_ADDR_1": "490 IH 35 South",
        "SHIPTO_CITY": "Austin",
        "SHIPTO_STATE": "TX",
        "SHIPTO_ZIP": "78701",
        "SHIPTO_COUNTRY": "USA",
        "SHIPTO_ACTIVE": "Y",
        "SHIPTO_FFL_NUMBER": "5-74-453-01-2A-12345",
        "SHIPTO_FFL_EXPIRY_RAW": "1/1/2028",
        "MASTER_FFL_NUMBER": "5-74-453-01-2A-12345",
        "MASTER_FFL_EXPIRY_RAW": "01-01-2028",
        "CREDIT_STATUS": "A",
        "CREDIT_LIMIT": 50000.0,
        "CREDIT_LIMIT_CTL": "O",
        "SHIP_CREDIT_LIMIT_CTL": "C",
        "TOTAL_OPEN_RECV": 1000.0,
        "TOTAL_OPEN_SHIPPED": 0.0,
        "TOTAL_OPEN_ORDERS": 1500.0,
        "ATTACHMENT_COUNT": 2,
        "FFL_EZ_CHECK_COUNT": 1,
        "FFL_MASTER_COUNT": 0,
        "IS_RMA": 0,
        "IS_INTERNATIONAL": 0,
        "IS_EMPLOYEE": 0,
        "EXCLUDED_CUSTOMER": 0,
    }
    base.update(overrides)
    return base


def evaluate(rows, **kwargs):
    kwargs.setdefault("today", TODAY)
    return readiness.evaluate_readiness(rows, **kwargs)


def codes(payload, order="SO-1"):
    found = next(o for o in payload["orders"] if o["order_id"] == order)
    return sorted(h["reason_code"] for h in found["holds"])


def order(payload, order_id="SO-1"):
    return next(o for o in payload["orders"] if o["order_id"] == order_id)


class ParseExpirationTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "1/1/2026": date(2026, 1, 1),
            "01-01-2026": date(2026, 1, 1),
            "1-1-2026": date(2026, 1, 1),
            "12/1/2027": date(2027, 12, 1),
            "1/1/27": date(2027, 1, 1),
            "2026-04-01": date(2026, 4, 1),
            "2-2027": date(2027, 2, 1),
            "": None,
            None: None,
            "expired": None,
            "13/40/2026": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(readiness.parse_expiration(raw), expected)

    def test_numbers_match_with_mask(self):
        self.assertTrue(readiness.ffl_numbers_match("1-59-001-01-9D-30995", "1-XX-XXX-XX-XX-30995"))
        self.assertFalse(readiness.ffl_numbers_match("1-59-001-01-9D-30995", "1-59-001-01-9D-30996"))
        self.assertTrue(readiness.ffl_numbers_match("159001019D30995", "1-59-001-01-9D-30995"))
        self.assertFalse(readiness.ffl_numbers_match("", "x"))


class ReadinessRulesTests(unittest.TestCase):
    def test_clean_order_is_ready(self):
        payload = evaluate([row()])
        self.assertEqual(codes(payload), [])
        self.assertEqual(order(payload)["state"], "READY")
        self.assertEqual(payload["summary"]["ready"], 1)
        self.assertTrue(order(payload)["firearms"])

    def test_firmed_and_hold_statuses(self):
        self.assertIn("order_not_released", codes(evaluate([row(ORDER_STATUS="F")])))
        held = evaluate([row(ORDER_STATUS="H")])
        self.assertIn("order_hold", codes(held))
        self.assertEqual(order(held)["state"], "BLOCKED")
        self.assertEqual(order(held)["owner_teams"], ["sales"])

    def test_closed_lines_are_listed_but_not_evaluated(self):
        payload = evaluate([row(ORDER_STATUS="C", LINE_STATUS="C", OPEN_QTY=0, SHIPTO_FFL_NUMBER="")])
        self.assertEqual(codes(payload), [])
        self.assertTrue(order(payload)["closed"])
        self.assertEqual(order(payload)["line_count"], 1)

    def test_ffl_missing_expired_and_unparseable(self):
        missing = evaluate([row(SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER="")])
        self.assertIn("ffl_missing", codes(missing))
        expired = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="9/30/2026")])
        self.assertIn("ffl_expired", codes(expired))
        hold = next(h for h in order(expired)["holds"] if h["reason_code"] == "ffl_expired")
        self.assertEqual(hold["detail"]["expiry_raw"], "9/30/2026")
        self.assertEqual(hold["detail"]["days_ago"], 1)
        weird = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="see file")])
        self.assertIn("ffl_unparseable_expiry", codes(weird))
        self.assertEqual(order(weird)["state"], "ATTENTION")

    def test_ffl_uses_shipto_before_master(self):
        payload = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="1/1/2029", MASTER_FFL_EXPIRY_RAW="1/1/2020")])
        self.assertEqual(codes(payload), [])
        self.assertEqual(order(payload)["ffl"]["source"], "shipto")
        fallback = evaluate([row(SHIPTO_FFL_NUMBER="", SHIPTO_FFL_EXPIRY_RAW="", MASTER_FFL_EXPIRY_RAW="1/1/2020")])
        self.assertIn("ffl_expired", codes(fallback))
        self.assertEqual(order(fallback)["ffl"]["source"], "master")

    def test_ffl_expires_before_promise_and_number_mismatch_is_not_a_hold(self):
        payload = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="10/3/2026", PROMISE_SHIP_DATE=date(2026, 10, 10))])
        self.assertIn("ffl_expires_before_promise", codes(payload))
        mismatch = evaluate([row(MASTER_FFL_NUMBER="5-74-453-01-2A-99999")])
        self.assertEqual(codes(mismatch), [])
        self.assertFalse(order(mismatch)["ffl"]["numbers_agree"])

    def test_ffl_missing_on_both_records_is_critical(self):
        payload = evaluate([row(SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER="")])
        hold = next(h for h in order(payload)["holds"] if h["reason_code"] == "ffl_missing")
        self.assertTrue(hold["critical"])
        self.assertTrue(hold["blocking"])
        self.assertEqual(order(payload)["state"], "BLOCKED")
        self.assertIsNone(order(payload)["ffl"]["source"])
        # Every other hold is not critical.
        expired = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="9/30/2026")])
        self.assertFalse(any(h["critical"] for h in order(expired)["holds"]))

    def test_expired_shipto_ffl_is_not_rescued_by_current_master(self):
        payload = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="9/30/2026", MASTER_FFL_EXPIRY_RAW="1/1/2028")])
        self.assertIn("ffl_expired", codes(payload))
        hold = next(h for h in order(payload)["holds"] if h["reason_code"] == "ffl_expired")
        self.assertEqual(hold["detail"]["source"], "shipto")
        self.assertTrue(hold["detail"]["master_current"])
        self.assertIn("copy", hold["detail"]["fix"])
        stale_master = evaluate([row(SHIPTO_FFL_EXPIRY_RAW="9/30/2026", MASTER_FFL_EXPIRY_RAW="1/1/2020")])
        hold = next(h for h in order(stale_master)["holds"] if h["reason_code"] == "ffl_expired")
        self.assertFalse(hold["detail"]["master_current"])
        self.assertIn("renewed", hold["detail"]["fix"])
        fallback = evaluate([row(SHIPTO_FFL_NUMBER="", SHIPTO_FFL_EXPIRY_RAW="", MASTER_FFL_EXPIRY_RAW="1/1/2020")])
        hold = next(h for h in order(fallback)["holds"] if h["reason_code"] == "ffl_expired")
        self.assertEqual(hold["detail"]["source"], "master")
        self.assertIn("Ship-to has no FFL", hold["detail"]["fix"])

    def test_without_stock_reasons_recomputes_state_and_owners(self):
        payload = evaluate([
            row(order="SO-1", AVAILABLE_QTY=0),
            row(order="SO-2", AVAILABLE_QTY=0, SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER=""),
            row(order="SO-3"),
        ])
        by_id = {o["order_id"]: o for o in payload["orders"]}
        self.assertEqual(by_id["SO-1"]["state"], "BLOCKED")
        view = readiness.without_reasons(payload["orders"], readiness.STOCK_REASONS)
        v = {o["order_id"]: o for o in view}
        self.assertEqual(v["SO-1"]["state"], "READY")
        self.assertEqual(v["SO-1"]["holds"], [])
        self.assertEqual(v["SO-1"]["hidden_hold_count"], 1)
        self.assertEqual(v["SO-1"]["owner_teams"], [])
        self.assertEqual(v["SO-2"]["state"], "BLOCKED")
        self.assertEqual([h["reason_code"] for h in v["SO-2"]["holds"]], ["ffl_missing"])
        self.assertEqual(v["SO-2"]["owner_teams"], ["sales"])
        self.assertEqual(v["SO-3"]["hidden_hold_count"], 0)
        # the original payload is untouched
        self.assertEqual(by_id["SO-1"]["state"], "BLOCKED")
        summary = readiness.summarize_orders(view)
        self.assertEqual((summary["blocked"], summary["ready"]), (1, 2))
        self.assertNotIn("production", summary["by_owner"])
        self.assertNotIn("no_supply", summary["by_reason"])

    def test_orders_view_hides_rma_and_stock(self):
        from picklist.services import readiness_service
        payload = evaluate([
            row(order="SO-1", IS_RMA=1),
            row(order="SO-2", AVAILABLE_QTY=0),
            row(order="SO-3", ORDER_STATUS="F"),
        ])
        view = readiness_service.orders_view(payload)
        ids = [o["order_id"] for o in view["orders"]]
        self.assertEqual(ids, ["SO-2", "SO-3"])
        self.assertEqual(view["rma_hidden"], 1)
        self.assertTrue(view["stock_holds_hidden"])
        self.assertEqual(view["summary"]["orders"], 2)
        self.assertEqual((view["summary"]["blocked"], view["summary"]["ready"]), (1, 1))
        everything = readiness_service.orders_view(payload, hide_stock=False, hide_rma=False)
        self.assertEqual(len(everything["orders"]), 3)
        self.assertEqual(everything["rma_hidden"], 0)
        self.assertEqual(len(payload["orders"]), 3)  # source untouched

    def test_orders_view_drops_retired_holds_written_by_old_evaluators(self):
        from picklist.services import readiness_service
        payload = evaluate([row(order="SO-1", ORDER_STATUS="F")])
        stale = readiness._hold("SO-1", "ship_via_missing")
        stale2 = readiness._hold("SO-1", "ffl_master_shipto_mismatch")
        payload["orders"][0]["holds"].extend([stale, stale2])
        view = readiness_service.orders_view(payload)
        codes_seen = [h["reason_code"] for h in view["orders"][0]["holds"]]
        self.assertEqual(codes_seen, ["order_not_released"])
        self.assertEqual(view["orders"][0]["hidden_hold_count"], 0)
        self.assertNotIn("ship_via_missing", view["summary"]["by_reason"])

    def test_retired_reasons_hidden_from_filter(self):
        from picklist.services import readiness_service
        codes_offered = {o["code"] for o in readiness_service.reason_options()}
        self.assertNotIn("ffl_master_shipto_mismatch", codes_offered)
        self.assertIn("ffl_missing", codes_offered)

    def test_ffl_doc_requirement_is_configurable(self):
        payload = evaluate([row(FFL_EZ_CHECK_COUNT=0, FFL_MASTER_COUNT=0)])
        self.assertIn("ffl_doc_missing", codes(payload))
        relaxed = evaluate([row(FFL_EZ_CHECK_COUNT=0, FFL_MASTER_COUNT=0)], config={"require_ffl_doc": False})
        self.assertEqual(codes(relaxed), [])

    def test_ffl_rules_skip_components_and_excluded_classes(self):
        components = evaluate([row(ITEM_TYPE="components", SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER="", FFL_EZ_CHECK_COUNT=0)])
        self.assertEqual(codes(components), [])
        self.assertFalse(order(components)["firearms"])
        international = evaluate([row(IS_INTERNATIONAL=1, SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER="")])
        self.assertEqual(codes(international), ["excluded_class"])
        hold = order(international)["holds"][0]
        self.assertEqual(hold["owner_team"], "compliance")
        employee = evaluate([row(IS_EMPLOYEE=1)])
        self.assertEqual(order(employee)["holds"][0]["owner_team"], "shipping")
        rma = evaluate([row(IS_RMA=1, SHIPTO_FFL_NUMBER="", MASTER_FFL_NUMBER="")])
        self.assertEqual(codes(rma), ["rma_excluded"])

    def test_credit_status_and_limit(self):
        status = evaluate([row(CREDIT_STATUS="H")])
        self.assertIn("credit_status_hold", codes(status))
        self.assertEqual(order(status)["owner_teams"], ["finance"])
        # exposure 1000 + order 1500 = 2500 > limit 2000
        over = evaluate([row(CREDIT_LIMIT=2000.0)])
        self.assertIn("credit_limit_would_exceed", codes(over))
        detail = next(h for h in order(over)["holds"] if h["reason_code"] == "credit_limit_would_exceed")["detail"]
        self.assertAlmostEqual(detail["over_by"], 500.0)
        # exactly at the limit is fine
        at_limit = evaluate([row(CREDIT_LIMIT=2500.0)])
        self.assertNotIn("credit_limit_would_exceed", codes(at_limit))
        # no ship-time check configured -> no limit hold
        no_check = evaluate([row(CREDIT_LIMIT=2000.0, SHIP_CREDIT_LIMIT_CTL="N")])
        self.assertNotIn("credit_limit_would_exceed", codes(no_check))
        # zero limit means unlimited
        unlimited = evaluate([row(CREDIT_LIMIT=0)])
        self.assertNotIn("credit_limit_would_exceed", codes(unlimited))

    def test_credit_sums_all_open_lines_of_the_order(self):
        rows = [row(line=1, OPEN_VALUE=800.0, CREDIT_LIMIT=2500.0), row(line=2, OPEN_VALUE=800.0, CREDIT_LIMIT=2500.0)]
        payload = evaluate(rows)
        self.assertIn("credit_limit_would_exceed", codes(payload))
        self.assertAlmostEqual(order(payload)["open_value"], 1600.0)

    def test_ship_to_and_ship_via(self):
        no_shipto = evaluate([row(SHIPTO_NAME="", SHIPTO_ADDR_1="", SHIP_TO_ID="", SHIPTO_FFL_NUMBER="")])
        self.assertIn("ship_to_missing", codes(no_shipto))
        # Blank ship via is routine for Sales; it is no longer a hold.
        blank_via = evaluate([row(SHIP_VIA=None)])
        self.assertNotIn("ship_via_missing", codes(blank_via))
        self.assertIsNone(order(blank_via)["ship_via_source"])

    def test_supply_per_line(self):
        rows = [row(line=1, AVAILABLE_QTY=0), row(line=2, OPEN_QTY=3, OPEN_VALUE=4500.0, AVAILABLE_QTY=1)]
        payload = evaluate(rows)
        self.assertEqual(codes(payload), ["no_supply", "partial_supply"])
        holds = order(payload)["holds"]
        self.assertEqual(holds[0]["line_no"], "1")
        self.assertEqual(holds[1]["line_no"], "2")
        self.assertEqual(holds[1]["detail"]["available_qty"], 1.0)
        self.assertEqual(order(payload)["owner_teams"], ["production"])

    def test_not_on_picklist_only_when_ready_in_window_and_run_exists(self):
        ready = evaluate([row()], picklist_orders=set(), picklist_horizon=date(2026, 10, 11))
        self.assertEqual(codes(ready), ["not_on_picklist"])
        self.assertFalse(order(ready)["on_picklist"])
        listed = evaluate([row()], picklist_orders={"so-1"}, picklist_horizon=date(2026, 10, 11))
        self.assertEqual(codes(listed), [])
        self.assertTrue(order(listed)["on_picklist"])
        no_run = evaluate([row()], picklist_orders=None)
        self.assertEqual(codes(no_run), [])
        self.assertIsNone(order(no_run)["on_picklist"])
        beyond = evaluate([row(PROMISE_SHIP_DATE=date(2026, 11, 1))], picklist_orders=set(), picklist_horizon=date(2026, 10, 11))
        self.assertEqual(codes(beyond), [])
        blocked = evaluate([row(ORDER_STATUS="F")], picklist_orders=set(), picklist_horizon=date(2026, 10, 11))
        self.assertNotIn("not_on_picklist", codes(blocked))

    def test_gate_passthrough(self):
        gate = {"SO-1": {"decision": "ACCUMULATING", "reason_code": "accumulating_for_daily_batch", "label": "ACCUMULATING - 2/42", "next_release_date": "2026-10-02", "enforced": True, "mode": "enforced"}}
        payload = evaluate([row()], gate_decisions=gate)
        self.assertEqual(codes(payload), ["gate_hold"])
        self.assertTrue(order(payload)["gate"]["enforced"])
        # Advisory decisions are shown on the order but never become a hold.
        advisory = evaluate([row()], gate_decisions={"SO-1": {**gate["SO-1"], "enforced": False, "mode": "advisory"}})
        self.assertEqual(codes(advisory), [])
        self.assertEqual(advisory["orders"][0]["gate"]["decision"], "ACCUMULATING")
        self.assertFalse(advisory["orders"][0]["gate"]["enforced"])
        legacy = evaluate([row()], gate_decisions={"SO-1": {"decision": "HOLD", "reason_code": "x", "label": "HOLD"}})
        self.assertEqual(codes(legacy), [])
        self.assertEqual(order(payload)["state"], "ATTENTION")
        self.assertEqual(order(payload)["gate"]["next_release_date"], "2026-10-02")
        released = evaluate([row()], gate_decisions={"SO-1": {"decision": "RELEASE", "reason_code": "complete", "label": "SHIP NOW"}})
        self.assertEqual(codes(released), [])

    def test_manual_hold(self):
        payload = evaluate([row()], manual_holds=[{"cust_order_id": "so-1", "hold_kind": "marketing", "reason": "Trade show", "expires_at": "2026-10-08", "request_id": 7}])
        self.assertEqual(codes(payload), ["manual_hold"])
        self.assertEqual(order(payload)["holds"][0]["detail"]["hold_kind"], "marketing")
        self.assertEqual(order(payload)["state"], "BLOCKED")

    def test_doc_findings_tier2(self):
        findings = {"SO-1": [{"reason_code": "ship_to_vs_ffl_premise_mismatch", "passed": 0, "detail": {"shipto": "490 I-35", "premise": "490 IH 35 South"}}]}
        payload = evaluate([row()], doc_findings=findings)
        self.assertEqual(codes(payload), ["ship_to_vs_ffl_premise_mismatch"])
        self.assertEqual(order(payload)["holds"][0]["tier"], 2)

    def test_summary_and_grouping(self):
        rows = [
            row(order="SO-A", line=1, ORDER_STATUS="F"),
            row(order="SO-A", line=2, ORDER_STATUS="F", AVAILABLE_QTY=0),
            row(order="SO-B", CREDIT_STATUS="H"),
            row(order="SO-C"),
        ]
        payload = evaluate(rows)
        self.assertEqual(payload["summary"]["orders"], 3)
        self.assertEqual(payload["summary"]["blocked"], 2)
        self.assertEqual(payload["summary"]["ready"], 1)
        self.assertEqual(payload["summary"]["by_reason"]["order_not_released"], 1)
        self.assertEqual(payload["summary"]["by_owner"]["sales"], 1)
        self.assertEqual(payload["summary"]["by_owner"]["finance"], 1)
        self.assertEqual(order(payload, "SO-A")["line_count"], 2)
        self.assertEqual(len(payload["holds"]), 3)

    def test_evaluated_at_is_respected(self):
        stamp = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        payload = evaluate([row()], evaluated_at=stamp)
        self.assertEqual(payload["evaluated_at"], stamp.isoformat())


class BinClassificationTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(readiness.classify_bin("SHIPPING", "R03S03"), "pickable")
        self.assertEqual(readiness.classify_bin("SHIPPING", "R10"), "rack10")
        self.assertEqual(readiness.classify_bin("SHIPPING", "R11S01"), "r11_components")
        self.assertEqual(readiness.classify_bin("SHIPPING", "STAGE-A"), "stage")
        self.assertEqual(readiness.classify_bin("SHIPPING", "INTERNATIONAL"), "international")
        self.assertEqual(readiness.classify_bin("MAIN", "C2-SERIALIZED"), "main")
        self.assertEqual(readiness.classify_bin("DISTRIBUTION", "R14S4B6"), "distribution")
        self.assertEqual(readiness.classify_bin("DISTRIBUTION", "STOCK-1"), "distribution_stock")

    def test_group_locations(self):
        rows = [
            {"PART_ID": "801-1", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "R03S03", "QTY": 2},
            {"PART_ID": "801-1", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "STAGE", "QTY": 1},
            {"PART_ID": "801-1", "WAREHOUSE_ID": "MAIN", "LOCATION_ID": "C2", "QTY": 5},
            {"PART_ID": "801-2", "WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": "R10", "QTY": 1},
        ]
        grouped = readiness.group_locations(rows)
        self.assertEqual(grouped["801-1"]["pickable_qty"], 2.0)
        self.assertEqual(grouped["801-1"]["by_class"]["main"], 5.0)
        self.assertEqual(grouped["801-1"]["bins"][0]["location"], "R03S03")
        self.assertEqual(grouped["801-2"]["by_class"], {"rack10": 1.0})


if __name__ == "__main__":
    unittest.main()
