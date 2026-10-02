"""Line up a day's picklist plan against what actually shipped.

Pure module: takes the plan snapshot rows (stored by app.py when a picklist
run succeeds) and the shipper-line rows from sql/recon_shipments.sql, and
returns a JSON-safe dict. No Flask, no database access — everything here is
unit-testable with plain lists of dicts.

Matching is by (customer order, part). The picklist allocates at
order+part+location granularity but SHIPPER_LINE has no location, so both
sides are aggregated to order+part before comparison. A plan line's status:

  shipped     — full planned qty left on the plan date
  partial     — some but not all planned qty left on the plan date
  late        — nothing left on the plan date, but it shipped on a later day
                (only observable when reconciling a past date)
  not_shipped — nothing left at all

Shipments on the plan date that match no plan line are reported separately as
"unplanned" — usually orders excluded from the picklist (international, RMA
replacements) or manual ships.

Voided packlists (SHIPPER.STATUS 'X'/'V') never count as shipped qty; a line
whose only activity was voided keeps its not_shipped/late status and carries
voided_qty so the UI can say why.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

VOIDED_SHIPPER_STATUSES = {"X", "V"}

# Column names as they come out of the picklist queries / plan snapshots.
PLAN_ORDER_KEY = "Cust Order ID"
PLAN_PART_KEY = "Part Id"
PLAN_QTY_KEY = "SO Qty"
PLAN_CUSTOMER_KEY = "Customer ID"
PLAN_LOCATION_KEY = "Location"
PLAN_PRODUCT_KEY = "Product code"


def _is_missing(value: Any) -> bool:
    """None, NaN, or NaT — pandas hands NULL columns over as NaN/NaT, not None."""
    if value is None:
        return True
    try:
        return value != value  # NaN/NaT are the only values unequal to themselves
    except Exception:  # noqa: BLE001 — exotic types compare weirdly; treat as present
        return False


def _num(value: Any) -> float:
    if _is_missing(value):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def _as_date(value: Any) -> Optional[date]:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _tracking_list(row: dict) -> list[str]:
    """Carrier tracking for a shipment row — UPS export list first, UDF fallback."""
    ups = _text(row.get("TRACKING_NUMBERS"))
    if ups:
        return [t.strip() for t in ups.split(",") if t.strip()]
    udf = _text(row.get("UDF_TRACKING_NUMBER"))
    if udf:
        return [udf]
    return []


def _aggregate_plan(plans: dict[str, dict]) -> dict[tuple, dict]:
    """Merge every plan's rows to one entry per (order, part)."""
    merged: dict[tuple, dict] = {}
    for query_type, plan in plans.items():
        for row in plan.get("rows") or []:
            order = _text(row.get(PLAN_ORDER_KEY))
            part = _text(row.get(PLAN_PART_KEY))
            if not order or not part:
                continue
            key = (order.upper(), part.upper())
            entry = merged.setdefault(key, {
                "cust_order_id": order,
                "part_id": part,
                "product_code": _text(row.get(PLAN_PRODUCT_KEY)) or None,
                "customer_id": _text(row.get(PLAN_CUSTOMER_KEY)) or None,
                "planned_qty": 0.0,
                "locations": [],
                "types": [],
            })
            entry["planned_qty"] += _num(row.get(PLAN_QTY_KEY))
            loc = _text(row.get(PLAN_LOCATION_KEY))
            if loc and loc not in entry["locations"]:
                entry["locations"].append(loc)
            if query_type not in entry["types"]:
                entry["types"].append(query_type)
            if not entry["customer_id"]:
                entry["customer_id"] = _text(row.get(PLAN_CUSTOMER_KEY)) or None
    return merged


def _aggregate_shipments(shipments: list[dict], plan_date: date) -> dict[tuple, dict]:
    """One entry per (order, part) with same-day / late / voided quantities."""
    merged: dict[tuple, dict] = {}
    for row in shipments:
        order = _text(row.get("CUST_ORDER_ID"))
        part = _text(row.get("PART_ID"))
        if not order:
            continue
        key = (order.upper(), part.upper())
        entry = merged.setdefault(key, {
            "cust_order_id": order,
            "part_id": part or None,
            "product_code": _text(row.get("PRODUCT_CODE")) or None,
            "customer_id": _text(row.get("CUSTOMER_ID")) or None,
            "customer_name": _text(row.get("CUSTOMER_NAME")) or None,
            "same_day_qty": 0.0,
            "late_qty": 0.0,
            "voided_qty": 0.0,
            "packlists": [],
            "tracking": [],
        })
        qty = _num(row.get("SHIPPED_QTY"))
        shipped_on = _as_date(row.get("SHIPPED_DATE"))
        voided = _text(row.get("SHIPPER_STATUS")).upper() in VOIDED_SHIPPER_STATUSES
        if voided:
            entry["voided_qty"] += qty
        elif shipped_on is not None and shipped_on > plan_date:
            entry["late_qty"] += qty
        else:
            entry["same_day_qty"] += qty

        packlist = {
            "packlist_id": row.get("PACKLIST_ID"),
            "shipped_date": shipped_on.isoformat() if shipped_on else None,
            "qty": qty,
            "voided": voided,
            "late": bool(shipped_on and shipped_on > plan_date and not voided),
            "invoice_id": row.get("INVOICE_ID"),
        }
        seen = {(p["packlist_id"], p["shipped_date"]) for p in entry["packlists"]}
        if (packlist["packlist_id"], packlist["shipped_date"]) in seen:
            for existing in entry["packlists"]:
                if (existing["packlist_id"], existing["shipped_date"]) == (
                    packlist["packlist_id"], packlist["shipped_date"]
                ):
                    existing["qty"] += qty
                    break
        else:
            entry["packlists"].append(packlist)
        if not voided:
            for t in _tracking_list(row):
                if t not in entry["tracking"]:
                    entry["tracking"].append(t)
    return merged


