"""Classify component demand the picklist cannot fulfil.

Pure module: takes the line-level rows from sql/shortage_components.sql and
returns a JSON-safe dict. No Flask, no database access — unit-testable with
plain lists of dicts.

The picklist allocates bin supply to demand FIFO by desired ship date
(past due first). This module replays that allocation per part so each line
knows how much of it will actually print, then classifies the remainder:

  needs_transfer — short, but MAIN / other-location stock covers the whole
                   shortfall. These feed the transfer move list.
  partial        — some of the shortfall is coverable by transfer, the rest
                   is a true stockout.
  stockout       — no stock anywhere in the building. Production/purchasing
                   signal, not a shipping action.

A missing ship-to no longer changes the allocation (the picklist prints
those lines too); shipto_missing is carried per line purely as a "fix the
order before it can actually ship" hint.

Lines that will print in full never appear in the shortage list; their units
show up in summary.will_print_units for context.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

_REASON_ORDER = {"needs_transfer": 0, "partial": 1, "stockout": 2}


def _is_missing(value: Any) -> bool:
    """None, NaN, or NaT — pandas hands NULL columns over as NaN/NaT, not None."""
    if value is None:
        return True
    try:
        return value != value  # NaN/NaT are the only values unequal to themselves
    except Exception:  # noqa: BLE001 — exotic types compare weirdly; treat as present
        return False


def _int(value: Any) -> int:
    if _is_missing(value):
        return 0
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


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


def _text(value: Any) -> Optional[str]:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _fifo_key(line: dict, today: date):
    desired = line["desired_ship_date"] or today
    return (
        0 if desired < today else 1,
        desired,
        line["order_date"] or date.min,
        line["cust_order_id"] or "",
        line["line_no"] or 0,
    )


def build_shortage(rows: list[dict], lookahead_days: int) -> dict:
    """The shortage payload for the shipping page / API."""
    today = date.today()
    lines: list[dict] = []
    for row in rows:
        if row.get("TODAY") is not None:
            today = _as_date(row["TODAY"]) or today
        lines.append({
            "cust_order_id": _text(row.get("CUST_ORDER_ID")),
            "line_no": _int(row.get("LINE_NO")),
            "customer_id": _text(row.get("CUSTOMER_ID")),
            "customer_name": _text(row.get("CUSTOMER_NAME")),
            "shipto_missing": _text(row.get("SHIPTO_ID")) is None,
            "part_id": _text(row.get("PART_ID")),
            "product_code": _text(row.get("PRODUCT_CODE")),
            "part_description": _text(row.get("PART_DESCRIPTION")),
            "open_qty": _int(row.get("OPEN_QTY")),
            "order_date": _as_date(row.get("ORDER_DATE")),
            "desired_ship_date": _as_date(row.get("DESIRED_SHIP_DATE")),
            "picklist_qty": _int(row.get("PICKLIST_QTY")),
            "main_qty": _int(row.get("MAIN_QTY")),
            "other_qty": _int(row.get("OTHER_QTY")),
            "stock_locations": _text(row.get("STOCK_LOCATIONS")),
        })

    by_part: dict[str, list[dict]] = {}
    for line in lines:
        if line["part_id"]:
            by_part.setdefault(line["part_id"], []).append(line)

    shortage_lines: list[dict] = []
    transfers: dict[str, dict] = {}
    summary = {
        "lookahead_days": lookahead_days,
        "demand_lines": len(lines),
        "demand_units": sum(l["open_qty"] for l in lines),
        "will_print_units": 0,
        "short_units": 0,
        "transfer_units": 0,
        "stockout_units": 0,
        "short_lines": 0,
    }

    for part_id, part_lines in by_part.items():
        part_lines.sort(key=lambda l: _fifo_key(l, today))
        remaining_bins = part_lines[0]["picklist_qty"]
        transfer_pool = part_lines[0]["main_qty"] + part_lines[0]["other_qty"]

        # Pass 1 — replay the picklist: bin supply goes FIFO across the lines.
        for line in part_lines:
            alloc = min(line["open_qty"], remaining_bins)
            remaining_bins -= alloc
            line["will_print_qty"] = alloc
            summary["will_print_units"] += alloc

        # Pass 2 — classify each shortfall against the transfer pool.
        for line in part_lines:
            short = line["open_qty"] - line["will_print_qty"]
            if short <= 0:
                continue

            coverable = min(short, transfer_pool)
            transfer_pool -= coverable
            uncovered = short - coverable

            if uncovered == 0:
                reason = "needs_transfer"
            elif coverable > 0:
                reason = "partial"
            else:
                reason = "stockout"

            summary["short_units"] += short
            summary["short_lines"] += 1
            summary["transfer_units"] += coverable
            summary["stockout_units"] += uncovered

            if coverable > 0:
                entry = transfers.setdefault(part_id, {
                    "part_id": part_id,
                    "part_description": line["part_description"],
                    "product_code": line["product_code"],
                    "qty_needed": 0,
                    "stock_locations": line["stock_locations"],
                })
                entry["qty_needed"] += coverable

            desired = line["desired_ship_date"]
            shortage_lines.append({
                "reason": reason,
                "cust_order_id": line["cust_order_id"],
                "line_no": line["line_no"],
                "customer_id": line["customer_id"],
                "customer_name": line["customer_name"],
                "shipto_missing": line["shipto_missing"],
                "part_id": part_id,
                "product_code": line["product_code"],
                "part_description": line["part_description"],
                "open_qty": line["open_qty"],
                "will_print_qty": line["will_print_qty"],
                "short_qty": short,
                "transfer_qty": coverable,
                "stockout_qty": uncovered,
                "desired_ship_date": desired.isoformat() if desired else None,
                "past_due": bool(desired and desired < today),
                "stock_locations": line["stock_locations"],
            })

    shortage_lines.sort(key=lambda l: (
        _REASON_ORDER.get(l["reason"], 9),
        not l["past_due"],
        l["desired_ship_date"] or "",
        l["customer_id"] or "",
        l["cust_order_id"] or "",
        l["line_no"],
    ))
    transfer_list = sorted(transfers.values(), key=lambda t: -t["qty_needed"])

    return {
        "today": today.isoformat(),
        "summary": summary,
        "lines": shortage_lines,
        "transfers": transfer_list,
    }
