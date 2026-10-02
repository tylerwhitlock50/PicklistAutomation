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
from datetime import date, datetime, time as clock_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional


EPSILON = 1e-9

# Every reason_code a decision can carry. Codes are persisted in the audit store
# and shown on the dashboard, so treat this registry as append-only.
REASON_CODES: dict[str, str] = {
    "hard_block": "Order fails a hard business rule (status, credit, RMA, etc.)",
    "no_supply": "No allocatable supply for a time-critical or excepted order",
    "approved_exception": "Released by an approved manual exception",
    "commitment_at_risk": "Released because the promise date is at risk",
    "ship_to_cooldown": "Destination shipped within its cooldown window",
    "waiting_for_prior_major_order": "Major account exposes one order at a time",
    "maximum_hold_reached": "Released: accumulation reached its maximum hold age",
    "daily_customer_batch": "Released: daily store batch time reached",
    "accumulation_target_reached": "Released: gun accumulation target reached",
    "scheduled_customer_release": "Released on the customer's sweep day",
    "complete": "Released: order (or active batch) is complete",
    "accumulating_commitment_at_risk": "Accumulating, but a promise date is at risk",
    "accumulating_for_daily_batch": "Protecting stock until the daily batch time",
    "accumulating_for_customer_release": "Protecting stock toward a threshold or sweep",
    "accumulating_for_completion": "Protecting stock until the order completes",
    "scheduled_hold": "Held for the customer's scheduled sweep day",
    "below_batch_threshold": "Held below the customer's batch threshold",
    "waiting_for_completion": "Held until the full order can ship complete",
    "manual_hold": "Held by an approved set-aside / exception request",
}


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
                "ship_to_id": _text(_get(row, "SHIP_TO_ID", "ship_to_id")),
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
    min_guns = max(
        0.0,
        _num(
            policy.get("target_guns")
            if policy.get("target_guns") not in (None, "")
            else policy.get("min_guns")
        ),
    )
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


def _policy_profile(policy: dict) -> dict[str, Any]:
    accumulate, target_guns, sweep_weekday = _policy_values(policy)
    account_type = str(policy.get("account_type") or "").strip().lower()
    if account_type not in {"major", "standard"}:
        account_type = "major" if accumulate else "standard"
    cadence = str(policy.get("release_cadence") or "").strip().lower()
    if cadence not in {"threshold", "daily", "completion"}:
        cadence = "threshold" if target_guns > EPSILON or sweep_weekday is not None else "completion"
    mix_default = account_type != "major"
    mix_orders = _bool(policy.get("mix_orders"), default=mix_default)
    release_time_raw = str(policy.get("daily_release_time") or "14:00").strip()
    try:
        release_time = clock_time.fromisoformat(release_time_raw)
    except ValueError:
        release_time = clock_time(14, 0)
        release_time_raw = "14:00"
    try:
        max_hold_days = max(0, int(policy.get("max_hold_days", 7)))
    except (TypeError, ValueError):
        max_hold_days = 7
    try:
        ship_to_cooldown_days = max(0, int(policy.get("ship_to_cooldown_days", 0)))
    except (TypeError, ValueError):
        ship_to_cooldown_days = 0
    return {
        "account_type": account_type,
        "accumulate": accumulate,
        "target_guns": target_guns,
        "sweep_weekday": sweep_weekday,
        "mix_orders": mix_orders,
        "release_cadence": cadence,
        "daily_release_time": release_time_raw,
        "daily_release_clock": release_time,
        "max_hold_days": min(max_hold_days, 90),
        "ship_to_cooldown_days": min(ship_to_cooldown_days, 30),
    }


def _cooldown_decision(
    cooldown: dict[str, Any], decision: str, *, at_risk: bool = False
) -> tuple[str, str]:
    """Canonical (reason_code, label) for an active ship-to cooldown.

    The cooldown rule is enforced at three points in the evaluation (at-risk
    commitments, accumulating groups, and plain holds); this is the single
    source for its reason code and label wording.
    """
    verb = "protected until" if decision == "ACCUMULATING" else "eligible"
    label = (
        f"{decision} - ship-to last shipped {cooldown['last_shipped'].isoformat()}; "
        f"{verb} {cooldown['next_eligible'].isoformat()}"
    )
    if at_risk:
        label += "; commitment at risk - review exception"
    return "ship_to_cooldown", label