def _line_status(planned: float, same_day: float, late: float) -> str:
    if same_day >= planned and planned > 0:
        return "shipped"
    if same_day > 0:
        return "partial"
    if late > 0:
        return "late"
    return "not_shipped"


_STATUS_ORDER = {"not_shipped": 0, "late": 1, "partial": 2, "shipped": 3}


def _empty_bucket() -> dict:
    return {
        "planned_lines": 0,
        "planned_units": 0.0,
        "shipped_units": 0.0,      # same-day units, capped at planned per line
        "late_units": 0.0,
        "lines_shipped": 0,
        "lines_partial": 0,
        "lines_late": 0,
        "lines_not_shipped": 0,
    }


def _accumulate(bucket: dict, line: dict) -> None:
    bucket["planned_lines"] += 1
    bucket["planned_units"] += line["planned_qty"]
    bucket["shipped_units"] += min(line["shipped_same_day"], line["planned_qty"])
    bucket["late_units"] += line["shipped_late"]
    bucket[f"lines_{line['status']}"] += 1


def _finish_bucket(bucket: dict) -> dict:
    planned = bucket["planned_units"]
    bucket["fill_rate_pct"] = (
        round(100.0 * bucket["shipped_units"] / planned, 1) if planned else None
    )
    return bucket


def build_reconciliation(
    plan_date: date,
    plans: dict[str, dict],
    shipments: list[dict],
) -> dict:
    """The full reconciliation payload for one plan date.

    plans: {query_type: {"run_id": int, "run_timestamp": str, "rows": [dict]}}
    shipments: rows from sql/recon_shipments.sql covering plan_date .. today.
    """
    plan_lines = _aggregate_plan(plans)
    shipped = _aggregate_shipments(shipments, plan_date)

    lines: list[dict] = []
    for key, plan in plan_lines.items():
        ship = shipped.get(key)
        same_day = ship["same_day_qty"] if ship else 0.0
        late = ship["late_qty"] if ship else 0.0
        status = _line_status(plan["planned_qty"], same_day, late)
        lines.append({
            "cust_order_id": plan["cust_order_id"],
            "part_id": plan["part_id"],
            "product_code": plan["product_code"] or (ship or {}).get("product_code"),
            "customer_id": plan["customer_id"] or (ship or {}).get("customer_id"),
            "customer_name": (ship or {}).get("customer_name"),
            "types": plan["types"],
            "locations": plan["locations"],
            "planned_qty": plan["planned_qty"],
            "shipped_same_day": same_day,
            "shipped_late": late,
            "shipped_total": same_day + late,
            "over_shipped": max(0.0, same_day + late - plan["planned_qty"]),
            "voided_qty": ship["voided_qty"] if ship else 0.0,
            "status": status,
            "packlists": ship["packlists"] if ship else [],
            "tracking": ship["tracking"] if ship else [],
        })
    lines.sort(key=lambda l: (
        _STATUS_ORDER.get(l["status"], 9),
        l["customer_id"] or "",
        l["cust_order_id"],
        l["part_id"],
    ))

    plan_keys = set(plan_lines)
    unplanned: list[dict] = []
    for key, ship in shipped.items():
        if key in plan_keys:
            continue
        # Only same-day activity is "shipped but not on the picklist" — later
        # days belong to those days' own reconciliations.
        if ship["same_day_qty"] <= 0:
            continue
        unplanned.append({
            "cust_order_id": ship["cust_order_id"],
            "part_id": ship["part_id"],
            "product_code": ship["product_code"],
            "customer_id": ship["customer_id"],
            "customer_name": ship["customer_name"],
            "qty": ship["same_day_qty"],
            "packlists": [p for p in ship["packlists"] if not p["voided"] and not p["late"]],
            "tracking": ship["tracking"],
        })
    unplanned.sort(key=lambda u: (u["customer_id"] or "", u["cust_order_id"], u["part_id"] or ""))

    overall = _empty_bucket()
    by_type: dict[str, dict] = {qt: _empty_bucket() for qt in plans}
    for line in lines:
        _accumulate(overall, line)
        for qt in line["types"]:
            if qt in by_type:
                _accumulate(by_type[qt], line)
    _finish_bucket(overall)
    for bucket in by_type.values():
        _finish_bucket(bucket)
    overall["unplanned_lines"] = len(unplanned)
    overall["unplanned_units"] = sum(u["qty"] for u in unplanned)

    return {
        "plan_date": plan_date.isoformat(),
        "plans": {
            qt: {
                "run_id": plan.get("run_id"),
                "run_timestamp": plan.get("run_timestamp"),
                "row_count": len(plan.get("rows") or []),
            }
            for qt, plan in plans.items()
        },
        "summary": {**overall, "by_type": by_type},
        "lines": lines,
        "unplanned": unplanned,
    }
