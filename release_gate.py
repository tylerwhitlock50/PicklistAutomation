"""Deterministic order release-gating rules.

The gate decides which complete or time-critical orders are eligible for the existing
picklist allocation. It never writes an ERP order status. In advisory mode its decisions
are displayed only; in enforced mode the caller injects the released order set into the
picklist query before allocation so held demand cannot consume supply. Customer policies
may also protect shelf inventory in an ACCUMULATING state until a configured release
condition is met.
"""

from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional


EPSILON = 1e-9


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:  # noqa: BLE001
        return False


def _get(row: dict, *names: str) -> Any:
    for name in names:
        if name in row:
            return row.get(name)
    return None


def _text(value: Any) -> Optional[str]:
    if _is_missing(value):
        return None
    result = str(value).strip()
    return result or None


def _num(value: Any) -> float:
    if _is_missing(value):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any, default: bool = False) -> bool:
    if _is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_date(value: Any) -> Optional[date]:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _round_qty(value: float) -> int | float:
    rounded = round(value)
    return int(rounded) if abs(value - rounded) <= EPSILON else round(value, 2)


def _next_weekday(today: date, weekday: int) -> date:
    days = (weekday - today.weekday()) % 7
    return today + timedelta(days=days)


def _normalize_candidates(rows: Iterable[dict]) -> tuple[dict[str, dict], dict[str, float]]:
    orders: dict[str, dict] = {}
    supply: dict[str, float] = {}
    seen_lines: set[tuple[str, str]] = set()

    for row in rows:
        order_id = _text(_get(row, "CUST_ORDER_ID", "cust_order_id", "order_id"))
        line_no = _text(_get(row, "LINE_NO", "line_no", "CUST_ORDER_LINE_NO"))
        part_id = _text(_get(row, "PART_ID", "part_id"))
        if not order_id or line_no is None or not part_id:
            continue
        key = (order_id.upper(), line_no)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        available = max(0.0, _num(_get(row, "AVAILABLE_QTY", "available_qty")))
        supply[part_id.upper()] = max(supply.get(part_id.upper(), 0.0), available)

        open_qty = max(0.0, _num(_get(row, "OPEN_QTY", "open_qty")))
        if open_qty <= EPSILON:
            continue
        order = orders.setdefault(
            order_id.upper(),
            {
                "order_id": order_id,
                "customer_id": _text(_get(row, "CUSTOMER_ID", "customer_id")),
                "customer_name": _text(_get(row, "CUSTOMER_NAME", "customer_name")),
                "order_date": _as_date(_get(row, "ORDER_DATE", "order_date")),
                "promise_del": _as_date(
                    _get(row, "PROMISE_DEL_DATE", "promise_del_date")
                ),
                "promise_ship": _as_date(
                    _get(row, "PROMISE_SHIP_DATE", "promise_ship_date")
                ),
                "desired_ship": _as_date(
                    _get(row, "DESIRED_SHIP_DATE", "desired_ship_date")
                ),
                "lines": [],
                "block_reasons": set(),
                "open_qty": 0.0,
                "open_guns": 0.0,
            },
        )
        item_type = (_text(_get(row, "ITEM_TYPE", "item_type")) or "guns").lower()
        line = {
            "line_no": line_no,
            "part_id": part_id.upper(),
            "open_qty": open_qty,
            "item_type": item_type,
        }
        order["lines"].append(line)
        order["open_qty"] += open_qty
        if item_type == "guns":
            order["open_guns"] += open_qty

        checks = (
            ("order not released", _bool(_get(row, "ORDER_RELEASED", "order_released"), True)),
            ("line not available", _bool(_get(row, "LINE_AVAILABLE", "line_available"), True)),
            ("credit hold", _bool(_get(row, "CREDIT_APPROVED", "credit_approved"), True)),
            ("missing ship-to", _bool(_get(row, "SHIP_TO_PRESENT", "ship_to_present"), True)),
        )
        for reason, passed in checks:
            if not passed:
                order["block_reasons"].add(reason)
        if _bool(_get(row, "IS_RMA", "is_rma")):
            order["block_reasons"].add("RMA")
        if _bool(_get(row, "IS_INTERNATIONAL", "is_international")):
            order["block_reasons"].add("international")
        if _bool(_get(row, "IS_EMPLOYEE", "is_employee")):
            order["block_reasons"].add("employee")
        if _bool(_get(row, "EXCLUDED_CUSTOMER", "excluded_customer")):
            order["block_reasons"].add("excluded customer")

    for order in orders.values():
        order["lines"].sort(key=lambda row: (row["part_id"], row["line_no"]))
    return orders, supply


