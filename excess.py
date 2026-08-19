"""Price the duplicate-packlist habit so the dashboard can shame it away.

Pure module: takes the SHIPPER header rows from sql/excess_packlists.sql and
returns a JSON-safe dict. No Flask, no database access — unit-testable with
plain lists of dicts.

The rule: for a given (sales order, ship day), every packlist beyond the
first is "excess" — an extra box that incurred its own carrier charge
(cost_usd each). Packlists for the same order on different days are NOT
excess; splitting across days can be legitimate backorder behavior, and the
metric stays deliberately narrow so nobody can argue with it.

Two lenses on the same rows:

  shipped groups — money already spent, bucketed today / rolling 7 days /
                   calendar month-to-date (month-to-date so the number
                   reconciles against the monthly carrier invoice; rolling
                   week so Mondays don't read as a fresh start).
  fixable groups — packlists created today and not yet shipped that
                   duplicate another packlist on the same order today.
                   These can still be consolidated before pickup.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional


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


def _iso(value: Any) -> Optional[str]:
    parsed = _as_date(value)
    return parsed.isoformat() if parsed else None


def _text(value: Any) -> Optional[str]:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _packlist_entry(row: dict) -> dict:
    return {
        "packlist_id": _text(row.get("PACKLIST_ID")),
        "created": _iso(row.get("CREATE_DATE")),
        "shipped": _iso(row.get("SHIPPED_DATE")),
        "line_count": _int(row.get("LINE_COUNT")),
        "serial_count": _int(row.get("SERIAL_COUNT")),
        "ship_via": _text(row.get("SHIP_VIA")),
    }


def _group_entry(
    order_id: str,
    members: list[dict],
    ship_date: Optional[date],
    cost_usd: float,
) -> dict:
    first = members[0]
    excess = len(members) - 1
    return {
        "order_id": order_id,
        "customer_id": _text(first.get("CUSTOMER_ID")),
        "customer_name": _text(first.get("CUSTOMER_NAME")),
        "ship_date": ship_date.isoformat() if ship_date else None,
        "packlist_count": len(members),
        "excess_count": excess,
        "excess_cost": round(excess * cost_usd, 2),
        "packlists": [_packlist_entry(row) for row in members],
    }


def build_excess(rows: list[dict], today: date, cost_usd: float) -> dict:
    """The excess-packlist payload for the dashboard banner / shipping view."""
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    shipped_by_order_day: dict[tuple[str, date], list[dict]] = {}
    today_by_order: dict[str, list[dict]] = {}
    ignored_no_order = 0

    for row in rows:
        order_id = _text(row.get("CUST_ORDER_ID"))
        if not order_id:
            ignored_no_order += 1
            continue
        shipped_on = _as_date(row.get("SHIPPED_DATE"))
        if shipped_on is not None:
            shipped_by_order_day.setdefault((order_id, shipped_on), []).append(row)
            if shipped_on == today:
                today_by_order.setdefault(order_id, []).append(row)
        elif _as_date(row.get("CREATE_DATE")) == today:
            today_by_order.setdefault(order_id, []).append(row)
        # Unshipped rows created before today are invisible until they ship.

    summary = {
        "today": {"excess_count": 0, "excess_cost": 0.0, "orders": 0},
        "week": {"excess_count": 0, "excess_cost": 0.0, "orders": 0},
        "month": {"excess_count": 0, "excess_cost": 0.0, "orders": 0},
        "fixable_count": 0,
        "fixable_savings": 0.0,
        "cost_per_excess": round(cost_usd, 2),
        "ignored_no_order": ignored_no_order,
    }

    groups: list[dict] = []
    for (order_id, ship_date), members in shipped_by_order_day.items():
        if len(members) < 2:
            continue
        group = _group_entry(order_id, members, ship_date, cost_usd)
        groups.append(group)
        buckets = ["month"] if ship_date >= month_start else []
        if ship_date >= week_start:
            buckets.append("week")
        if ship_date == today:
            buckets.append("today")
        for bucket in buckets:
            summary[bucket]["excess_count"] += group["excess_count"]
            summary[bucket]["excess_cost"] += group["excess_cost"]
            summary[bucket]["orders"] += 1
    groups.sort(key=lambda g: (g["ship_date"] or "", g["order_id"]), reverse=True)

    fixable: list[dict] = []
    for order_id, members in sorted(today_by_order.items()):
        if len(members) < 2:
            continue
        unshipped = [m for m in members if _is_missing(m.get("SHIPPED_DATE"))]
        if not unshipped:
            continue  # already all shipped — counted above, nothing to fix
        group = _group_entry(order_id, members, today, cost_usd)
        fixable_count = min(len(unshipped), len(members) - 1)
        group["fixable_count"] = fixable_count
        group["fixable_savings"] = round(fixable_count * cost_usd, 2)
        fixable.append(group)
        summary["fixable_count"] += fixable_count
        summary["fixable_savings"] += group["fixable_savings"]

    for bucket in ("today", "week", "month"):
        summary[bucket]["excess_cost"] = round(summary[bucket]["excess_cost"], 2)
    summary["fixable_savings"] = round(summary["fixable_savings"], 2)

    return {
        "summary": summary,
        "fixable": fixable,
        "groups": groups,
        "window": {
            "week_start": week_start.isoformat(),
            "month_start": month_start.isoformat(),
            "today": today.isoformat(),
        },
    }
