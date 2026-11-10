"""Stage inventory: daily flow, why-staged attribution, Today panel route."""
import datetime as dt
from unittest.mock import patch

import pandas as pd

from picklist.app import app
from picklist.domain import stage_inventory as si
from picklist.services import shipping_service

TUE = dt.date(2026, 11, 10)


def _hold(code, owner="sales", blocking=True, label=None):
    return {"reason_code": code, "owner_team": owner, "blocking": blocking, "label": label or code}


def _order(order_id, part, open_qty, *, due="2026-11-10", holds=(), gate=None, closed=False):
    return {
        "order_id": order_id, "customer_name": f"Cust {order_id}", "state": "READY", "due": due,
        "closed": closed, "holds": list(holds), "gate": gate,
        "lines": [{"line_no": "1", "part_id": part, "open_qty": open_qty, "line_status": "A"}],
    }


GATE_RELEASE = {"decision": "RELEASE", "reason_code": "complete", "label": "SHIP NOW - complete"}


# --------------------------------------------------------------------------- flow

def test_flow_nets_arrivals_against_shipments_and_skips_reshuffles():
    rows = [
        {"FLOW_DATE": "2026-11-10", "MOVE_KIND": "ARRIVED", "QTY": 106, "ORDERS": 0},
        {"FLOW_DATE": "2026-11-10", "MOVE_KIND": "RESHUFFLED", "QTY": 29, "ORDERS": 0},
        {"FLOW_DATE": pd.Timestamp("2026-11-09"), "MOVE_KIND": "ARRIVED", "QTY": 57.0, "ORDERS": 0},
        {"FLOW_DATE": dt.date(2026, 11, 9), "MOVE_KIND": "SHIPPED", "QTY": 2, "ORDERS": 2},
        {"FLOW_DATE": "2026-11-09", "MOVE_KIND": "MOVED_OUT", "QTY": 1, "ORDERS": 0},
        {"FLOW_DATE": "2026-11-09", "MOVE_KIND": "SHIPPED_ALL", "QTY": 10, "ORDERS": 4},
        {"FLOW_DATE": "2026-11-09", "MOVE_KIND": "UNSHIPPED", "QTY": 3, "ORDERS": 1},
        {"FLOW_DATE": "2026-11-09", "MOVE_KIND": "REVERSED", "QTY": 1, "ORDERS": 0},
        {"FLOW_DATE": "2026-11-09", "MOVE_KIND": "SOMETHING_NEW", "QTY": 50, "ORDERS": 0},
        {"FLOW_DATE": "2026-10-01", "MOVE_KIND": "SHIPPED", "QTY": 999, "ORDERS": 9},  # outside window
    ]
    flow = si.summarize_flow(rows, today=TUE, days=14)
    assert flow["today"]["arrived"] == 106
    assert flow["today"]["reshuffled"] == 29
    assert flow["today"]["net"] == 106  # bin-to-bin moves are not in or out
    assert flow["previous"]["date"] == "2026-11-09"
    prev = flow["previous"]
    # Stage shipments are read against every gun shipped that day (racks included).
    assert (prev["shipped"], prev["shipped_orders"], prev["shipped_all"], prev["shipped_all_orders"]) == (2, 2, 10, 4)
    # net = arrived - reversed + unshipped - shipped - moved_out; unknown kinds ignored.
    assert prev["net"] == 57 - 1 + 3 - 2 - 1
    assert flow["last_7"]["arrived"] == 163
    assert flow["last_7"]["shipped_all"] == 10
    assert flow["last_7"]["net"] == 106 + 56
    assert all(day["date"] != "2026-10-01" for day in flow["days"])


def test_flow_drops_quiet_weekends_and_monday_looks_back_to_friday():
    monday = dt.date(2026, 11, 9)
    rows = [{"FLOW_DATE": "2026-11-07", "MOVE_KIND": "ARRIVED", "QTY": 3}]  # busy Saturday
    flow = si.summarize_flow(rows, today=monday, days=7)
    dates = [day["date"] for day in flow["days"]]
    assert "2026-11-07" in dates and "2026-11-08" not in dates
    assert flow["previous"]["date"] == "2026-11-06"


# --------------------------------------------------------------------------- bucketing

