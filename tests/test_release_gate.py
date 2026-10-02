import unittest
from datetime import date, datetime, timedelta, timezone

from picklist.domain import release_gate


TODAY = date(2026, 8, 27)  # Thursday


def candidate(order, part="GUN-A", open_qty=1, available=1, customer="CUST",
              promise=None, item_type="guns", line=1, **flags):
    row = {
        "CUST_ORDER_ID": order,
        "LINE_NO": line,
        "CUSTOMER_ID": customer,
        "CUSTOMER_NAME": customer + " NAME",
        "ORDER_DATE": date(2026, 8, 1),
        "PART_ID": part,
        "ITEM_TYPE": item_type,
        "OPEN_QTY": open_qty,
        "AVAILABLE_QTY": available,
        "PROMISE_SHIP_DATE": promise or date(2026, 9, 15),
        "ORDER_RELEASED": 1,
        "LINE_AVAILABLE": 1,
        "CREDIT_APPROVED": 1,
        "SHIP_TO_PRESENT": 1,
        "IS_RMA": 0,
        "IS_INTERNATIONAL": 0,
        "IS_EMPLOYEE": 0,
        "EXCLUDED_CUSTOMER": 0,
    }
    row.update(flags)
    return row


def decision(payload, order):
    return next(row for row in payload["decisions"] if row["order_id"] == order)


