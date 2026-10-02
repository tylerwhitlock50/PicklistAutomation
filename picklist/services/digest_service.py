"""Shipped-orders Teams digest."""
from datetime import date, timedelta
from typing import Any, Optional

from picklist.domain import notifier, shipments
from picklist.services.orders_service import fetch_shipment_lookup_rows
from picklist.timeutil import _today_local


def build_shipped_digest_payload(day: Optional[date] = None) -> dict[str, Any]:
    day = day or _today_local()
    rows = fetch_shipment_lookup_rows(day.isoformat(), (day + timedelta(days=1)).isoformat())
    return shipments.build_shipped_digest(rows, day=day)


def send_shipped_digest(day: Optional[date] = None, *, force: bool = False) -> dict[str, Any]:
    payload = build_shipped_digest_payload(day)
    result = {
        "day": payload["day"],
        "packlists": payload["packlist_count"],
        "orders": payload["order_count"],
        "missing_tracking": payload["missing_tracking"],
        "sent": False,
        "reason": None,
    }
    if payload["packlist_count"] == 0 and not force:
        result["reason"] = "nothing shipped"
        return result
    missing = payload["missing_tracking"]
    footer = (
        f"{len(missing)} packlist{'s' if len(missing) != 1 else ''} still without a tracking number: "
        + ", ".join(missing[:10])
        + (" ..." if len(missing) > 10 else "")
        if missing
        else None
    )
    chunks = notifier.chunk_rows(payload["rows"])
    sent_all = True
    for index, chunk in enumerate(chunks):
        suffix = f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        sent = notifier.send_teams_notification(
            "shipped_digest",
            title=payload["title"] + suffix,
            text=None if chunk else "Nothing shipped today.",
            rows=chunk or None,
            columns=payload["columns"] if chunk else None,
            link=notifier.public_url("/shipments"),
            footer=footer if index == len(chunks) - 1 else None,
            event_key=payload["event_key"] + (f":part{index + 1}" if index else ""),
            force=force,
        )
        sent_all = sent_all and bool(sent)
    result["sent"] = sent_all
    result["cards"] = len(chunks)
    if not sent:
        result["reason"] = "not delivered (see notification log)"
    return result