def test_readiness_blocking_hold_wins_over_gate_and_stock_holds_are_ignored():
    order = _order("SO-1", "P", 1, holds=[_hold("no_supply", "production"), _hold("credit_status_hold", "finance")],
                   gate=GATE_RELEASE)
    assert si.order_bucket(order)[0] == "blocked_finance"
    # Supply holds alone never explain a staged gun: stage is excluded from supply.
    stock_only = _order("SO-2", "P", 1, holds=[_hold("no_supply", "production")], gate=GATE_RELEASE)
    assert si.order_bucket(stock_only) == ("clear", [])


def test_gate_decisions_count_even_when_advisory():
    accumulating = {"decision": "ACCUMULATING", "reason_code": "accumulating_for_daily_batch", "mode": "advisory"}
    held = {"decision": "HOLD", "reason_code": "ship_to_cooldown"}
    gate_no_supply = {"decision": "HOLD", "reason_code": "no_supply", "label": "HOLD - no allocatable supply"}
    blocked = {"decision": "BLOCKED", "reason_code": "hard_block", "label": "BLOCKED - line not available, order not released"}
    assert si.order_bucket(_order("A", "P", 1, gate=accumulating))[0] == "accumulating"
    assert si.order_bucket(_order("B", "P", 1, gate=held))[0] == "gate_hold"
    # The gate stopped at its supply check (stage is invisible to it), so its
    # cooldown / batch policies never ran: timing unknown, never "clear".
    assert si.order_bucket(_order("C", "P", 1, gate=gate_no_supply))[0] == "gate_blind"
    key, reasons = si.order_bucket(_order("D", "P", 1, gate=blocked))
    assert key == "gate_blocked"
    assert [r["label"] for r in reasons] == ["Line not available", "Order not released"]
    horizon = dt.date(2026, 11, 20)
    key, reasons = si.order_bucket(_order("E", "P", 1, due="2026-12-01", gate=None), gate_horizon=horizon)
    assert key == "not_due" and reasons[0]["label"] == "Order due 2026-12-01"
    # Due inside the gate window with no decision (gate off or failed) is not "early".
    assert si.order_bucket(_order("F", "P", 1, due="2026-11-12", gate=None), gate_horizon=horizon)[0] == "no_gate"
    assert si.order_bucket(_order("G", "P", 1, due="2026-12-01", gate=None))[0] == "no_gate"


# --------------------------------------------------------------------------- attribution

def test_releasable_orders_claim_staged_guns_before_blocked_ones():
    orders = [
        _order("SO-BLOCKED", "P1", 5, due="2026-10-01", holds=[_hold("order_not_released")]),
        _order("SO-CLEAR", "P1", 3, due="2026-11-30", gate=GATE_RELEASE),
    ]
    why = si.attribute_staged({"P1": 4}, orders)
    assert why["clear_to_ship"] == 3
    assert {b["key"]: b["qty"] for b in why["buckets"]} == {"clear": 3, "blocked_sales": 1}


def test_unreleasable_orders_fill_oldest_due_first():
    blocked = {"decision": "BLOCKED", "reason_code": "hard_block", "label": "BLOCKED - line not available"}
    orders = [
        _order("EDI-LATER", "P1", 300, due="2026-12-01", gate=None),
        _order("EDI-OVERDUE", "P1", 60, due="2026-10-01", gate=blocked),
    ]
    why = si.attribute_staged({"P1": 93}, orders, gate_horizon=dt.date(2026, 11, 20))
    assert {b["key"]: b["qty"] for b in why["buckets"]} == {"gate_blocked": 60, "not_due": 33}
    assert [o["order_id"] for o in why["orders"]] == ["EDI-LATER", "EDI-OVERDUE"]  # report order is by bucket


