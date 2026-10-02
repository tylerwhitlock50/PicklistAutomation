"""Stock lookup payload (pure): where a part is, by bin class, with serials,
and how much of it is already spoken for (allocated, reserved, held)."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

import readiness
import shipments as _ship

SERIALS_PER_BIN = 50


def _text(value: Any) -> str:
    return _ship._text(value)


def _num(value: Any) -> float:
    return _ship._num(value)


def _get(row: Mapping[str, Any], key: str) -> Any:
    return _ship._get(row, key)


def build_stock_payload(
    part_id: str,
    *,
    location_rows: Iterable[Mapping[str, Any]],
    allocation: Optional[Mapping[str, Any]] = None,
    reservations: Iterable[Mapping[str, Any]] = (),
    manual_holds: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    part = (part_id or "").strip().upper()
    bins: list[dict[str, Any]] = []
    description = ""
    product_code = ""
    by_class: dict[str, dict[str, Any]] = {}
    for row in location_rows:
        description = description or _text(_get(row, "PART_DESCRIPTION"))
        product_code = product_code or _text(_get(row, "PRODUCT_CODE"))
        warehouse = _text(_get(row, "WAREHOUSE_ID"))
        location = _text(_get(row, "LOCATION_ID"))
        qty = _num(_get(row, "QTY"))
        if qty <= 0:
            continue
        kind = readiness.classify_bin(warehouse, location)
        serials = _ship.split_list(_get(row, "SERIALS"))
        serial_count = int(_num(_get(row, "SERIAL_COUNT"))) or len(serials)
        entry = {
            "warehouse": warehouse,
            "location": location,
            "qty": qty,
            "class": kind,
            "class_label": readiness.BIN_CLASS_LABELS.get(kind, kind),
            "serials": serials[:SERIALS_PER_BIN],
            "serial_count": serial_count,
            "serials_truncated": serial_count > SERIALS_PER_BIN,
            "oldest_transaction_at": _ship._datetime_text(_get(row, "OLDEST_TRANSACTION_AT")),
        }
        bins.append(entry)
        bucket = by_class.setdefault(kind, {"class": kind, "label": entry["class_label"], "qty": 0.0, "bins": 0, "serials": 0})
        bucket["qty"] += qty
        bucket["bins"] += 1
        bucket["serials"] += serial_count
    order = ["pickable", "stage", "rack10", "r11_components", "international", "shipping_other", "distribution", "distribution_stock", "main", "other"]
    bins.sort(key=lambda b: (order.index(b["class"]) if b["class"] in order else 99, b["warehouse"], b["location"]))
    classes = sorted(by_class.values(), key=lambda c: order.index(c["class"]) if c["class"] in order else 99)
    pickable_qty = by_class.get("pickable", {}).get("qty", 0.0)

    # --- already spoken for
    allocated_to: list[dict[str, Any]] = []
    allocated_qty = 0.0
    if allocation and not allocation.get("error"):
        demand = allocation.get("demand") or {}
        lines = demand.get("lines", []) if isinstance(demand, dict) else list(demand)
        for line in lines:
            on_hand = 0.0
            for alloc in line.get("allocations", []) or []:
                if _text(alloc.get("class")).upper() == "ON_HAND":
                    on_hand += _num(alloc.get("qty"))
            if on_hand <= 0:
                continue
            allocated_qty += on_hand
            dates = line.get("dates") or {}
            allocated_to.append(
                {
                    "order_id": _text(line.get("so") or line.get("order_id")).upper(),
                    "line_no": _text(line.get("line_no")),
                    "customer_id": _text(line.get("customer_id")),
                    "customer_name": _text(line.get("customer_name")),
                    "qty": on_hand,
                    "position": line.get("position"),
                    "promise": _text(dates.get("eff_promise_del") or dates.get("eff_promise_ship")),
                    "status": _text(line.get("supply_status")),
                    "in_picklist_window": bool(line.get("in_picklist_window")),
                }
            )
    reserved: list[dict[str, Any]] = []
    for row in reservations:
        if _text(_get(row, "part_id")).upper() != part:
            continue
        if _text(_get(row, "status")).lower() not in {"", "active"}:
            continue
        reserved.append(
            {
                "serial_no": _text(_get(row, "serial_no")),
                "order_id": _text(_get(row, "cust_order_id")).upper(),
                "customer_id": _text(_get(row, "customer_id")),
                "since": _ship._datetime_text(_get(row, "first_assigned_at")),
                "location": _text(_get(row, "location_id")),
            }
        )
    held_orders = {_text(_get(h, "cust_order_id")).upper() for h in manual_holds if _text(_get(h, "cust_order_id"))}
    held_for = [a for a in allocated_to if a["order_id"] in held_orders]

    free_qty = max(0.0, pickable_qty - allocated_qty)
    return {
        "part_id": part,
        "description": description,
        "product_code": product_code,
        "found": bool(bins),
        "bins": bins,
        "classes": classes,
        "pickable_qty": pickable_qty,
        "total_qty": sum(b["qty"] for b in bins),
        "allocated_qty": allocated_qty,
        "allocated_to": allocated_to,
        "reserved_serials": reserved,
        "held_for": held_for,
        "free_qty": free_qty,
        "allocation_error": allocation.get("error") if allocation else None,
    }


def serial_locations_payload(serial: str, locations: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, Any]:
    """Shape fetch_serial_onhand_locations output for the stock page."""
    key = (serial or "").strip().upper()
    rows = list(locations.get(key) or locations.get(serial) or [])
    places = []
    for row in rows:
        warehouse = _text(_get(row, "WAREHOUSE_ID") or _get(row, "warehouse_id"))
        location = _text(_get(row, "LOCATION_ID") or _get(row, "location_id"))
        kind = readiness.classify_bin(warehouse, location)
        places.append(
            {
                "part_id": _text(_get(row, "PART_ID") or _get(row, "part_id")).upper(),
                "warehouse": warehouse,
                "location": location,
                "qty": _num(_get(row, "QTY") or _get(row, "qty") or _get(row, "NET_QTY")),
                "class": kind,
                "class_label": readiness.BIN_CLASS_LABELS.get(kind, kind),
            }
        )
    return {"serial": key, "found": bool(places), "locations": places}