class ReleaseGateTests(unittest.TestCase):
    def build(self, rows, **kwargs):
        return release_gate.evaluate_release_gate(rows, today=TODAY, **kwargs)

    def test_complete_order_releases(self):
        result = self.build([candidate("SO-1", open_qty=2, available=2)])
        row = decision(result, "SO-1")
        self.assertEqual((row["decision"], row["reason_code"]), ("RELEASE", "complete"))
        self.assertEqual(result["released_orders"], ["SO-1"])

    def test_incomplete_order_holds_without_reserving_supply(self):
        rows = [
            candidate("SO-HOLD", open_qty=2, available=1, promise=date(2026, 9, 1)),
            candidate("SO-FLOW", open_qty=1, available=1, promise=date(2026, 9, 2)),
        ]
        result = self.build(rows)
        self.assertEqual(decision(result, "SO-HOLD")["reason_code"], "waiting_for_completion")
        self.assertEqual(decision(result, "SO-FLOW")["decision"], "RELEASE")

    def test_accumulating_order_protects_supply_from_later_orders(self):
        rows = [
            candidate(
                "SO-BIG", open_qty=3, available=2, customer="BIG",
                promise=date(2026, 9, 1),
            ),
            candidate(
                "SO-OTHER", open_qty=2, available=2, customer="OTHER",
                promise=date(2026, 9, 2),
            ),
        ]
        result = self.build(rows, customer_policies={"BIG": {"accumulate": True}})
        accumulating = decision(result, "SO-BIG")
        self.assertEqual(accumulating["decision"], "ACCUMULATING")
        self.assertEqual(accumulating["protected_guns"], 2)
        self.assertEqual(decision(result, "SO-OTHER")["decision"], "HOLD")
        self.assertEqual(result["protected_supply"], {"GUN-A": 2})
        self.assertEqual(result["summary"]["protected_guns"], 2)
        self.assertEqual(result["released_orders"], [])

    def test_accumulation_target_releases_partial_batch(self):
        result = self.build(
            [candidate("SO-BIG", open_qty=3, available=2, customer="BIG")],
            customer_policies={"BIG": {"accumulate": True, "min_guns": 2}},
        )
        row = decision(result, "SO-BIG")
        self.assertEqual(row["decision"], "RELEASE")
        self.assertEqual(row["reason_code"], "accumulation_target_reached")
        self.assertEqual((row["ready_guns"], row["missing_qty"]), (2, 1))

    def test_accumulation_sweep_releases_available_remainder(self):
        result = self.build(
            [candidate("SO-BIG", open_qty=100, available=17, customer="BIG")],
            customer_policies={
                "BIG": {"accumulate": True, "min_guns": 100, "sweep_weekday": 3}
            },
        )
        row = decision(result, "SO-BIG")
        self.assertEqual((row["decision"], row["reason_code"]), (
            "RELEASE", "scheduled_customer_release"
        ))
        self.assertEqual(row["ready_guns"], 17)

    def test_complete_accumulating_order_releases_without_batch_constraints(self):
        result = self.build(
            [candidate("SO-BIG", open_qty=2, available=2, customer="BIG")],
            customer_policies={"BIG": {"accumulate": True}},
        )
        self.assertEqual(decision(result, "SO-BIG")["decision"], "RELEASE")

    def test_at_risk_accumulating_order_requires_review_instead_of_splitting(self):
        result = self.build(
            [candidate(
                "SO-BIG", open_qty=3, available=1, customer="BIG", promise=TODAY
            )],
            customer_policies={"BIG": {"accumulate": True}},
        )
        row = decision(result, "SO-BIG")
        self.assertEqual(row["decision"], "ACCUMULATING")
        self.assertEqual(row["reason_code"], "accumulating_commitment_at_risk")
        self.assertTrue(row["commitment_at_risk"])
        self.assertIn("review exception", row["label"])

    def test_exception_overrides_accumulation(self):
        exception = {
            "cust_order_id": "SO-BIG",
            "expires_at": TODAY + timedelta(days=1),
            "revoked_at": None,
        }
        result = self.build(
            [candidate("SO-BIG", open_qty=3, available=1, customer="BIG")],
            customer_policies={"BIG": {"accumulate": True}},
            exceptions=[exception],
        )
        self.assertEqual(decision(result, "SO-BIG")["reason_code"], "approved_exception")

    def test_ordinary_at_risk_order_keeps_priority_over_accumulation(self):
        rows = [
            candidate(
                "SO-URGENT", open_qty=1, available=1, customer="OTHER", promise=TODAY
            ),
            candidate(
                "SO-BIG", open_qty=1, available=1, customer="BIG",
                promise=date(2026, 9, 1),
            ),
        ]
        result = self.build(rows, customer_policies={"BIG": {"accumulate": True}})
        self.assertEqual(decision(result, "SO-URGENT")["decision"], "RELEASE")
        self.assertEqual(decision(result, "SO-BIG")["decision"], "ACCUMULATING")
        self.assertEqual(decision(result, "SO-BIG")["protected_qty"], 0)

    def test_commitment_at_risk_can_release_partial(self):
        row = candidate(
            "SO-DUE", open_qty=3, available=1,
            promise=TODAY + timedelta(days=1),
        )
        result = self.build([row], due_override_days=1)
        picked = decision(result, "SO-DUE")
        self.assertEqual(picked["reason_code"], "commitment_at_risk")
        self.assertEqual(picked["ready_qty"], 1)
        self.assertEqual(picked["missing_qty"], 2)

    def test_hard_block_precedes_due_override_and_exception(self):
        row = candidate(
            "SO-BLOCK", promise=TODAY, CREDIT_APPROVED=0, available=1
        )
        exception = {
            "cust_order_id": "SO-BLOCK", "reason": "expedite",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            "revoked_at": None,
        }
        result = self.build([row], exceptions=[exception])
        self.assertEqual(decision(result, "SO-BLOCK")["decision"], "BLOCKED")

    def test_active_exception_releases_and_expired_exception_does_not(self):
        row = candidate("SO-1", open_qty=2, available=1)
        active = {"cust_order_id": "SO-1", "expires_at": TODAY + timedelta(days=1), "revoked_at": None}
        result = self.build([row], exceptions=[active])
        self.assertEqual(decision(result, "SO-1")["reason_code"], "approved_exception")

        expired = {"cust_order_id": "SO-1", "expires_at": TODAY - timedelta(days=1), "revoked_at": None}
        result = self.build([row], exceptions=[expired])
        self.assertEqual(decision(result, "SO-1")["reason_code"], "waiting_for_completion")

    def test_customer_minimum_holds_then_releases_batch(self):
        rows = [
            candidate("SO-1", available=2, customer="BIG", promise=date(2026, 9, 1)),
            candidate("SO-2", available=2, customer="BIG", promise=date(2026, 9, 2)),
        ]
        held = self.build(rows, customer_policies={"BIG": {"min_guns": 3}})
        self.assertEqual(held["summary"]["hold"], 2)
        released = self.build(rows, customer_policies={"BIG": {"min_guns": 2}})
        self.assertEqual(released["summary"]["release"], 2)

    def test_customer_sweep_day_releases_below_threshold(self):
        result = self.build(
            [candidate("SO-1", customer="BIG")],
            customer_policies={"BIG": {"min_guns": 24, "sweep_weekday": 3}},
        )
        self.assertEqual(decision(result, "SO-1")["reason_code"], "scheduled_customer_release")

    def test_non_sweep_hold_exposes_next_release_date(self):
        result = release_gate.evaluate_release_gate(
            [candidate("SO-1", customer="BIG")],
            today=date(2026, 8, 26),  # Wednesday
            customer_policies={"BIG": {"min_guns": 24, "sweep_weekday": 3}},
        )
        row = decision(result, "SO-1")
        self.assertEqual(row["reason_code"], "scheduled_hold")
        self.assertEqual(row["next_release_date"], "2026-08-27")

    def test_held_customer_batch_does_not_consume_supply(self):
        rows = [
            candidate("SO-BIG", customer="BIG", promise=date(2026, 9, 1), available=1),
            candidate("SO-OTHER", customer="OTHER", promise=date(2026, 9, 2), available=1),
        ]
        result = self.build(rows, customer_policies={"BIG": {"min_guns": 24}})
        self.assertEqual(decision(result, "SO-BIG")["decision"], "HOLD")
        self.assertEqual(decision(result, "SO-OTHER")["decision"], "RELEASE")

    def test_components_only_complete_order_is_not_held_for_gun_minimum(self):
        row = candidate("SO-COMP", part="PART-X", item_type="components", customer="BIG")
        result = self.build([row], customer_policies={"BIG": {"min_guns": 24}})
        self.assertEqual(decision(result, "SO-COMP")["decision"], "RELEASE")

    def test_no_supply_is_an_explicit_hold(self):
        result = self.build([candidate("SO-1", available=0, promise=TODAY)])
        self.assertEqual(decision(result, "SO-1")["reason_code"], "no_supply")

    def test_supply_is_never_allocated_twice(self):
        rows = [
            candidate("SO-1", available=1, promise=date(2026, 9, 1)),
            candidate("SO-2", available=1, promise=date(2026, 9, 2)),
        ]
        result = self.build(rows)
        self.assertEqual(result["summary"]["release"], 1)
        self.assertEqual(result["summary"]["hold"], 1)
        self.assertEqual(result["summary"]["release_units"], 1)

    def test_filter_released_rows(self):
        payload = self.build([candidate("SO-1"), candidate("SO-2", part="GUN-B")])
        rows = [{"Cust Order ID": "SO-1"}, {"Cust Order ID": "SO-X"}]
        self.assertEqual(release_gate.filter_released_rows(rows, payload), [rows[0]])

    def test_policy_version_and_mode_are_preserved(self):
        result = self.build([candidate("SO-1")], mode="enforced", policy_version=7)
        self.assertEqual(result["mode"], "enforced")
        self.assertEqual(result["policy_version"], 7)

    def test_major_account_uses_one_active_sales_order(self):
        rows = [
            candidate("SO-OLD", open_qty=50, available=42, customer="MAJOR", promise=date(2026, 9, 1)),
            candidate("SO-NEW", open_qty=10, available=42, customer="MAJOR", promise=date(2026, 9, 2)),
        ]
        result = self.build(
            rows,
            customer_policies={
                "MAJOR": {
                    "account_type": "major",
                    "accumulate": True,
                    "target_guns": 42,
                    "mix_orders": False,
                    "max_hold_days": 7,
                }
            },
        )
        self.assertEqual(decision(result, "SO-OLD")["reason_code"], "accumulation_target_reached")
        self.assertEqual(
            decision(result, "SO-NEW")["reason_code"],
            "waiting_for_prior_major_order",
        )

    def test_major_account_releases_complete_remainder_below_target(self):
        result = self.build(
            [candidate("SO-OLD", open_qty=20, available=20, customer="MAJOR")],
            customer_policies={
                "MAJOR": {
                    "account_type": "major",
                    "accumulate": True,
                    "target_guns": 42,
                    "mix_orders": False,
                }
            },
        )
        row = decision(result, "SO-OLD")
        self.assertEqual((row["decision"], row["reason_code"]), ("RELEASE", "complete"))

    def test_default_standard_policy_releases_one_ship_to_batch_at_cutoff(self):
        rows = [
            candidate("SO-1", part="GUN-A", open_qty=4, available=4, customer="STORE", SHIP_TO_ID="MAIN"),
            candidate("SO-2", part="GUN-B", open_qty=2, available=2, customer="STORE", SHIP_TO_ID="MAIN"),
        ]
        policy = {
            "DEFAULT": {
                "account_type": "standard",
                "accumulate": True,
                "mix_orders": True,
                "release_cadence": "daily",
                "daily_release_time": "14:00",
                "max_hold_days": 7,
            }
        }
        before = self.build(
            rows,
            customer_policies=policy,
            local_now=datetime(2026, 8, 27, 13, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(before["summary"]["accumulating"], 2)
        after = self.build(
            rows,
            customer_policies=policy,
            local_now=datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(after["summary"]["release"], 2)
        self.assertTrue(all(row["reason_code"] == "daily_customer_batch" for row in after["decisions"]))

    def test_ship_to_shipped_today_accumulates_until_tomorrow(self):
        result = self.build(
            [candidate("SO-1", open_qty=2, available=2, customer="STORE", SHIP_TO_ID="MAIN")],
            customer_policies={
                "DEFAULT": {
                    "account_type": "standard",
                    "accumulate": True,
                    "mix_orders": True,
                    "release_cadence": "daily",
                    "daily_release_time": "14:00",
                    "ship_to_cooldown_days": 1,
                }
            },
            ship_to_history=[{
                "CUSTOMER_ID": "STORE",
                "SHIP_TO_ID": "MAIN",
                "LAST_SHIPPED_DATE": TODAY,
            }],
            local_now=datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
        )
        row = decision(result, "SO-1")
        self.assertEqual((row["decision"], row["reason_code"]), (
            "ACCUMULATING", "ship_to_cooldown"
        ))
        self.assertEqual(row["last_ship_to_shipment_date"], "2026-08-27")
        self.assertEqual(row["next_ship_to_eligible_date"], "2026-08-28")
        self.assertEqual(row["next_release_date"], "2026-08-28")
        self.assertEqual(result["summary"]["ship_to_cooldown"], 1)

    def test_three_day_cooldown_blocks_only_the_matching_ship_to(self):
        rows = [
            candidate("SO-MAIN", part="GUN-A", customer="STORE", SHIP_TO_ID="MAIN"),
            candidate("SO-ALT", part="GUN-B", customer="STORE", SHIP_TO_ID="ALT"),
        ]
        result = self.build(
            rows,
            customer_policies={
                "DEFAULT": {
                    "account_type": "standard",
                    "accumulate": True,
                    "mix_orders": True,
                    "release_cadence": "daily",
                    "daily_release_time": "14:00",
                    "ship_to_cooldown_days": 3,
                }
            },
            ship_to_history=[{
                "CUSTOMER_ID": "STORE",
                "SHIP_TO_ID": "MAIN",
                "LAST_SHIPPED_DATE": TODAY,
            }],
            local_now=datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(decision(result, "SO-MAIN")["next_release_date"], "2026-08-30")
        self.assertEqual(decision(result, "SO-MAIN")["reason_code"], "ship_to_cooldown")
        self.assertEqual(decision(result, "SO-ALT")["reason_code"], "daily_customer_batch")

    def test_approved_exception_overrides_ship_to_cooldown(self):
        result = self.build(
            [candidate("SO-1", customer="STORE", SHIP_TO_ID="MAIN")],
            customer_policies={
                "DEFAULT": {"accumulate": True, "ship_to_cooldown_days": 3}
            },
            ship_to_history=[{
                "CUSTOMER_ID": "STORE",
                "SHIP_TO_ID": "MAIN",
                "LAST_SHIPPED_DATE": TODAY,
            }],
            exceptions=[{
                "cust_order_id": "SO-1",
                "expires_at": TODAY + timedelta(days=1),
                "revoked_at": None,
            }],
        )
        self.assertEqual(decision(result, "SO-1")["reason_code"], "approved_exception")

    def test_seven_day_old_reservation_releases_available_remainder(self):
        started = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        result = self.build(
            [candidate("SO-MAJOR", open_qty=100, available=1, customer="MAJOR")],
            customer_policies={
                "MAJOR": {
                    "account_type": "major",
                    "accumulate": True,
                    "target_guns": 42,
                    "mix_orders": False,
                    "max_hold_days": 7,
                }
            },
            serial_inventory=[{"SERIAL_NO": "SER-1", "PART_ID": "GUN-A"}],
            reservations=[{
                "serial_no": "SER-1",
                "part_id": "GUN-A",
                "cust_order_id": "SO-MAJOR",
                "customer_id": "MAJOR",
                "status": "active",
                "first_assigned_at": started.isoformat(),
                "accumulation_started_at": started.isoformat(),
            }],
            local_now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )
        row = decision(result, "SO-MAJOR")
        self.assertEqual((row["decision"], row["reason_code"]), ("RELEASE", "maximum_hold_reached"))
        self.assertEqual(row["reservation_age_days"], 7)

    def test_sticky_serial_reservation_prevents_new_urgent_order_cutting_in(self):
        started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        result = self.build(
            [
                candidate("SO-URGENT", available=1, customer="OTHER", promise=TODAY),
                candidate("SO-MAJOR", open_qty=2, available=1, customer="MAJOR", promise=date(2026, 9, 2)),
            ],
            customer_policies={"MAJOR": {"account_type": "major", "accumulate": True, "target_guns": 42, "mix_orders": False}},
            serial_inventory=[{"SERIAL_NO": "SER-1", "PART_ID": "GUN-A"}],
            reservations=[{
                "serial_no": "SER-1",
                "part_id": "GUN-A",
                "cust_order_id": "SO-MAJOR",
                "customer_id": "MAJOR",
                "status": "active",
                "first_assigned_at": started.isoformat(),
                "accumulation_started_at": started.isoformat(),
            }],
            local_now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(decision(result, "SO-URGENT")["reason_code"], "no_supply")
        self.assertEqual(decision(result, "SO-MAJOR")["protected_guns"], 1)

    def test_new_serials_are_attached_to_protected_order(self):
        result = self.build(
            [candidate("SO-MAJOR", open_qty=10, available=2, customer="MAJOR")],
            customer_policies={"MAJOR": {"account_type": "major", "accumulate": True, "target_guns": 42, "mix_orders": False}},
            serial_inventory=[
                {"SERIAL_NO": "SER-1", "PART_ID": "GUN-A", "LOCATION_ID": "R01-A"},
                {"SERIAL_NO": "SER-2", "PART_ID": "GUN-A", "LOCATION_ID": "R01-A"},
            ],
        )
        row = decision(result, "SO-MAJOR")
        self.assertEqual(row["decision"], "ACCUMULATING")
        self.assertEqual({item["serial_no"] for item in row["serial_assignments"]}, {"SER-1", "SER-2"})
        self.assertEqual(result["summary"]["tracked_serials"], 2)

    def test_ship_to_history_row_without_customer_is_ignored(self):
        for bad_customer in (None, "", float("nan")):
            result = self.build(
                [candidate("SO-1", open_qty=1, available=1)],
                ship_to_history=[{
                    "CUSTOMER_ID": bad_customer,
                    "SHIP_TO_ID": "MAIN",
                    "LAST_SHIPPED_DATE": TODAY,
                }],
            )
            self.assertEqual(decision(result, "SO-1")["decision"], "RELEASE")

    def test_min_cooldown_floor_applies_without_customer_policy(self):
        history = [{
            "CUSTOMER_ID": "CUST",
            "SHIP_TO_ID": "DEFAULT",
            "LAST_SHIPPED_DATE": TODAY,
        }]
        unprotected = self.build(
            [candidate("SO-1", open_qty=1, available=1)], ship_to_history=history
        )
        self.assertEqual(decision(unprotected, "SO-1")["decision"], "RELEASE")
        floored = self.build(
            [candidate("SO-1", open_qty=1, available=1)],
            ship_to_history=history,
            min_ship_to_cooldown_days=1,
        )
        row = decision(floored, "SO-1")
        self.assertEqual((row["decision"], row["reason_code"]), ("HOLD", "ship_to_cooldown"))
        self.assertEqual(row["next_ship_to_eligible_date"], "2026-08-28")
        self.assertEqual(floored["summary"]["ship_to_cooldown_holds"], 1)

    def test_min_cooldown_floor_does_not_hold_unshipped_destinations(self):
        result = self.build(
            [candidate("SO-1", open_qty=1, available=1)],
            ship_to_history=[],
            min_ship_to_cooldown_days=1,
        )
        self.assertEqual(decision(result, "SO-1")["decision"], "RELEASE")

    def test_locally_released_ship_to_blocks_second_same_day_release(self):
        # A picklist run logs its releases; the merged history row (SOURCE picklist)
        # must trigger the cooldown exactly like an ERP shipment.
        result = self.build(
            [candidate("SO-2", open_qty=1, available=1, customer="STORE", SHIP_TO_ID="MAIN")],
            ship_to_history=[{
                "CUSTOMER_ID": "STORE",
                "SHIP_TO_ID": "MAIN",
                "LAST_SHIPPED_DATE": TODAY.isoformat(),
                "SOURCE": "picklist",
            }],
            min_ship_to_cooldown_days=1,
        )
        row = decision(result, "SO-2")
        self.assertEqual((row["decision"], row["reason_code"]), ("HOLD", "ship_to_cooldown"))

    def test_default_policy_does_not_hold_ordinary_customers(self):
        # A DEFAULT entry (or an empty per-customer stub) must not flip the legacy
        # batching branch: a plain complete order still releases as complete.
        result = self.build(
            [candidate("SO-1", open_qty=2, available=2, customer="PLAIN")],
            customer_policies={
                "DEFAULT": {"ship_to_cooldown_days": 1},
                "PLAIN": {},
            },
        )
        row = decision(result, "SO-1")
        self.assertEqual((row["decision"], row["reason_code"]), ("RELEASE", "complete"))

    def test_cooldown_labels_share_canonical_format(self):
        history = [{
            "CUSTOMER_ID": "STORE",
            "SHIP_TO_ID": "MAIN",
            "LAST_SHIPPED_DATE": TODAY,
        }]
        hold = self.build(
            [candidate("SO-H", customer="STORE", SHIP_TO_ID="MAIN")],
            customer_policies={"DEFAULT": {"ship_to_cooldown_days": 2}},
            ship_to_history=history,
        )
        accumulating = self.build(
            [candidate("SO-A", open_qty=2, available=1, customer="STORE", SHIP_TO_ID="MAIN")],
            customer_policies={
                "DEFAULT": {"accumulate": True, "ship_to_cooldown_days": 2}
            },
            ship_to_history=history,
        )
        hold_label = decision(hold, "SO-H")["label"]
        acc_label = decision(accumulating, "SO-A")["label"]
        suffix = "ship-to last shipped 2026-08-27; "
        self.assertEqual(hold_label, f"HOLD - {suffix}eligible 2026-08-29")
        self.assertEqual(acc_label, f"ACCUMULATING - {suffix}protected until 2026-08-29")
        self.assertEqual(accumulating["summary"]["ship_to_cooldown_accumulating"], 1)

    def test_manual_hold_never_releases_or_reserves_supply(self):
        holds = [
            {"cust_order_id": "so-held", "hold_kind": "marketing", "expires_at": "2026-09-05T00:00:00"},
            {"cust_order_id": "SO-RELEASED", "hold_kind": "vip", "expires_at": "2026-09-05", "released_at": "2026-08-20T10:00:00"},
            {"cust_order_id": "SO-EXPIRED", "hold_kind": "vip", "expires_at": "2026-08-01"},
        ]
        result = self.build(
            [
                candidate("SO-HELD", part="GUN-X", open_qty=1, available=1),
                candidate("SO-NEXT", part="GUN-X", open_qty=1, available=1),
                candidate("SO-RELEASED", part="GUN-Y"),
                candidate("SO-EXPIRED", part="GUN-Z"),
            ],
            manual_holds=holds,
        )
        held = decision(result, "SO-HELD")
        self.assertEqual((held["decision"], held["reason_code"]), ("HOLD", "manual_hold"))
        self.assertEqual(held["label"], "HOLD - manual marketing hold until 2026-09-05")
        # The single GUN-X goes to the next order because a held order never reserves supply.
        self.assertEqual(decision(result, "SO-NEXT")["decision"], "RELEASE")
        self.assertEqual(decision(result, "SO-RELEASED")["decision"], "RELEASE")
        self.assertEqual(decision(result, "SO-EXPIRED")["decision"], "RELEASE")

    def test_manual_hold_beats_approved_exception(self):
        result = self.build(
            [candidate("SO-1")],
            exceptions=[{"cust_order_id": "SO-1", "expires_at": "2026-09-30", "reason": "ship request"}],
            manual_holds=[{"cust_order_id": "SO-1", "hold_kind": "rework", "expires_at": "2026-09-30"}],
        )
        self.assertEqual(decision(result, "SO-1")["reason_code"], "manual_hold")

    def test_all_emitted_reason_codes_are_registered(self):
        scenarios = [
            self.build([candidate("SO-1", open_qty=2, available=2)]),
            self.build([candidate("SO-2", open_qty=2, available=1)]),
            self.build([candidate("SO-3", ORDER_RELEASED=0)]),
            self.build([candidate("SO-M")], manual_holds=[{"cust_order_id": "SO-M", "hold_kind": "vip", "expires_at": "2026-12-31"}]),
            self.build(
                [candidate("SO-4", open_qty=3, available=2, customer="BIG")],
                customer_policies={"BIG": {"accumulate": True}},
            ),
            self.build(
                [candidate("SO-5", customer="STORE", SHIP_TO_ID="MAIN")],
                customer_policies={"DEFAULT": {"ship_to_cooldown_days": 2}},
                ship_to_history=[{
                    "CUSTOMER_ID": "STORE",
                    "SHIP_TO_ID": "MAIN",
                    "LAST_SHIPPED_DATE": TODAY,
                }],
            ),
            self.build(
                [candidate("SO-6", open_qty=1, available=1, customer="BATCH")],
                customer_policies={"BATCH": {"min_guns": 5}},
            ),
        ]
        for result in scenarios:
            for row in result["decisions"]:
                self.assertIn(row["reason_code"], release_gate.REASON_CODES)


if __name__ == "__main__":
    unittest.main()