def _as_datetime(value: Any) -> Optional[datetime]:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, clock_time.min, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _allocate(
    order: dict,
    remaining: dict[str, float],
    require_complete: bool,
    reserved: Optional[dict[str, float]] = None,
) -> dict:
    trial = deepcopy(remaining)
    reserved = reserved or {}
    allocations = []
    for line in order["lines"]:
        available = max(0.0, trial.get(line["part_id"], 0.0))
        reserved_qty = min(
            line["open_qty"], max(0.0, reserved.get(line["part_id"], 0.0))
        )
        new_qty = min(max(0.0, line["open_qty"] - reserved_qty), available)
        qty = reserved_qty + new_qty
        if require_complete and qty < line["open_qty"] - EPSILON:
            return {
                "complete": False,
                "ready_qty": 0.0,
                "ready_guns": 0.0,
                "allocations": [],
                "remaining": remaining,
            }
        if new_qty > EPSILON:
            trial[line["part_id"]] = available - new_qty
        if qty > EPSILON:
            allocations.append(
                {
                    **line,
                    "qty": qty,
                    "reserved_qty": reserved_qty,
                    "new_qty": new_qty,
                }
            )
    ready_qty = sum(row["qty"] for row in allocations)
    ready_guns = sum(row["qty"] for row in allocations if row["item_type"] == "guns")
    return {
        "complete": ready_qty >= order["open_qty"] - EPSILON,
        "ready_qty": ready_qty,
        "ready_guns": ready_guns,
        "allocations": allocations,
        "remaining": trial,
    }