def _priority(order: dict) -> tuple:
    return (
        order.get("promise_del") is None,
        order.get("promise_del") or date.max,
        order.get("promise_ship") is None,
        order.get("promise_ship") or date.max,
        order.get("desired_ship") or date.max,
        order.get("order_date") or date.max,
        order["order_id"],
    )


def _customer_key(order: dict) -> str:
    order_key = order["order_id"].upper()
    return (order.get("customer_id") or f"ORDER:{order_key}").upper()


def _is_at_risk(order: dict, today: date, due_override_days: int) -> bool:
    return bool(
        order.get("promise_ship")
        and order["promise_ship"] <= today + timedelta(days=due_override_days)
    )


def _policy_values(policy: dict) -> tuple[bool, float, Optional[int]]:
    accumulate = _bool(policy.get("accumulate"))
    min_guns = max(0.0, _num(policy.get("min_guns")))
    sweep_weekday_raw = policy.get("sweep_weekday")
    sweep_weekday = None
    if sweep_weekday_raw not in (None, ""):
        try:
            sweep_weekday = int(sweep_weekday_raw)
        except (TypeError, ValueError):
            sweep_weekday = None
        if sweep_weekday not in range(7):
            sweep_weekday = None
    return accumulate, min_guns, sweep_weekday


def _allocate(order: dict, remaining: dict[str, float], require_complete: bool) -> dict:
    trial = deepcopy(remaining)
    allocations = []
    for line in order["lines"]:
        available = max(0.0, trial.get(line["part_id"], 0.0))
        qty = min(line["open_qty"], available)
        if require_complete and qty < line["open_qty"] - EPSILON:
            return {
                "complete": False,
                "ready_qty": 0.0,
                "ready_guns": 0.0,
                "allocations": [],
                "remaining": remaining,
            }
        if qty > EPSILON:
            trial[line["part_id"]] = available - qty
            allocations.append({**line, "qty": qty})
    ready_qty = sum(row["qty"] for row in allocations)
    ready_guns = sum(row["qty"] for row in allocations if row["item_type"] == "guns")
    return {
        "complete": ready_qty >= order["open_qty"] - EPSILON,
        "ready_qty": ready_qty,
        "ready_guns": ready_guns,
        "allocations": allocations,
        "remaining": trial,
    }


def _active_exception_map(exceptions: Iterable[dict], today: date) -> dict[str, dict]:
    result = {}
    for row in exceptions:
        order_id = _text(_get(row, "cust_order_id", "CUST_ORDER_ID", "order_id"))
        expires = _as_date(_get(row, "expires_at", "EXPIRES_AT"))
        revoked = _get(row, "revoked_at", "REVOKED_AT")
        if not order_id or not _is_missing(revoked):
            continue
        if expires is not None and expires < today:
            continue
        result[order_id.upper()] = row
    return result


