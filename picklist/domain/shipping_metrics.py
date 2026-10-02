"""Shipping management KPI calculations.

The module is deliberately independent from Flask, pandas, and database access.  SQL
returns a compact union of ORDER_LINE and SHIPMENT_LINE records; this module owns the
business grain, de-duplication, period logic, trends, diagnostics, and JSON-safe output.

Headline definitions are intentionally explicit:

* Ship on time: due-order cohort completed by the latest effective Promise Ship date.
* Ship complete: first shipment day cleared every physical line on the order.
* Guns / shipment measures: serialized firearm units on live firearm-bearing packlists.

Promise-date coverage and missing order facts are surfaced as guardrails instead of being
silently treated as successes or failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional


VOIDED_SHIPPER_STATUSES = {"X", "V"}
EPSILON = 1e-9


METRIC_DEFINITIONS = {
    "ship_on_time": {
        "label": "Ship on time",
        "format": "percent",
        "definition": (
            "Orders due in the selected window whose physical lines were fully shipped "
            "by the latest effective Promise Ship date. Orders with incomplete promise "
            "date coverage are shown separately and excluded from the rate."
        ),
    },
    "ship_complete": {
        "label": "Ship complete",
        "format": "percent",
        "definition": (
            "Orders whose first shipment day cleared all physical order quantity, divided "
            "by orders whose first shipment day falls in the selected window."
        ),
    },
    "average_guns_per_shipment": {
        "label": "Average guns per shipment",
        "format": "decimal",
        "definition": (
            "Serialized firearm units shipped divided by distinct live firearm-bearing "
            "VISUAL packlists in the selected window."
        ),
    },
    "single_gun_shipments": {
        "label": "Single-gun shipments",
        "format": "integer",
        "definition": (
            "Distinct live firearm-bearing VISUAL packlists containing exactly one "
            "serialized firearm. The companion rate uses firearm-bearing packlists as "
            "its denominator."
        ),
    },
    "total_guns_shipped": {
        "label": "Total guns shipped",
        "format": "integer",
        "definition": (
            "Serialized firearm units on non-voided shipment lines in the selected window."
        ),
    },
    "total_shipments": {
        "label": "Total shipments",
        "format": "integer",
        "definition": (
            "Distinct non-voided VISUAL packlists containing at least one serialized "
            "firearm in the selected window."
        ),
    },
}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN / NaT
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


def _optional_num(value: Any) -> Optional[float]:
    return None if _is_missing(value) else _num(value)


def _as_datetime(value: Any) -> Optional[datetime]:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _as_date(value: Any) -> Optional[date]:
    parsed = _as_datetime(value)
    return parsed.date() if parsed else None


def _round_qty(value: float) -> int | float:
    rounded = round(value)
    if abs(value - rounded) <= EPSILON:
        return int(rounded)
    return round(value, 2)


def _pct(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


@dataclass
class OrderLine:
    order_id: str
    line_no: str
    customer_id: Optional[str]
    customer_name: Optional[str]
    part_id: Optional[str]
    product_code: Optional[str]
    order_qty: float
    promise_ship: Optional[date]
    order_first_ship: Optional[date]
    order_shipped_by_promise: Optional[float]
    order_first_day_shipped: Optional[float]


@dataclass
class ShipmentLine:
    packlist_id: str
    shipper_line_no: str
    order_id: Optional[str]
    order_line_no: Optional[str]
    customer_id: Optional[str]
    customer_name: Optional[str]
    part_id: Optional[str]
    shipped_at: datetime
    shipped_qty: float
    firearm_qty: float
    status: str


@dataclass
class PreparedData:
    order_lines: dict[tuple[str, str], OrderLine] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    shipments: list[ShipmentLine] = field(default_factory=list)
    duplicate_shipment_rows: int = 0


def _prepare(rows: Iterable[dict]) -> PreparedData:
    prepared = PreparedData()
    raw_shipments: dict[tuple[str, str, str, str, str], ShipmentLine] = {}

    for index, row in enumerate(rows):
        record_type = (_text(_get(row, "RECORD_TYPE", "record_type")) or "").upper()
        order_id = _text(_get(row, "CUST_ORDER_ID", "cust_order_id", "order_id"))
        line_no = _text(
            _get(row, "CUST_ORDER_LINE_NO", "cust_order_line_no", "LINE_NO", "line_no")
        )

        if record_type == "ORDER_LINE":
            if not order_id or line_no is None:
                continue
            key = (order_id.upper(), line_no)
            candidate = OrderLine(
                order_id=order_id,
                line_no=line_no,
                customer_id=_text(_get(row, "CUSTOMER_ID", "customer_id")),
                customer_name=_text(_get(row, "CUSTOMER_NAME", "customer_name")),
                part_id=_text(_get(row, "PART_ID", "part_id")),
                product_code=_text(_get(row, "PRODUCT_CODE", "product_code")),
                order_qty=max(0.0, _num(_get(row, "ORDER_QTY", "order_qty"))),
                promise_ship=_as_date(
                    _get(row, "PROMISE_SHIP_DATE", "promise_ship_date", "promise_ship")
                ),
                order_first_ship=_as_date(
                    _get(row, "ORDER_FIRST_SHIP_DATE", "order_first_ship_date")
                ),
                order_shipped_by_promise=_optional_num(
                    _get(
                        row,
                        "ORDER_SHIPPED_BY_PROMISE_QTY",
                        "order_shipped_by_promise_qty",
                    )
                ),
                order_first_day_shipped=_optional_num(
                    _get(
                        row,
                        "ORDER_FIRST_DAY_SHIPPED_QTY",
                        "order_first_day_shipped_qty",
                    )
                ),
            )
            existing = prepared.order_lines.get(key)
            if existing is None or candidate.order_qty > existing.order_qty:
                prepared.order_lines[key] = candidate
            continue

        if record_type != "SHIPMENT_LINE":
            continue
        packlist_id = _text(_get(row, "PACKLIST_ID", "packlist_id"))
        shipped_at = _as_datetime(_get(row, "SHIPPED_DATE", "shipped_date"))
        if not packlist_id or shipped_at is None:
            continue
        status = (_text(_get(row, "SHIPPER_STATUS", "shipper_status")) or "").upper()
        if status in VOIDED_SHIPPER_STATUSES:
            continue
        shipper_line_no = _text(_get(row, "SHIPPER_LINE_NO", "shipper_line_no"))
        part_id = _text(_get(row, "PART_ID", "part_id"))
        fallback_line_key = f"{line_no or ''}:{part_id or ''}:{index}"
        stable_line_key = shipper_line_no or fallback_line_key
        shipment = ShipmentLine(
            packlist_id=packlist_id,
            shipper_line_no=stable_line_key,
            order_id=order_id,
            order_line_no=line_no,
            customer_id=_text(_get(row, "CUSTOMER_ID", "customer_id")),
            customer_name=_text(_get(row, "CUSTOMER_NAME", "customer_name")),
            part_id=part_id,
            shipped_at=shipped_at,
            shipped_qty=max(0.0, _num(_get(row, "SHIPPED_QTY", "shipped_qty"))),
            firearm_qty=max(0.0, _num(_get(row, "FIREARM_QTY", "firearm_qty"))),
            status=status,
        )
        dedupe_key = (
            packlist_id.upper(),
            stable_line_key,
            (order_id or "").upper(),
            line_no or "",
            shipped_at.isoformat(),
        )
        if dedupe_key in raw_shipments:
            prepared.duplicate_shipment_rows += 1
            existing = raw_shipments[dedupe_key]
            existing.shipped_qty = max(existing.shipped_qty, shipment.shipped_qty)
            existing.firearm_qty = max(existing.firearm_qty, shipment.firearm_qty)
        else:
            raw_shipments[dedupe_key] = shipment

    prepared.shipments = sorted(
        raw_shipments.values(),
        key=lambda row: (row.shipped_at, row.packlist_id, row.shipper_line_no),
    )

    for line in prepared.order_lines.values():
        order = prepared.orders.setdefault(
            line.order_id.upper(),
            {
                "order_id": line.order_id,
                "customer_id": line.customer_id,
                "customer_name": line.customer_name,
                "lines": [],
                "order_qty": 0.0,
                "promise_ship": None,
                "missing_promise_lines": 0,
                "first_ship_date": None,
                "shipped_by_promise_qty": None,
                "first_day_shipped_qty": None,
            },
        )
        order["lines"].append(line)
        order["order_qty"] += line.order_qty
        if line.promise_ship is None:
            order["missing_promise_lines"] += 1
        elif order["promise_ship"] is None or line.promise_ship > order["promise_ship"]:
            # Complete-order commitment: all physical lines are due by the latest line promise.
            order["promise_ship"] = line.promise_ship
        if line.order_first_ship is not None and (
            order["first_ship_date"] is None
            or line.order_first_ship < order["first_ship_date"]
        ):
            order["first_ship_date"] = line.order_first_ship
        if line.order_shipped_by_promise is not None:
            order["shipped_by_promise_qty"] = max(
                order["shipped_by_promise_qty"] or 0.0,
                line.order_shipped_by_promise,
            )
        if line.order_first_day_shipped is not None:
            order["first_day_shipped_qty"] = max(
                order["first_day_shipped_qty"] or 0.0,
                line.order_first_day_shipped,
            )
        if not order["customer_id"]:
            order["customer_id"] = line.customer_id
        if not order["customer_name"]:
            order["customer_name"] = line.customer_name

    for order in prepared.orders.values():
        order["line_keys"] = {
            (order["order_id"].upper(), line.line_no) for line in order["lines"]
        }
    return prepared


def _physical_shipment_qty(prepared: PreparedData, order_id: str, through: date) -> float:
    order = prepared.orders.get(order_id.upper())
    if not order:
        return 0.0
    total = 0.0
    for row in prepared.shipments:
        if not row.order_id or row.order_id.upper() != order_id.upper():
            continue
        if row.shipped_at.date() > through:
            continue
        if row.order_line_no is not None and (
            order_id.upper(), row.order_line_no
        ) not in order["line_keys"]:
            continue
        total += row.shipped_qty
    return total


def _first_ship_day(prepared: PreparedData, order_id: str) -> Optional[date]:
    days = [
        row.shipped_at.date()
        for row in prepared.shipments
        if row.order_id and row.order_id.upper() == order_id.upper()
    ]
    return min(days) if days else None


def _period_summary(prepared: PreparedData, start: date, end: date) -> dict[str, Any]:
    window_shipments = [
        row for row in prepared.shipments if start <= row.shipped_at.date() < end
    ]
    packlists: dict[str, dict[str, Any]] = {}
    for row in window_shipments:
        entry = packlists.setdefault(
            row.packlist_id.upper(),
            {
                "packlist_id": row.packlist_id,
                "ship_date": row.shipped_at.date(),
                "firearm_qty": 0.0,
                "orders": set(),
                "customer_id": row.customer_id,
                "customer_name": row.customer_name,
            },
        )
        entry["firearm_qty"] += row.firearm_qty
        if row.order_id:
            entry["orders"].add(row.order_id.upper())

    firearm_packlists = [row for row in packlists.values() if row["firearm_qty"] > 0]
    guns = sum(row["firearm_qty"] for row in firearm_packlists)
    shipment_count = len(firearm_packlists)
    single_count = sum(abs(row["firearm_qty"] - 1.0) <= EPSILON for row in firearm_packlists)

    due_orders = []
    missing_promise_orders = []
    for order in prepared.orders.values():
        promise = order["promise_ship"]
        if order["missing_promise_lines"]:
            missing_promise_orders.append(order)
            continue
        if promise is not None and start <= promise < end:
            shipped_by_due = order["shipped_by_promise_qty"]
            if shipped_by_due is None:
                shipped_by_due = _physical_shipment_qty(
                    prepared, order["order_id"], promise
                )
            due_orders.append((order, shipped_by_due >= order["order_qty"] - EPSILON, shipped_by_due))
    on_time_count = sum(1 for _, on_time, _ in due_orders if on_time)

    first_ship_orders = []
    for order in prepared.orders.values():
        first_day = order["first_ship_date"] or _first_ship_day(
            prepared, order["order_id"]
        )
        if first_day is None or not (start <= first_day < end):
            continue
        shipped_first_day = order["first_day_shipped_qty"]
        if shipped_first_day is None:
            shipped_first_day = _physical_shipment_qty(
                prepared, order["order_id"], first_day
            )
        first_ship_orders.append(
            (order, shipped_first_day >= order["order_qty"] - EPSILON, shipped_first_day)
        )
    complete_count = sum(1 for _, complete, _ in first_ship_orders if complete)

    return {
        "guns": guns,
        "shipments": shipment_count,
        "average_guns": round(guns / shipment_count, 2) if shipment_count else None,
        "single_shipments": int(single_count),
        "single_rate_pct": _pct(single_count, shipment_count),
        "due_orders": len(due_orders),
        "on_time_orders": on_time_count,
        "ship_on_time_pct": _pct(on_time_count, len(due_orders)),
        "first_ship_orders": len(first_ship_orders),
        "complete_orders": complete_count,
        "ship_complete_pct": _pct(complete_count, len(first_ship_orders)),
        "packlists": firearm_packlists,
        "due_order_details": due_orders,
        "first_ship_order_details": first_ship_orders,
        "missing_promise_orders": missing_promise_orders,
    }


def _metric(
    value: Any,
    prior_value: Any,
    *,
    numerator: Any = None,
    denominator: Any = None,
    companion: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    delta = None
    if value is not None and prior_value is not None:
        delta = round(float(value) - float(prior_value), 2)
    return {
        "value": value,
        "prior_value": prior_value,
        "delta": delta,
        "numerator": numerator,
        "denominator": denominator,
        "companion": companion or {},
    }


def build_shipping_metrics(
    rows: Iterable[dict],
    start: date,
    end: date,
    *,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build JP's six shipping KPIs and decision-ready diagnostics.

    ``start`` is inclusive and ``end`` is exclusive. The input should include the
    immediately preceding equal-length window so comparison values can be calculated.
    """
    if not isinstance(start, date) or not isinstance(end, date) or start >= end:
        raise ValueError("start and end must be dates with start before end")

    prepared = _prepare(rows)
    current = _period_summary(prepared, start, end)
    span = end - start
    prior_start = start - span
    prior = _period_summary(prepared, prior_start, start)

    cards = {
        "ship_on_time": _metric(
            current["ship_on_time_pct"],
            prior["ship_on_time_pct"],
            numerator=current["on_time_orders"],
            denominator=current["due_orders"],
        ),
        "ship_complete": _metric(
            current["ship_complete_pct"],
            prior["ship_complete_pct"],
            numerator=current["complete_orders"],
            denominator=current["first_ship_orders"],
        ),
        "average_guns_per_shipment": _metric(
            current["average_guns"],
            prior["average_guns"],
            numerator=_round_qty(current["guns"]),
            denominator=current["shipments"],
        ),
        "single_gun_shipments": _metric(
            current["single_shipments"],
            prior["single_shipments"],
            numerator=current["single_shipments"],
            denominator=current["shipments"],
            companion={"rate_pct": current["single_rate_pct"]},
        ),
        "total_guns_shipped": _metric(
            _round_qty(current["guns"]),
            _round_qty(prior["guns"]),
        ),
        "total_shipments": _metric(current["shipments"], prior["shipments"]),
    }
    for key, definition in METRIC_DEFINITIONS.items():
        cards[key].update(definition)

    trends = []
    cursor = start
    while cursor < end:
        day = _period_summary(prepared, cursor, cursor + timedelta(days=1))
        trends.append(
            {
                "date": cursor.isoformat(),
                "guns": _round_qty(day["guns"]),
                "shipments": day["shipments"],
                "average_guns": day["average_guns"],
                "single_shipments": day["single_shipments"],
                "single_rate_pct": day["single_rate_pct"],
                "due_orders": day["due_orders"],
                "on_time_orders": day["on_time_orders"],
                "ship_on_time_pct": day["ship_on_time_pct"],
                "first_ship_orders": day["first_ship_orders"],
                "complete_orders": day["complete_orders"],
                "ship_complete_pct": day["ship_complete_pct"],
            }
        )
        cursor += timedelta(days=1)

    late_orders = []
    for order, on_time, shipped_by_due in current["due_order_details"]:
        if on_time:
            continue
        late_orders.append(
            {
                "order_id": order["order_id"],
                "customer_id": order["customer_id"],
                "customer_name": order["customer_name"],
                "promise_ship": order["promise_ship"].isoformat(),
                "ordered_qty": _round_qty(order["order_qty"]),
                "shipped_by_due": _round_qty(shipped_by_due),
                "short_qty": _round_qty(max(0.0, order["order_qty"] - shipped_by_due)),
            }
        )
    late_orders.sort(key=lambda row: (row["promise_ship"], row["order_id"]))

    single_shipments = []
    for packlist in current["packlists"]:
        if abs(packlist["firearm_qty"] - 1.0) > EPSILON:
            continue
        order = None
        if len(packlist["orders"]) == 1:
            order = prepared.orders.get(next(iter(packlist["orders"])))
        if order and order["order_qty"] <= 1.0 + EPSILON:
            classification = "complete one-unit order"
        elif order:
            classification = "partial / consolidation review"
        else:
            classification = "order facts unavailable"
        single_shipments.append(
            {
                "packlist_id": packlist["packlist_id"],
                "ship_date": packlist["ship_date"].isoformat(),
                "order_id": order["order_id"] if order else None,
                "customer_id": packlist["customer_id"] or (order or {}).get("customer_id"),
                "customer_name": packlist["customer_name"] or (order or {}).get("customer_name"),
                "classification": classification,
            }
        )
    single_shipments.sort(key=lambda row: (row["ship_date"], row["packlist_id"]), reverse=True)

    shipment_order_ids = {
        row.order_id.upper() for row in prepared.shipments if row.order_id
    }
    as_of_value = as_of or datetime.now(timezone.utc)
    return {
        "period": {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "days": span.days,
            "prior_start": prior_start.isoformat(),
            "prior_end_exclusive": start.isoformat(),
            "latest_complete_date": (end - timedelta(days=1)).isoformat(),
        },
        "as_of": as_of_value.isoformat(),
        "cards": cards,
        "trends": trends,
        "actions": {
            "late_orders": late_orders,
            "single_shipments": single_shipments,
        },
        "coverage": {
            "orders": len(prepared.orders),
            "on_time_eligible_orders": current["due_orders"],
            "missing_promise_orders": len(current["missing_promise_orders"]),
            "first_ship_orders": current["first_ship_orders"],
            "shipment_orders_without_order_facts": len(shipment_order_ids - set(prepared.orders)),
            "shipment_rows": len(prepared.shipments),
            "duplicate_shipment_rows_ignored": prepared.duplicate_shipment_rows,
        },
        "source": {
            "shipment_grain": "VISUAL packlist",
            "firearm_measure": "serialized trace units",
            "on_time_commitment": "line Promise Ship with header fallback",
            "ship_complete_scope": "all physical order lines with a part",
        },
    }