def test_every_staged_unit_lands_in_exactly_one_headline_group():
    orders = [
        _order("SO-A", "P1", 2, gate={"decision": "ACCUMULATING", "reason_code": "accumulating_for_daily_batch"}),
        _order("SO-B", "P1", 1, holds=[_hold("rma_excluded", "shipping")]),
        _order("SO-C", "P2", 1, due="2026-12-24"),
        _order("SO-D", "P5", 1, due="2026-11-11", holds=[_hold("credit_status_hold", "finance")]),
        _order("SO-CLOSED", "P3", 9, closed=True, gate=GATE_RELEASE),
    ]
    why = si.attribute_staged({"P1": 5, "P2": 1, "p3": 2, "P4": 0, "P5": 1}, orders,
                              descriptions={"p1": "Rifle"}, gate_horizon=dt.date(2026, 11, 20))
    assert why["total"] == 9
    # RMA / excluded orders are worked off-picklist on purpose: not "stuck".
    assert (why["on_schedule"], why["early"], why["stuck"], why["outside"], why["no_decision"], why["unmatched"]) == (2, 1, 1, 1, 0, 4)
    assert sum(why[group] for group in si.HEADLINE_GROUPS) == why["total"]
    by_part = {p["part_id"]: p for p in why["parts"]}
    assert by_part["P1"]["buckets"] == {"accumulating": 2, "off_picklist": 1, "extra": 2}
    assert by_part["P1"]["description"] == "Rifle"
    assert by_part["P3"]["buckets"] == {"no_order": 2}  # closed orders do not count as demand
    assert "P4" not in by_part
    assert sum(b["qty"] for b in why["buckets"]) == why["total"]


def test_headline_groups_cover_every_bucket_exactly_once():
    keys = [key for group in si.HEADLINE_GROUPS.values() for key in group]
    assert sorted(keys) == sorted(si.BUCKETS)


def test_completion_hold_is_cleared_only_when_stage_covers_the_order():
    waiting = {"decision": "HOLD", "reason_code": "waiting_for_completion", "label": "HOLD - waiting for completion"}
    covered = si.attribute_staged({"P1": 4}, [_order("SO-FULL", "P1", 4, gate=waiting)])
    # Fully staged: the completion hold is the gate's blind spot, but batch
    # thresholds etc. were never checked, so it is not "clear to ship".
    assert {b["key"]: b["qty"] for b in covered["buckets"]} == {"gate_blind": 4}
    assert covered["clear_to_ship"] == 0 and covered["no_decision"] == 4
    partial = si.attribute_staged({"P1": 2}, [_order("SO-PART", "P1", 4, gate=waiting)])
    assert {b["key"]: b["qty"] for b in partial["buckets"]} == {"gate_hold": 2}


def test_off_picklist_is_classified_by_reason_not_owner():
    # Readiness assigns international exclusions to Compliance; they are still
    # worked off-picklist on purpose, not "stuck until a hold is fixed".
    intl = _order("SO-INTL", "P1", 2, holds=[_hold("excluded_class", "compliance"), _hold("order_not_released")])
    assert si.order_bucket(intl)[0] == "off_picklist"
    rma = _order("SO-RMA", "P1", 1, holds=[_hold("rma_excluded", "shipping")])
    assert si.order_bucket(rma)[0] == "off_picklist"
    genuine = _order("SO-COMP", "P1", 1, holds=[_hold("some_compliance_hold", "compliance")])
    assert si.order_bucket(genuine)[0] == "blocked_compliance"
    why = si.attribute_staged({"P1": 3}, [intl, rma])
    assert (why["outside"], why["stuck"]) == (3, 0)


# --------------------------------------------------------------------------- service + routes

def _dwell_rows():
    now = pd.Timestamp("2026-11-10T13:00:00")
    rows = []
    for i, (loc, part) in enumerate([("STAGE1", "P1"), ("STAGE1", "P1"), ("STAGE3", "P2"), ("R04S03", "P1")]):
        rows.append({"WAREHOUSE_ID": "SHIPPING", "LOCATION_ID": loc, "SERIAL_NO": f"S{i}", "PART_ID": part,
                     "PART_DESCRIPTION": f"{part} rifle", "ARRIVED_AT": now - pd.Timedelta(hours=5),
                     "ERP_NOW": now, "dwell_hours": 5.0})
    return pd.DataFrame(rows)


READINESS_PAYLOAD = {
    "never_run": False,
    "evaluated_at": "2026-11-10T19:00:00+00:00",
    "orders": [_order("SO-1", "P1", 1, gate=GATE_RELEASE)],
}