def evaluate_release_gate(
    rows: Iterable[dict],
    *,
    today: date,
    mode: str = "advisory",
    due_override_days: int = 1,
    customer_policies: Optional[dict[str, dict]] = None,
    exceptions: Iterable[dict] = (),
    policy_version: int = 1,
    evaluated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return one auditable decision per order, including protected accumulation."""
    normalized_mode = (mode or "advisory").strip().lower()
    if normalized_mode not in {"off", "advisory", "enforced"}:
        raise ValueError("mode must be off, advisory, or enforced")
    if due_override_days < 0:
        raise ValueError("due_override_days must be zero or greater")

    orders, supply = _normalize_candidates(rows)
    remaining = deepcopy(supply)
    policies = {str(key).upper(): value for key, value in (customer_policies or {}).items()}
    active_exceptions = _active_exception_map(exceptions, today)
    decisions: dict[str, dict] = {}

    def record(
        order: dict,
        decision: str,
        code: str,
        label: str,
        allocation: dict,
        next_release_date: Optional[date] = None,
        *,
        target_guns: Optional[float] = None,
        group_ready_guns: Optional[float] = None,
        release_condition: Optional[str] = None,
        commitment_at_risk: bool = False,
    ) -> None:
        ready_qty = allocation.get("ready_qty", 0.0)
        accumulating = decision == "ACCUMULATING"
        decisions[order["order_id"].upper()] = {
            "order_id": order["order_id"],
            "customer_id": order["customer_id"],
            "customer_name": order["customer_name"],
            "decision": decision,
            "reason_code": code,
            "label": label,
            "open_qty": _round_qty(order["open_qty"]),
            "ready_qty": _round_qty(ready_qty),
            "missing_qty": _round_qty(max(0.0, order["open_qty"] - ready_qty)),
            "open_guns": _round_qty(order["open_guns"]),
            "ready_guns": _round_qty(allocation.get("ready_guns", 0.0)),
            "protected_qty": _round_qty(ready_qty if accumulating else 0.0),
            "protected_guns": _round_qty(
                allocation.get("ready_guns", 0.0) if accumulating else 0.0
            ),
            "target_guns": (
                _round_qty(target_guns) if target_guns is not None else None
            ),
            "group_ready_guns": (
                _round_qty(group_ready_guns)
                if group_ready_guns is not None
                else None
            ),
            "release_condition": release_condition,
            "commitment_at_risk": bool(commitment_at_risk),
            "promise_ship": order["promise_ship"].isoformat() if order["promise_ship"] else None,
            "promise_del": order["promise_del"].isoformat() if order["promise_del"] else None,
            "next_release_date": next_release_date.isoformat() if next_release_date else None,
            "allocations": [
                {
                    "line_no": row["line_no"],
                    "part_id": row["part_id"],
                    "qty": _round_qty(row["qty"]),
                    "item_type": row["item_type"],
                }
                for row in allocation.get("allocations", [])
            ],
        }

    ordered = sorted(orders.values(), key=_priority)

    # Hard business blocks never reserve supply.
    for order in ordered:
        if not order["block_reasons"]:
            continue
        potential = _allocate(order, supply, require_complete=False)
        reasons = ", ".join(sorted(order["block_reasons"]))
        record(order, "BLOCKED", "hard_block", f"BLOCKED - {reasons}", potential)

    # Exceptions and time-critical commitments get first access. Partial release is
    # intentional here and visible in the reason code. An accumulating customer's own
    # at-risk order stays protected for management review; an explicit exception can
    # still release it.
    for order in ordered:
        key = order["order_id"].upper()
        if key in decisions:
            continue
        exception = active_exceptions.get(key)
        at_risk = _is_at_risk(order, today, due_override_days)
        policy = policies.get(_customer_key(order), {})
        accumulates = _bool(policy.get("accumulate")) and order["open_guns"] > EPSILON
        if not exception and (not at_risk or accumulates):
            continue
        allocation = _allocate(order, remaining, require_complete=False)
        if allocation["ready_qty"] <= EPSILON:
            record(order, "HOLD", "no_supply", "HOLD - no allocatable supply", allocation)
            continue
        remaining = allocation["remaining"]
        if exception:
            record(
                order,
                "RELEASE",
                "approved_exception",
                "SHIP NOW - approved exception",
                allocation,
            )
        else:
            record(
                order,
                "RELEASE",
                "commitment_at_risk",
                "SHIP NOW - commitment at risk",
                allocation,
            )

    # Accumulating customers receive a logical reservation while inventory remains in
    # its shelf location. Protected quantities reduce what later customer groups can
    # release, but the order itself does not reach the picklist until its configured
    # target/sweep condition is met, it is complete under a completion-only policy, or
    # it receives an exception.
    accumulating_by_customer: dict[str, list[dict]] = {}
    for order in ordered:
        key = order["order_id"].upper()
        if key in decisions or order["open_guns"] <= EPSILON:
            continue
        customer_key = _customer_key(order)
        policy = policies.get(customer_key, {})
        if _bool(policy.get("accumulate")):
            accumulating_by_customer.setdefault(customer_key, []).append(order)

    accumulating_groups = sorted(
        accumulating_by_customer.items(),
        key=lambda item: _priority(sorted(item[1], key=_priority)[0]),
    )
    for customer_key, customer_orders in accumulating_groups:
        policy = policies.get(customer_key, {})
        _, min_guns, sweep_weekday = _policy_values(policy)
        sweep_day = sweep_weekday is not None and today.weekday() == sweep_weekday
        next_sweep = None
        if sweep_weekday is not None:
            sweep_search_date = today + timedelta(days=1) if sweep_day else today
            next_sweep = _next_weekday(sweep_search_date, sweep_weekday)

        trial_remaining = deepcopy(remaining)
        accumulated: list[tuple[dict, dict]] = []
        for order in sorted(customer_orders, key=_priority):
            allocation = _allocate(order, trial_remaining, require_complete=False)
            trial_remaining = allocation["remaining"]
            accumulated.append((order, allocation))

        group_ready_guns = sum(
            allocation["ready_guns"] for _, allocation in accumulated
        )
        threshold_reached = (
            min_guns > EPSILON and group_ready_guns >= min_guns - EPSILON
        )
        group_releases = threshold_reached or sweep_day
        unconstrained = min_guns <= EPSILON and sweep_weekday is None
        remaining = trial_remaining

        if min_guns > EPSILON and sweep_weekday is not None:
            release_condition = (
                f"Release at {_round_qty(min_guns)} protected customer guns "
                f"or the {calendar.day_name[sweep_weekday]} sweep"
            )
        elif min_guns > EPSILON:
            release_condition = (
                f"Release at {_round_qty(min_guns)} protected customer guns"
            )
        elif sweep_weekday is not None:
            release_condition = (
                f"Release on the {calendar.day_name[sweep_weekday]} sweep"
            )
        else:
            release_condition = "Release when the order is complete"

        for order, allocation in accumulated:
            at_risk = _is_at_risk(order, today, due_override_days)
            if group_releases and allocation["ready_qty"] > EPSILON:
                if threshold_reached:
                    code = "accumulation_target_reached"
                    label = (
                        "SHIP NOW - accumulation target reached "
                        f"({_round_qty(group_ready_guns)} guns)"
                    )
                else:
                    code = "scheduled_customer_release"
                    label = "SHIP NOW - scheduled customer release"
                record(
                    order,
                    "RELEASE",
                    code,
                    label,
                    allocation,
                    target_guns=min_guns if min_guns > EPSILON else None,
                    group_ready_guns=group_ready_guns,
                    release_condition=release_condition,
                    commitment_at_risk=at_risk,
                )
                continue

            if unconstrained and allocation["complete"]:
                record(
                    order,
                    "RELEASE",
                    "complete",
                    "SHIP NOW - complete",
                    allocation,
                    release_condition=release_condition,
                    commitment_at_risk=at_risk,
                )
                continue

            if allocation["ready_qty"] <= EPSILON:
                label = "ACCUMULATING - waiting for allocatable supply"
            elif min_guns > EPSILON:
                label = (
                    "ACCUMULATING - "
                    f"{_round_qty(group_ready_guns)}/{_round_qty(min_guns)} "
                    "customer guns protected"
                )
            elif next_sweep:
                label = (
                    f"ACCUMULATING - {_round_qty(allocation['ready_guns'])} guns "
                    f"protected until {next_sweep.isoformat()}"
                )
            else:
                label = (
                    "ACCUMULATING - "
                    f"{_round_qty(allocation['ready_guns'])}/"
                    f"{_round_qty(order['open_guns'])} guns protected"
                )
            if at_risk:
                label += "; commitment at risk - review exception"
                code = "accumulating_commitment_at_risk"
            elif min_guns > EPSILON or sweep_weekday is not None:
                code = "accumulating_for_customer_release"
            else:
                code = "accumulating_for_completion"
            record(
                order,
                "ACCUMULATING",
                code,
                label,
                allocation,
                next_sweep,
                target_guns=(
                    min_guns if min_guns > EPSILON else order["open_guns"]
                ),
                group_ready_guns=group_ready_guns,
                release_condition=release_condition,
                commitment_at_risk=at_risk,
            )

    # Process customer groups in priority order. A custom customer policy may hold a
    # complete set until its minimum gun batch or sweep weekday is reached. Held groups
    # do not reserve supply, so lower-priority releasable orders can still flow.
    by_customer: dict[str, list[dict]] = {}
    for order in ordered:
        key = order["order_id"].upper()
        if key in decisions:
            continue
        customer_key = _customer_key(order)
        by_customer.setdefault(customer_key, []).append(order)

    customer_groups = sorted(
        by_customer.items(),
        key=lambda item: _priority(sorted(item[1], key=_priority)[0]),
    )
    for customer_key, customer_orders in customer_groups:
        policy = policies.get(customer_key, {})
        _, min_guns, sweep_weekday = _policy_values(policy)

        trial_remaining = deepcopy(remaining)
        complete_candidates: list[tuple[dict, dict]] = []
        incomplete_orders: list[tuple[dict, dict]] = []
        for order in sorted(customer_orders, key=_priority):
            allocation = _allocate(order, trial_remaining, require_complete=True)
            if allocation["complete"]:
                trial_remaining = allocation["remaining"]
                complete_candidates.append((order, allocation))
            else:
                incomplete_orders.append(
                    (order, _allocate(order, remaining, require_complete=False))
                )

        ready_guns = sum(allocation["ready_guns"] for _, allocation in complete_candidates)
        has_custom_policy = bool(policy)
        sweep_day = sweep_weekday is not None and today.weekday() == sweep_weekday
        contains_only_non_gun_orders = bool(complete_candidates) and all(
            order["open_guns"] <= EPSILON for order, _ in complete_candidates
        )
        group_releases = (
            not has_custom_policy
            or contains_only_non_gun_orders
            or sweep_day
            or (min_guns > EPSILON and ready_guns >= min_guns - EPSILON)
            or (min_guns <= EPSILON and sweep_weekday is None)
        )

        if group_releases:
            remaining = trial_remaining
            for order, allocation in complete_candidates:
                if has_custom_policy and sweep_day and ready_guns < min_guns - EPSILON:
                    record(
                        order,
                        "RELEASE",
                        "scheduled_customer_release",
                        "SHIP NOW - scheduled customer release",
                        allocation,
                    )
                else:
                    record(order, "RELEASE", "complete", "SHIP NOW - complete", allocation)
        else:
            next_release = _next_weekday(today, sweep_weekday) if sweep_weekday is not None else None
            for order, allocation in complete_candidates:
                if next_release:
                    label = f"HOLD - scheduled release on {next_release.isoformat()}"
                    code = "scheduled_hold"
                else:
                    label = "HOLD - below customer batch threshold"
                    code = "below_batch_threshold"
                record(order, "HOLD", code, label, allocation, next_release)

        for order, potential in incomplete_orders:
            record(
                order,
                "HOLD",
                "waiting_for_completion",
                "HOLD - waiting for order completion",
                potential,
            )

    decision_rows = sorted(decisions.values(), key=lambda row: (
        {"RELEASE": 0, "ACCUMULATING": 1, "HOLD": 2, "BLOCKED": 3}.get(
            row["decision"], 9
        ),
        row["promise_ship"] or "9999-12-31",
        row["order_id"],
    ))
    summary = {
        "orders": len(decision_rows),
        "release": sum(row["decision"] == "RELEASE" for row in decision_rows),
        "accumulating": sum(
            row["decision"] == "ACCUMULATING" for row in decision_rows
        ),
        "hold": sum(row["decision"] == "HOLD" for row in decision_rows),
        "blocked": sum(row["decision"] == "BLOCKED" for row in decision_rows),
        "release_units": _round_qty(sum(float(row["ready_qty"]) for row in decision_rows if row["decision"] == "RELEASE")),
        "protected_units": _round_qty(
            sum(
                float(row["protected_qty"])
                for row in decision_rows
                if row["decision"] == "ACCUMULATING"
            )
        ),
        "protected_guns": _round_qty(
            sum(
                float(row["protected_guns"])
                for row in decision_rows
                if row["decision"] == "ACCUMULATING"
            )
        ),
        "held_ready_units": _round_qty(sum(float(row["ready_qty"]) for row in decision_rows if row["decision"] == "HOLD")),
    }
    protected_supply: dict[str, float] = {}
    for row in decision_rows:
        if row["decision"] != "ACCUMULATING":
            continue
        for allocation in row["allocations"]:
            part_id = allocation["part_id"]
            protected_supply[part_id] = (
                protected_supply.get(part_id, 0.0) + float(allocation["qty"])
            )
    evaluated = evaluated_at or datetime.now(timezone.utc)
    return {
        "mode": normalized_mode,
        "policy_version": int(policy_version),
        "evaluated_at": evaluated.isoformat(),
        "due_override_days": due_override_days,
        "summary": summary,
        "released_orders": [row["order_id"] for row in decision_rows if row["decision"] == "RELEASE"],
        "decisions": decision_rows,
        "supply": {part: _round_qty(qty) for part, qty in sorted(supply.items())},
        "protected_supply": {
            part: _round_qty(qty) for part, qty in sorted(protected_supply.items())
        },
        "remaining_supply": {
            part: _round_qty(qty) for part, qty in sorted(remaining.items()) if qty > EPSILON
        },
    }


def filter_released_rows(rows: Iterable[dict], payload: dict) -> list[dict]:
    """Filter already-shaped rows for non-SQL callers and tests.

    Production picklist SQL applies the same released set before allocation; this helper is
    useful for exports, tests, and any future caller that already has unallocated rows.
    """
    released = {str(value).strip().upper() for value in payload.get("released_orders", [])}
    result = []
    for row in rows:
        order_id = _text(_get(row, "Cust Order ID", "CUST_ORDER_ID", "cust_order_id"))
        if order_id and order_id.upper() in released:
            result.append(dict(row))
    return result
