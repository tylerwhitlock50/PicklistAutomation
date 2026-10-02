"""Shipment and tracking helpers (pure).

Takes rows from sql/order_shipments.sql / sql/shipments_lookup.sql (one per
packlist line) and groups them into packlists with tracking numbers, carrier
links, serials and voided flags. Also builds the daily "shipped + tracking"
digest the Teams notifier posts so nobody has to type a tracking number into
a chat again.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Optional

VOID_STATUSES = {"X", "V"}
_UPS_RE = re.compile(r"^1Z[0-9A-Z]{16}$", re.IGNORECASE)
_FEDEX_RE = re.compile(r"^\d{12}$|^\d{15}$|^\d{20}$|^\d{22}$")
_USPS_RE = re.compile(r"^(9[2345]\d{20,24})$")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else number


def _get(row: Mapping[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    lower = key.lower()
    for candidate, value in row.items():
        if str(candidate).lower() == lower:
            return value
    return None


def _date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = _text(value)
    return text[:10] if text else None


def _datetime_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="minutes")
    text = _text(value)
    return text[:16] if text else None


def split_list(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    seen: list[str] = []
    for item in re.split(r"[,\s]+", text):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def carrier_for(tracking: str) -> Optional[str]:
    number = (tracking or "").strip().replace(" ", "")
    if _UPS_RE.match(number):
        return "UPS"
    if _USPS_RE.match(number):
        return "USPS"
    if _FEDEX_RE.match(number):
        return "FedEx"
    return None


def tracking_url(tracking: str) -> Optional[str]:
    number = (tracking or "").strip().replace(" ", "")
    carrier = carrier_for(number)
    if carrier == "UPS":
        return f"https://www.ups.com/track?loc=en_US&tracknum={number}"
    if carrier == "FedEx":
        return f"https://www.fedex.com/fedextrack/?trknbr={number}"
    if carrier == "USPS":
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={number}"
    return None


def tracking_numbers(row: Mapping[str, Any]) -> list[str]:
    """Z_UPS_SHIPMENTS numbers first; the packlist UDF only when none exist."""
    numbers = [n for n in split_list(_get(row, "TRACKING_NUMBERS")) if n != "0"]
    if numbers:
        return numbers
    fallback = _text(_get(row, "UDF_TRACKING_NUMBER"))
    return [fallback] if fallback and fallback != "0" else []


def group_packlists(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse packlist-line rows into packlists (newest ship date first)."""
    packlists: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for row in rows:
        packlist_id = _text(_get(row, "PACKLIST_ID"))
        if not packlist_id:
            continue
        status = _text(_get(row, "SHIPPER_STATUS")).upper()
        entry = packlists.get(packlist_id)
        if entry is None:
            numbers = tracking_numbers(row)
            entry = {
                "packlist_id": packlist_id,
                "order_id": _text(_get(row, "CUST_ORDER_ID")).upper(),
                "customer_id": _text(_get(row, "CUSTOMER_ID")),
                "customer_name": _text(_get(row, "CUSTOMER_NAME")),
                "created": _datetime_text(_get(row, "PACKLIST_CREATED")),
                "shipped_date": _date_text(_get(row, "SHIPPED_DATE")),
                "status": status,
                "voided": status in VOID_STATUSES,
                "shipped": status not in VOID_STATUSES and bool(_date_text(_get(row, "SHIPPED_DATE"))),
                "ship_via": _text(_get(row, "SHIP_VIA")),
                "invoice_id": _text(_get(row, "INVOICE_ID")),
                "waybill": _text(_get(row, "WAYBILL_NUMBER")),
                "tracking": [
                    {"number": number, "carrier": carrier_for(number), "url": tracking_url(number)}
                    for number in numbers
                ],
                "tracking_source": "ups" if split_list(_get(row, "TRACKING_NUMBERS")) else ("udf" if numbers else None),
                "lines": [],
                "units": 0.0,
                "serials": [],
            }
            packlists[packlist_id] = entry
        qty = _num(_get(row, "SHIPPED_QTY"))
        serials = split_list(_get(row, "SERIALS"))
        entry["lines"].append(
            {
                "line_no": _text(_get(row, "LINE_NO")),
                "order_line_no": _text(_get(row, "CUST_ORDER_LINE_NO")),
                "part_id": _text(_get(row, "PART_ID")).upper(),
                "product_code": _text(_get(row, "PRODUCT_CODE")),
                "description": _text(_get(row, "PART_DESCRIPTION")),
                "qty": qty,
                "serials": serials,
            }
        )
        entry["units"] += qty
        for serial in serials:
            if serial not in entry["serials"]:
                entry["serials"].append(serial)
    ordered = sorted(
        packlists.values(),
        key=lambda p: (p["shipped_date"] or "9999-99-99", p["packlist_id"]),
        reverse=True,
    )
    return ordered


def summarize(packlists: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(packlists)
    live = [p for p in items if not p["voided"]]
    shipped = [p for p in live if p["shipped"]]
    return {
        "packlists": len(items),
        "voided": len(items) - len(live),
        "shipped_packlists": len(shipped),
        "units_shipped": sum(p["units"] for p in shipped),
        "serials_shipped": sum(len(p["serials"]) for p in shipped),
        "last_shipped": max((p["shipped_date"] for p in shipped if p["shipped_date"]), default=None),
        "tracking": [t for p in shipped for t in p["tracking"]],
        "missing_tracking": [p["packlist_id"] for p in shipped if not p["tracking"]],
    }


def build_shipped_digest(rows: Iterable[Mapping[str, Any]], *, day: dt.date) -> dict[str, Any]:
    """Rows -> one digest payload: table rows for a Teams card plus plain text."""
    packlists = [p for p in group_packlists(rows) if not p["voided"]]
    packlists.sort(key=lambda p: (p["customer_name"] or p["customer_id"], p["order_id"], p["packlist_id"]))
    table: list[list[str]] = []
    for p in packlists:
        tracking = ", ".join(t["number"] for t in p["tracking"]) or "(no tracking yet)"
        table.append([p["order_id"], (p["customer_name"] or p["customer_id"])[:30], p["packlist_id"], tracking, str(int(p["units"]))])
    orders = {p["order_id"] for p in packlists}
    units = sum(p["units"] for p in packlists)
    text_lines = [f"{row[0]} · {row[1]} · {row[2]} · {row[3]} · {row[4]} unit(s)" for row in table]
    return {
        "day": day.isoformat(),
        "packlists": packlists,
        "rows": table,
        "columns": ["Order", "Customer", "Packlist", "Tracking", "Units"],
        "order_count": len(orders),
        "packlist_count": len(packlists),
        "units": units,
        "missing_tracking": [p["packlist_id"] for p in packlists if not p["tracking"]],
        "title": f"Shipped today ({day.isoformat()}): {len(packlists)} packlist{'s' if len(packlists) != 1 else ''}, {len(orders)} order{'s' if len(orders) != 1 else ''}",
        "text": "\n".join(text_lines) if text_lines else "Nothing shipped today.",
        "event_key": f"shipped_digest:{day.isoformat()}",
    }