def test_build_stage_inventory_combines_snapshot_flow_and_readiness():
    flow_rows = [{"FLOW_DATE": "2026-11-10", "MOVE_KIND": "ARRIVED", "QTY": 3, "ORDERS": 0, "ERP_TODAY": "2026-11-10"}]
    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=_dwell_rows()), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", return_value=flow_rows), \
         patch("picklist.services.readiness_service.current_payload", return_value=READINESS_PAYLOAD):
        result = shipping_service.build_stage_inventory()
    assert result["on_hand"] == 3  # R04S03 is not a stage bin
    assert result["as_of_display"] == "2026-11-10 13:00"
    assert [row["location"] for row in result["by_location"]] == ["SHIPPING/STAGE1", "SHIPPING/STAGE3"]
    assert result["flow"]["today"]["arrived"] == 3
    assert {b["key"]: b["qty"] for b in result["why"]["buckets"]} == {"clear": 1, "extra": 1, "no_order": 1}


def test_build_stage_inventory_degrades_when_flow_or_readiness_fail():
    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=_dwell_rows()), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", side_effect=RuntimeError("ERP down")), \
         patch("picklist.services.readiness_service.current_payload", return_value={"never_run": True}):
        result = shipping_service.build_stage_inventory()
    assert result["on_hand"] == 3
    assert result["flow"] is None and "ERP down" in result["flow_error"]
    assert result["why"] is None and "not run yet" in result["why_error"]


def _client():
    # Other suites leave stores bound to closed connections; rebind like they do.
    from picklist import db
    from picklist.stores import pick_store, verify_store
    app.config["TESTING"] = True
    db.set_setting("feature_shipping_enabled", "true")
    pick_store.initialize(db.get_sqlite_conn)
    verify_store.initialize(db.get_sqlite_conn)
    return app.test_client()


def test_failed_readiness_run_is_an_error_not_no_open_order():
    failed = {"never_run": False, "evaluated_at": "2026-11-10T19:00:00+00:00", "orders": [], "error": "ERP timeout"}
    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=_dwell_rows()), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", return_value=[]), \
         patch("picklist.services.readiness_service.current_payload", return_value=failed):
        result = shipping_service.build_stage_inventory()
    assert result["why"] is None
    assert "readiness run failed (ERP timeout)" in result["why_error"]


def test_missing_gate_everywhere_warns_and_flow_skipped_when_snapshot_fails():
    no_gate = {**READINESS_PAYLOAD, "orders": [_order("SO-1", "P1", 1, gate=None)]}
    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=_dwell_rows()), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", return_value=[]), \
         patch("picklist.services.readiness_service.current_payload", return_value=no_gate):
        result = shipping_service.build_stage_inventory()
    assert "release-gate decision" in result["why_warning"]
    with patch.object(shipping_service, "_fetch_dwell_raw", side_effect=RuntimeError("ERP down")), \
         patch.object(shipping_service, "_fetch_stage_flow_rows") as flow:
        result = shipping_service.build_stage_inventory()
    assert result["error"] and not flow.called  # do not wait on a second ERP timeout


def test_today_page_loads_stage_panel_asynchronously_and_panel_renders():
    client = _client()
    html = client.get("/work").get_data(as_text=True)
    assert 'id="stage-panel"' in html
    assert 'data-stage-panel-url="/work/stage-panel"' in html

    with patch.object(shipping_service, "_fetch_dwell_raw", return_value=_dwell_rows()), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", return_value=[]), \
         patch("picklist.services.readiness_service.current_payload", return_value=READINESS_PAYLOAD):
        panel = client.get("/work/stage-panel")
        report = client.get("/shipping?view=stage")
    body = panel.get_data(as_text=True)
    assert panel.status_code == 200
    assert '<span class="stage-count">3</span> guns in stage' in body
    assert "1</strong> releasing on schedule" in body
    assert "shipped from stage" in body and "guns shipped company-wide" in body
    assert "2</strong> not needed by an open order" in body
    assert "built-in method" not in body
    assert "as of 2026-11-10 13:00 (ERP clock)" in body
    report_html = report.get_data(as_text=True)
    assert report.status_code == 200
    assert "Why they are still here" in report_html
    assert "/orders/SO-1" in report_html


def test_stage_panel_reports_erp_failure_without_500():
    client = _client()
    with patch.object(shipping_service, "_fetch_dwell_raw", side_effect=RuntimeError("ERP down")), \
         patch.object(shipping_service, "_fetch_stage_flow_rows", side_effect=RuntimeError("ERP down")):
        panel = client.get("/work/stage-panel")
    assert panel.status_code == 200
    assert "Stage counts are unavailable" in panel.get_data(as_text=True)