def _active_manual_hold_map(holds: Iterable[dict], today: date) -> dict[str, dict]:
    result = {}
    for row in holds or ():
        order_id = _text(_get(row, "cust_order_id", "CUST_ORDER_ID", "order_id"))
        released = _get(row, "released_at", "RELEASED_AT")
        expires = _as_date(_get(row, "expires_at", "EXPIRES_AT"))
        if not order_id or not _is_missing(released):
            continue
        if expires is not None and expires < today:
            continue
        result[order_id.upper()] = row
    return result


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
    serial_inventory: Optional[Iterable[dict]] = None,
    reservations: Iterable[dict] = (),
    ship_to_history: Optional[Iterable[dict]] = None,
    local_now: Optional[datetime] = None,
    policy_version: int = 1,
    evaluated_at: Optional[datetime] = None,
    min_ship_to_cooldown_days: int = 0,
    manual_holds: Iterable[dict] = (),
) -> dict[str, Any]:
    """Return one auditable decision per order, including protected accumulation."""
    normalized_mode = (mode or "advisory").strip().lower()
    if normalized_mode not in {"off", "advisory", "enforced"}:
        raise ValueError("mode must be off, advisory, or enforced")
    if due_override_days < 0:
        raise ValueError("due_override_days must be zero or greater")
    if min_ship_to_cooldown_days < 0:
        raise ValueError("min_ship_to_cooldown_days must be zero or greater")
    min_cooldown_days = min(int(min_ship_to_cooldown_days), 30)

    orders, supply = _normalize_candidates(rows)
    policies = {str(key).upper(): value for key, value in (customer_policies or {}).items()}
    default_policy = policies.get("DEFAULT", {})

    def policy_for(order: dict) -> dict:
        result = dict(default_policy)
        result.update(policies.get(_customer_key(order), {}))
        return result

    evaluated = evaluated_at or datetime.now(timezone.utc)
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=timezone.utc)
    local_clock = local_now or evaluated
    if local_clock.tzinfo is None:
        local_clock = local_clock.replace(tzinfo=timezone.utc)

    ship_to_tracking_available = ship_to_history is not None
    last_shipments: dict[tuple[str, str], date] = {}
    if ship_to_history is not None:
        for raw in ship_to_history:
            customer_id = (_text(_get(raw, "CUSTOMER_ID", "customer_id")) or "").upper()
            ship_to_id = (
                _text(_get(raw, "SHIP_TO_ID", "ship_to_id")) or "DEFAULT"
            ).upper()
            last_shipped = _as_date(
                _get(raw, "LAST_SHIPPED_DATE", "last_shipped_date")
            )
            if not customer_id or last_shipped is None:
                continue
            key = (customer_id, ship_to_id)
            if key not in last_shipments or last_shipped > last_shipments[key]:
                last_shipments[key] = last_shipped

    def ship_to_cooldown(order: dict, profile: dict[str, Any]) -> dict[str, Any]:
        customer_id = _customer_key(order)
        ship_to_id = (order.get("ship_to_id") or "DEFAULT").strip().upper()
        last_shipped = last_shipments.get((customer_id, ship_to_id))
        # The global floor guarantees same-day duplicate protection even for
        # customers with no explicit ship_to_cooldown_days policy.
        cooldown_days = max(
            int(profile.get("ship_to_cooldown_days") or 0), min_cooldown_days
        )
        next_eligible = (
            last_shipped + timedelta(days=cooldown_days)
            if last_shipped is not None and cooldown_days > 0
            else None
        )
        return {
            "days": cooldown_days,
            "last_shipped": last_shipped,
            "next_eligible": next_eligible,
            "active": bool(next_eligible is not None and today < next_eligible),
        }

    serial_tracking_available = serial_inventory is not None
    serial_rows: list[dict[str, Any]] = []
    live_serials: set[str] = set()
    serials_by_part: dict[str, list[dict[str, Any]]] = {}
    if serial_inventory is not None:
        for raw in serial_inventory:
            serial_no = _text(_get(raw, "SERIAL_NO", "serial_no"))
            part_id = _text(_get(raw, "PART_ID", "part_id"))
            if not serial_no or not part_id:
                continue
            serial_key = serial_no.upper()
            if serial_key in live_serials:
                continue
            normalized_serial = {
                "serial_no": serial_key,
                "part_id": part_id.upper(),
                "warehouse_id": _text(_get(raw, "WAREHOUSE_ID", "warehouse_id")),
                "location_id": _text(_get(raw, "LOCATION_ID", "location_id")),
                "last_transaction_at": _text(
                    _get(raw, "LAST_TRANSACTION_AT", "last_transaction_at")
                ),
            }
            live_serials.add(serial_key)
            serial_rows.append(normalized_serial)
            serials_by_part.setdefault(part_id.upper(), []).append(normalized_serial)
        for part_serials in serials_by_part.values():
            part_serials.sort(
                key=lambda row: (row.get("last_transaction_at") or "", row["serial_no"])
            )

        gun_parts = {
            line["part_id"]
            for order in orders.values()
            for line in order["lines"]
            if line["item_type"] == "guns"
        }
        for part_id in gun_parts:
            supply[part_id] = min(
                supply.get(part_id, 0.0), float(len(serials_by_part.get(part_id, [])))
            )

    valid_reservations: list[dict[str, Any]] = []
    reserved_by_order_part: dict[str, dict[str, float]] = {}
    reservation_serials_by_order_part: dict[tuple[str, str], list[dict[str, Any]]] = {}
    reserved_serials: set[str] = set()
    accumulation_started_by_order: dict[str, datetime] = {}
    for raw in reservations:
        if str(raw.get("status") or "active").lower() != "active":
            continue
        serial_no = str(raw.get("serial_no") or "").strip().upper()
        order_id = str(raw.get("cust_order_id") or raw.get("order_id") or "").strip().upper()
        part_id = str(raw.get("part_id") or "").strip().upper()
        if not serial_no or not order_id or not part_id or order_id not in orders:
            continue
        if serial_tracking_available and serial_no not in live_serials:
            continue
        order_parts = {line["part_id"] for line in orders[order_id]["lines"]}
        if part_id not in order_parts or serial_no in reserved_serials:
            continue
        normalized = {
            **raw,
            "serial_no": serial_no,
            "cust_order_id": order_id,
            "part_id": part_id,
        }
        valid_reservations.append(normalized)
        reserved_serials.add(serial_no)
        reserved_by_order_part.setdefault(order_id, {})[part_id] = (
            reserved_by_order_part.setdefault(order_id, {}).get(part_id, 0.0) + 1.0
        )
        reservation_serials_by_order_part.setdefault((order_id, part_id), []).append(
            normalized
        )
        started = _as_datetime(
            raw.get("accumulation_started_at") or raw.get("first_assigned_at")
        )
        if started and (
            order_id not in accumulation_started_by_order
            or started < accumulation_started_by_order[order_id]
        ):
            accumulation_started_by_order[order_id] = started

    remaining = deepcopy(supply)
    for order_parts in reserved_by_order_part.values():
        for part_id, qty in order_parts.items():
            remaining[part_id] = max(0.0, remaining.get(part_id, 0.0) - qty)

    def allocate(order: dict, current: dict[str, float], require_complete: bool) -> dict:
        return _allocate(
            order,
            current,
            require_complete,
            reserved=reserved_by_order_part.get(order["order_id"].upper(), {}),
        )

    active_exceptions = _active_exception_map(exceptions, today)
    manual_hold_map = _active_manual_hold_map(manual_holds, today)
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
        policy_profile: Optional[dict[str, Any]] = None,
        batch_key: Optional[str] = None,
        reservation_age_days: Optional[int] = None,
        accumulation_started_at: Optional[datetime] = None,
    ) -> None:
        ready_qty = allocation.get("ready_qty", 0.0)
        accumulating = decision == "ACCUMULATING"
        cooldown_profile = policy_profile or _policy_profile(policy_for(order))
        cooldown = ship_to_cooldown(order, cooldown_profile)
        decisions[order["order_id"].upper()] = {
            "order_id": order["order_id"],
            "customer_id": order["customer_id"],
            "customer_name": order["customer_name"],
            "ship_to_id": order["ship_to_id"],
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
            "account_type": (policy_profile or {}).get("account_type"),
            "mix_orders": (policy_profile or {}).get("mix_orders"),
            "release_cadence": (policy_profile or {}).get("release_cadence"),
            "max_hold_days": (policy_profile or {}).get("max_hold_days"),
            "ship_to_cooldown_days": cooldown["days"],
            "last_ship_to_shipment_date": (
                cooldown["last_shipped"].isoformat() if cooldown["last_shipped"] else None
            ),
            "next_ship_to_eligible_date": (
                cooldown["next_eligible"].isoformat() if cooldown["next_eligible"] else None
            ),
            "ship_to_cooldown_active": cooldown["active"],
            "batch_key": batch_key,
            "reservation_age_days": reservation_age_days,
            "accumulation_started_at": (
                accumulation_started_at.isoformat() if accumulation_started_at else None
            ),
            "promise_ship": order["promise_ship"].isoformat() if order["promise_ship"] else None,
            "promise_del": order["promise_del"].isoformat() if order["promise_del"] else None,
            "next_release_date": next_release_date.isoformat() if next_release_date else None,
            "allocations": [
                {
                    "line_no": row["line_no"],
                    "part_id": row["part_id"],
                    "qty": _round_qty(row["qty"]),
                    "reserved_qty": _round_qty(row.get("reserved_qty", 0.0)),
                    "new_qty": _round_qty(row.get("new_qty", row["qty"])),
                    "item_type": row["item_type"],
                }
                for row in allocation.get("allocations", [])
            ],
        }

    ordered = sorted(orders.values(), key=_priority)

    # Manual holds (approved set-aside / exception requests) never reserve supply
    # and never release, whatever the account policy says. They also count as a
    # block reason so a held order can never head a consolidation batch.
    for order in ordered:
        hold = manual_hold_map.get(order["order_id"].upper())
        if not hold:
            continue
        order["block_reasons"].add("manual hold")
        kind = _text(_get(hold, "hold_kind", "HOLD_KIND")) or "hold"
        until = _text(_get(hold, "expires_at", "EXPIRES_AT"))[:10]
        potential = allocate(order, remaining, require_complete=False)
        record(order, "HOLD", "manual_hold", f"HOLD - manual {kind} hold until {until}", potential)

    # Hard business blocks never reserve supply.
    for order in ordered:
        if not order["block_reasons"] or order["order_id"].upper() in decisions:
            continue
        potential = allocate(order, remaining, require_complete=False)
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
        policy = policy_for(order)
        profile = _policy_profile(policy)
        accumulates = _bool(policy.get("accumulate")) and order["open_guns"] > EPSILON
        if not exception and (not at_risk or accumulates):
            continue
        allocation = allocate(order, remaining, require_complete=False)
        if allocation["ready_qty"] <= EPSILON:
            record(order, "HOLD", "no_supply", "HOLD - no allocatable supply", allocation)
            continue
        cooldown = ship_to_cooldown(order, profile)
        if not exception and cooldown["active"]:
            code, label = _cooldown_decision(cooldown, "HOLD", at_risk=at_risk)
            record(
                order,
                "HOLD",
                code,
                label,
                allocation,
                cooldown["next_eligible"],
                commitment_at_risk=at_risk,
                policy_profile=profile,
            )
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

    # Accumulating policies reserve serial-backed supply. Major accounts with mixing
    # disabled expose exactly one active sales order at a time. Standard daily policies
    # may combine orders only inside the same customer + ship-to batch.
    major_head_by_customer: dict[str, str] = {}
    reserved_heads = sorted(
        accumulation_started_by_order.items(),
        key=lambda item: (item[1], item[0]),
    )
    for order_id, _started in reserved_heads:
        order = orders.get(order_id)
        if not order or order["block_reasons"] or order["open_guns"] <= EPSILON:
            continue
        profile = _policy_profile(policy_for(order))
        if profile["accumulate"] and profile["account_type"] == "major" and not profile["mix_orders"]:
            major_head_by_customer.setdefault(_customer_key(order), order_id)
    for order in ordered:
        if order["block_reasons"] or order["open_guns"] <= EPSILON:
            continue
        profile = _policy_profile(policy_for(order))
        if profile["accumulate"] and profile["account_type"] == "major" and not profile["mix_orders"]:
            major_head_by_customer.setdefault(_customer_key(order), order["order_id"].upper())

    accumulating_by_scope: dict[str, list[dict]] = {}
    scope_profiles: dict[str, dict[str, Any]] = {}
    for order in ordered:
        key = order["order_id"].upper()
        if key in decisions or order["open_guns"] <= EPSILON:
            continue
        profile = _policy_profile(policy_for(order))
        if not profile["accumulate"]:
            continue
        customer_key = _customer_key(order)
        if (
            profile["account_type"] == "major"
            and not profile["mix_orders"]
            and major_head_by_customer.get(customer_key) != key
        ):
            potential = allocate(order, remaining, require_complete=False)
            record(
                order,
                "HOLD",
                "waiting_for_prior_major_order",
                f"HOLD - waiting for {major_head_by_customer[customer_key]} to clear",
                potential,
                policy_profile=profile,
                batch_key=f"ORDER:{key}",
            )
            continue
        if profile["account_type"] == "major" and not profile["mix_orders"]:
            scope_key = f"ORDER:{key}"
        elif profile["release_cadence"] == "daily":
            scope_key = f"STORE:{customer_key}|SHIPTO:{order.get('ship_to_id') or 'DEFAULT'}"
        else:
            scope_key = f"CUSTOMER:{customer_key}"
        accumulating_by_scope.setdefault(scope_key, []).append(order)
        scope_profiles[scope_key] = profile

    accumulating_groups = sorted(
        accumulating_by_scope.items(),
        key=lambda item: _priority(sorted(item[1], key=_priority)[0]),
    )
    for scope_key, customer_orders in accumulating_groups:
        profile = scope_profiles[scope_key]
        target_guns = float(profile["target_guns"])
        sweep_weekday = profile["sweep_weekday"]
        sweep_day = sweep_weekday is not None and today.weekday() == sweep_weekday
        next_sweep = None
        if sweep_weekday is not None:
            sweep_search_date = today + timedelta(days=1) if sweep_day else today
            next_sweep = _next_weekday(sweep_search_date, sweep_weekday)

        trial_remaining = deepcopy(remaining)
        accumulated: list[tuple[dict, dict]] = []
        for order in sorted(customer_orders, key=_priority):
            allocation = allocate(order, trial_remaining, require_complete=False)
            trial_remaining = allocation["remaining"]
            accumulated.append((order, allocation))

        group_ready_guns = sum(
            allocation["ready_guns"] for _, allocation in accumulated
        )
        threshold_reached = (
            target_guns > EPSILON and group_ready_guns >= target_guns - EPSILON
        )
        group_complete = bool(accumulated) and all(
            allocation["complete"] for _, allocation in accumulated
        )
        group_started_values = [
            accumulation_started_by_order[order["order_id"].upper()]
            for order, _ in accumulated
            if order["order_id"].upper() in accumulation_started_by_order
        ]
        group_started = min(group_started_values) if group_started_values else None
        reservation_age_days = None
        aged_release = False
        if group_started is not None:
            started_local = group_started.astimezone(local_clock.tzinfo).date()
            reservation_age_days = max(0, (local_clock.date() - started_local).days)
            aged_release = reservation_age_days >= profile["max_hold_days"]

        daily_release = (
            profile["release_cadence"] == "daily"
            and local_clock.time().replace(tzinfo=None) >= profile["daily_release_clock"]
        )
        completion_release = (
            group_complete
            and (
                profile["release_cadence"] == "completion"
                or (profile["account_type"] == "major" and not profile["mix_orders"])
            )
        )
        group_releases = (
            threshold_reached
            or sweep_day
            or daily_release
            or aged_release
            or completion_release
        )
        remaining = trial_remaining

        if profile["release_cadence"] == "daily":
            release_condition = (
                f"Release one customer/ship-to batch daily at {profile['daily_release_time']} "
                f"or after {profile['max_hold_days']} days"
            )
            next_release_date = today if not daily_release else today + timedelta(days=1)
        elif target_guns > EPSILON:
            release_condition = (
                f"Release at {_round_qty(target_guns)} guns, when the active order is complete, "
                f"or after {profile['max_hold_days']} days"
            )
            next_release_date = next_sweep
        elif sweep_weekday is not None:
            release_condition = (
                f"Release on the {calendar.day_name[sweep_weekday]} sweep or after "
                f"{profile['max_hold_days']} days"
            )
            next_release_date = next_sweep
        else:
            release_condition = (
                f"Release when complete or after {profile['max_hold_days']} days"
            )
            next_release_date = None

        for order, allocation in accumulated:
            at_risk = _is_at_risk(order, today, due_override_days)
            cooldown = ship_to_cooldown(order, profile)
            if (
                group_releases
                and not cooldown["active"]
                and allocation["ready_qty"] > EPSILON
            ):
                if aged_release:
                    code = "maximum_hold_reached"
                    label = f"SHIP NOW - {profile['max_hold_days']}-day maximum reached"
                elif daily_release:
                    code = "daily_customer_batch"
                    label = f"SHIP NOW - daily {profile['daily_release_time']} store batch"
                elif threshold_reached:
                    code = "accumulation_target_reached"
                    label = (
                        "SHIP NOW - accumulation target reached "
                        f"({_round_qty(group_ready_guns)} guns)"
                    )
                elif sweep_day:
                    code = "scheduled_customer_release"
                    label = "SHIP NOW - scheduled customer release"
                else:
                    code = "complete"
                    label = "SHIP NOW - active sales order complete"
                record(
                    order,
                    "RELEASE",
                    code,
                    label,
                    allocation,
                    target_guns=target_guns if target_guns > EPSILON else None,
                    group_ready_guns=group_ready_guns,
                    release_condition=release_condition,
                    commitment_at_risk=at_risk,
                    policy_profile=profile,
                    batch_key=scope_key,
                    reservation_age_days=reservation_age_days,
                    accumulation_started_at=group_started,
                )
                continue

            decision_next_release = next_release_date
            if cooldown["active"]:
                decision_next_release = cooldown["next_eligible"]
                _, label = _cooldown_decision(
                    cooldown, "ACCUMULATING", at_risk=at_risk
                )
            elif allocation["ready_qty"] <= EPSILON:
                label = "ACCUMULATING - waiting for allocatable supply"
            elif profile["release_cadence"] == "daily":
                label = (
                    f"ACCUMULATING - {_round_qty(group_ready_guns)} store guns protected "
                    f"until {profile['daily_release_time']}"
                )
            elif target_guns > EPSILON:
                label = (
                    "ACCUMULATING - "
                    f"{_round_qty(group_ready_guns)}/{_round_qty(target_guns)} "
                    f"guns protected for {scope_key.lower()}"
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
            if cooldown["active"]:
                code = "ship_to_cooldown"
            elif at_risk:
                label += "; commitment at risk - review exception"
                code = "accumulating_commitment_at_risk"
            elif profile["release_cadence"] == "daily":
                code = "accumulating_for_daily_batch"
            elif target_guns > EPSILON or sweep_weekday is not None:
                code = "accumulating_for_customer_release"
            else:
                code = "accumulating_for_completion"
            record(
                order,
                "ACCUMULATING",
                code,
                label,
                allocation,
                decision_next_release,
                target_guns=(target_guns if target_guns > EPSILON else order["open_guns"]),
                group_ready_guns=group_ready_guns,
                release_condition=release_condition,
                commitment_at_risk=at_risk,
                policy_profile=profile,
                batch_key=scope_key,
                reservation_age_days=reservation_age_days,
                accumulation_started_at=group_started,
            )

    # A non-accumulating order for a destination shipped inside its configured
    # cooldown remains eligible for supply on the next allowed day; it does not
    # consume inventory while held. Approved exceptions were already handled above.
    for order in ordered:
        key = order["order_id"].upper()
        if key in decisions:
            continue
        profile = _policy_profile(policy_for(order))
        cooldown = ship_to_cooldown(order, profile)
        if not cooldown["active"]:
            continue
        potential = allocate(order, remaining, require_complete=False)
        code, label = _cooldown_decision(cooldown, "HOLD")
        record(
            order,
            "HOLD",
            code,
            label,
            potential,
            cooldown["next_eligible"],
            policy_profile=profile,
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
        policy = policy_for(customer_orders[0])
        profile = _policy_profile(policy)
        min_guns = profile["target_guns"]
        sweep_weekday = profile["sweep_weekday"]

        trial_remaining = deepcopy(remaining)
        complete_candidates: list[tuple[dict, dict]] = []
        incomplete_orders: list[tuple[dict, dict]] = []
        for order in sorted(customer_orders, key=_priority):
            allocation = allocate(order, trial_remaining, require_complete=True)
            if allocation["complete"]:
                trial_remaining = allocation["remaining"]
                complete_candidates.append((order, allocation))
            else:
                incomplete_orders.append(
                    (order, allocate(order, remaining, require_complete=False))
                )

        ready_guns = sum(allocation["ready_guns"] for _, allocation in complete_candidates)
        # A DEFAULT policy alone (or an empty per-customer stub) must not change
        # batching semantics: only a customer-specific entry with batching values
        # (a gun threshold or a sweep day) opts the customer into custom batching.
        has_custom_policy = customer_key in policies and (
            min_guns > EPSILON or sweep_weekday is not None
        )
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

    # Attach concrete serials to every protected/released configured batch. Existing
    # assignments are consumed first; newly available serials fill only the remaining
    # quantity. The caller persists ``reservation_updates`` after reconciling this same
    # ERP snapshot, making the assignment sticky across future evaluations.
    available_serials_by_part = {
        part_id: [
            row for row in part_serials if row["serial_no"] not in reserved_serials
        ]
        for part_id, part_serials in serials_by_part.items()
    }
    reservation_updates: list[dict[str, Any]] = []
    for decision in decisions.values():
        decision["serial_assignments"] = []
        if decision["decision"] not in {"RELEASE", "ACCUMULATING"} or not decision.get("batch_key"):
            continue
        order_id = decision["order_id"].upper()
        order_started = accumulation_started_by_order.get(order_id)
        for allocation in decision["allocations"]:
            if allocation["item_type"] != "guns":
                continue
            part_id = allocation["part_id"]
            existing_candidates = reservation_serials_by_order_part.get(
                (order_id, part_id), []
            )
            existing_needed = max(0, int(round(float(allocation.get("reserved_qty") or 0))))
            selected_existing = existing_candidates[:existing_needed]
            new_needed = max(0, int(round(float(allocation.get("new_qty") or 0))))
            selected_new = available_serials_by_part.get(part_id, [])[:new_needed]
            if selected_new:
                del available_serials_by_part[part_id][: len(selected_new)]

            for existing in selected_existing:
                first_assigned = str(existing.get("first_assigned_at") or evaluated.isoformat())
                accumulation_started = str(
                    existing.get("accumulation_started_at")
                    or (order_started.isoformat() if order_started else first_assigned)
                )
                assignment = {
                    "serial_no": existing["serial_no"],
                    "part_id": part_id,
                    "customer_id": decision.get("customer_id"),
                    "cust_order_id": decision["order_id"],
                    "line_no": allocation.get("line_no"),
                    "first_assigned_at": first_assigned,
                    "accumulation_started_at": accumulation_started,
                    "existing": True,
                }
                decision["serial_assignments"].append(assignment)
                reservation_updates.append(assignment)

            for serial_row in selected_new:
                first_assigned = evaluated.isoformat()
                accumulation_started = (
                    order_started.isoformat() if order_started else first_assigned
                )
                assignment = {
                    "serial_no": serial_row["serial_no"],
                    "part_id": part_id,
                    "customer_id": decision.get("customer_id"),
                    "cust_order_id": decision["order_id"],
                    "line_no": allocation.get("line_no"),
                    "first_assigned_at": first_assigned,
                    "accumulation_started_at": accumulation_started,
                    "existing": False,
                }
                decision["serial_assignments"].append(assignment)
                reservation_updates.append(assignment)

        if decision["serial_assignments"] and not decision.get("accumulation_started_at"):
            decision["accumulation_started_at"] = min(
                row["accumulation_started_at"] for row in decision["serial_assignments"]
            )
            decision["reservation_age_days"] = 0

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
        "tracked_serials": len(reservation_updates),
        # Total kept for template compatibility; the split separates orders blocked
        # from shipping (HOLD) from stock protected while accumulating anyway.
        "ship_to_cooldown": sum(
            row["reason_code"] == "ship_to_cooldown" for row in decision_rows
        ),
        "ship_to_cooldown_holds": sum(
            row["reason_code"] == "ship_to_cooldown" and row["decision"] == "HOLD"
            for row in decision_rows
        ),
        "ship_to_cooldown_accumulating": sum(
            row["reason_code"] == "ship_to_cooldown"
            and row["decision"] == "ACCUMULATING"
            for row in decision_rows
        ),
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
    return {
        "mode": normalized_mode,
        "policy_version": int(policy_version),
        "evaluated_at": evaluated.isoformat(),
        "due_override_days": due_override_days,
        "summary": summary,
        "released_orders": [row["order_id"] for row in decision_rows if row["decision"] == "RELEASE"],
        "decisions": decision_rows,
        "serial_tracking_available": serial_tracking_available,
        "ship_to_tracking_available": ship_to_tracking_available,
        "reservation_updates": reservation_updates,
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
