"""Orchestration for order readiness: fetch facts, evaluate, persist, notify.

app.py injects everything that touches its own globals (ERP readers, run
history, release gate, pick store, notifier) through ``configure`` so this
module stays importable and testable without Flask.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Callable, Iterable, Optional

import readiness
import readiness_store
import shipments

_logger = logging.getLogger("picklist-app.readiness")
_lock = threading.Lock()
_last_refresh: dict[str, Any] = {"at": None, "payload": None}

_deps: dict[str, Any] = {
    "fetch_candidates": None,          # () -> list[dict]
    "fetch_order_rows": None,          # (so) -> list[dict]
    "fetch_order_locations": None,     # (so) -> list[dict]
    "picklist_orders_today": None,     # () -> set[str] | None
    "picklist_horizon": None,          # () -> date | None
    "gate_decisions": None,            # () -> dict[order_id, decision]
    "manual_holds": None,              # () -> list[dict]
    "doc_findings": None,              # (orders) -> dict[order_id, list[finding]]
    "pick_status": None,               # (so) -> dict | None
    "fetch_order_shipments": None,     # (so) -> list[dict]
    "today": None,                     # () -> date
    "config": None,                    # () -> dict
    "notify": None,                    # (event, **card) -> bool
    "public_url": None,                # (path) -> str | None
    "cache_seconds": 300,
    "notify_row_cap": 25,
    "notify_summary_threshold": 50,
    "logger": None,
}


def configure(**deps: Any) -> None:
    unknown = set(deps) - set(_deps)
    if unknown:
        raise ValueError(f"unknown readiness_service deps: {sorted(unknown)}")
    _deps.update(deps)
    if deps.get("logger") is not None:
        global _logger  # noqa: PLW0603
        _logger = deps["logger"]


def _call(name: str, *args: Any, default: Any = None) -> Any:
    fn = _deps.get(name)
    if fn is None:
        return default
    return fn(*args)


def _today() -> dt.date:
    today = _call("today")
    return today if isinstance(today, dt.date) else dt.date.today()


# --------------------------------------------------------------------------- refresh


def refresh(trigger: str = "manual", *, force: bool = False) -> dict[str, Any]:
    """Fetch, evaluate, reconcile holds, notify, and save a snapshot.

    Returns the enriched payload. Never raises for ERP failures: the payload
    carries ``error`` and the last good snapshot stays in place.
    """
    cache_seconds = int(_deps.get("cache_seconds") or 0)
    with _lock:
        last_at = _last_refresh["at"]
        if (
            not force
            and last_at is not None
            and _last_refresh["payload"] is not None
            and (dt.datetime.now(dt.timezone.utc) - last_at).total_seconds() < cache_seconds
        ):
            return _last_refresh["payload"]

        evaluated_at = dt.datetime.now(dt.timezone.utc)
        try:
            rows = _call("fetch_candidates", default=[]) or []
        except Exception as exc:  # noqa: BLE001 - surface as payload error
            _logger.exception("Readiness candidate query failed")
            payload = {
                "evaluated_at": evaluated_at.isoformat(),
                "today": _today().isoformat(),
                "orders": [],
                "holds": [],
                "summary": {"orders": 0, "holds": 0, "blocked": 0, "attention": 0, "ready": 0,
                            "by_reason": {}, "by_owner": {}},
                "error": str(exc),
                "trigger": trigger,
            }
            try:
                readiness_store.save_snapshot(payload, trigger=trigger, error=str(exc))
            except Exception:  # noqa: BLE001
                _logger.exception("Could not save failed readiness snapshot")
            _last_refresh.update({"at": evaluated_at, "payload": payload})
            return payload

        gate = _safe("gate_decisions", default={}) or {}
        picklist_orders = _safe("picklist_orders_today")
        horizon = _safe("picklist_horizon")
        manual = _safe("manual_holds", default=[]) or []
        config = _safe("config", default={}) or {}

        payload = readiness.evaluate_readiness(
            rows,
            today=_today(),
            gate_decisions=gate,
            picklist_orders=picklist_orders,
            picklist_horizon=horizon,
            manual_holds=manual,
            doc_findings=None,
            config=config,
            evaluated_at=evaluated_at,
        )
        # Tier 2 (document OCR) runs after tier 1 so it only looks at orders
        # that have nothing else blocking them.
        findings_fn = _deps.get("doc_findings")
        if findings_fn is not None:
            try:
                findings = findings_fn(payload["orders"]) or {}
            except Exception:  # noqa: BLE001
                _logger.exception("FFL document findings failed; continuing with tier 1 only")
                findings = {}
            if findings:
                payload = readiness.evaluate_readiness(
                    rows,
                    today=_today(),
                    gate_decisions=gate,
                    picklist_orders=picklist_orders,
                    picklist_horizon=horizon,
                    manual_holds=manual,
                    doc_findings=findings,
                    config=config,
                    evaluated_at=evaluated_at,
                )

        stamp = payload["evaluated_at"]
        for hold in payload["holds"]:
            order = next((o for o in payload["orders"] if o["order_id"] == hold["order_id"]), None)
            if order:
                hold["customer_id"] = order["customer_id"]
                hold["customer_name"] = order["customer_name"]
        try:
            outcome = readiness_store.reconcile_holds(
                payload["holds"], evaluated_at=stamp,
                seen_orders=[o["order_id"] for o in payload["orders"]],
            )
        except Exception:  # noqa: BLE001
            _logger.exception("Hold reconciliation failed")
            outcome = {"new": [], "cleared": [], "kept": 0, "ids": {}}

        _attach_hold_ids(payload, outcome.get("ids") or {})
        payload["error"] = None
        payload["trigger"] = trigger
        payload["reconcile"] = {
            "new": len(outcome.get("new") or []),
            "cleared": len(outcome.get("cleared") or []),
            "kept": outcome.get("kept", 0),
        }

        try:
            snapshot_id = readiness_store.save_snapshot(payload, trigger=trigger, source_as_of=stamp)
            payload["snapshot_id"] = snapshot_id
            readiness_store.prune_snapshots(keep=200)
        except Exception:  # noqa: BLE001
            _logger.exception("Could not save readiness snapshot")
            payload["snapshot_id"] = None

        try:
            _notify(payload, outcome)
        except Exception:  # noqa: BLE001
            _logger.exception("Readiness notification failed")

        _last_refresh.update({"at": evaluated_at, "payload": payload})
        return payload


def _safe(name: str, *args: Any, default: Any = None) -> Any:
    try:
        return _call(name, *args, default=default)
    except Exception:  # noqa: BLE001 - optional inputs never block a refresh
        _logger.exception("Readiness input '%s' failed; continuing without it", name)
        return default


def _attach_hold_ids(payload: dict[str, Any], ids: dict[tuple[str, str, str], int]) -> None:
    for hold in payload.get("holds", []):
        key = (hold["order_id"], str(hold.get("line_no") or ""), hold["reason_code"])
        hold["id"] = ids.get(key)
    for order in payload.get("orders", []):
        for hold in order.get("holds", []):
            key = (hold["order_id"], str(hold.get("line_no") or ""), hold["reason_code"])
            hold["id"] = ids.get(key)


def _notify(payload: dict[str, Any], outcome: dict[str, Any]) -> None:
    notify = _deps.get("notify")
    if notify is None:
        return
    new = outcome.get("new") or []
    cleared = outcome.get("cleared") or []
    if not new and not cleared:
        return
    snapshot_id = payload.get("snapshot_id") or payload.get("evaluated_at")
    link = _call("public_url", "/orders")
    cap = int(_deps.get("notify_row_cap") or 25)
    threshold = int(_deps.get("notify_summary_threshold") or 50)

    if new:
        by_owner: dict[str, int] = {}
        for hold in new:
            by_owner[hold.get("owner_team", "")] = by_owner.get(hold.get("owner_team", ""), 0) + 1
        facts = [(readiness.OWNER_LABELS.get(team, team or "Unassigned"), count) for team, count in sorted(by_owner.items())]
        blocking = [h for h in new if h.get("blocking")]
        if len(new) > threshold:
            rows = None
            text = f"{len(new)} new holds appeared ({len(blocking)} blocking). Open the Orders page for the full list."
        else:
            rows = [
                [h["order_id"], (h.get("customer_name") or h.get("customer_id") or "")[:28],
                 h.get("label", h["reason_code"]), readiness.OWNER_LABELS.get(h.get("owner_team"), h.get("owner_team"))]
                for h in sorted(new, key=lambda h: (not h.get("blocking"), h["order_id"]))
            ][:cap]
            text = f"{len(new)} new hold{'s' if len(new) != 1 else ''} ({len(blocking)} blocking)."
        sent = notify(
            "hold_created",
            title=f"Orders need attention: {len(new)} new hold{'s' if len(new) != 1 else ''}",
            text=text,
            facts=facts,
            rows=rows,
            columns=["Order", "Customer", "Hold", "Owner"] if rows else None,
            link=link,
            event_key=f"holds_new:{snapshot_id}",
        )
        if sent:
            readiness_store.mark_notified([h["id"] for h in new if h.get("id")], when=payload.get("evaluated_at"))

    if cleared:
        rows = [
            [h["cust_order_id"], (h.get("customer_name") or h.get("customer_id") or "")[:28],
             h.get("label") or h.get("reason_code"), h.get("cleared_how") or ""]
            for h in cleared
        ][:cap] if len(cleared) <= threshold else None
        notify(
            "hold_resolved",
            title=f"{len(cleared)} hold{'s' if len(cleared) != 1 else ''} cleared",
            text=None if rows else f"{len(cleared)} holds cleared since the last check.",
            rows=rows,
            columns=["Order", "Customer", "Hold", "How"] if rows else None,
            link=link,
            event_key=f"holds_cleared:{snapshot_id}",
        )


# --------------------------------------------------------------------------- reads


def current_payload() -> dict[str, Any]:
    """Latest snapshot with live acknowledgement state overlaid."""
    snapshot = readiness_store.latest_snapshot()
    if snapshot is None:
        return {
            "evaluated_at": None,
            "orders": [],
            "holds": [],
            "summary": {"orders": 0, "holds": 0, "blocked": 0, "attention": 0, "ready": 0,
                        "by_reason": {}, "by_owner": {}},
            "error": "No readiness evaluation has run yet.",
            "snapshot_id": None,
            "never_run": True,
        }
    open_rows = {row["id"]: row for row in readiness_store.open_holds()}
    holds: list[dict[str, Any]] = []
    for order in snapshot["orders"]:
        for hold in order.get("holds", []):
            row = open_rows.get(hold.get("id"))
            if row:
                hold["first_seen_at"] = row["first_seen_at"]
                hold["acknowledged_at"] = row.get("acknowledged_at")
                hold["acknowledged_by"] = row.get("acknowledged_by")
                hold["age_hours"] = _age_hours(row["first_seen_at"])
            holds.append(hold)
        ages = [h.get("age_hours") for h in order.get("holds", []) if h.get("age_hours") is not None]
        order["oldest_hold_hours"] = max(ages) if ages else None
        order["acknowledged"] = bool(order.get("holds")) and all(h.get("acknowledged_at") for h in order["holds"])
    return {
        "evaluated_at": snapshot["evaluated_at"],
        "trigger": snapshot["trigger"],
        "orders": snapshot["orders"],
        "holds": holds,
        "summary": snapshot["summary"],
        "error": snapshot["error"],
        "snapshot_id": snapshot["id"],
        "never_run": False,
        "stale_minutes": _minutes_since(snapshot["evaluated_at"]),
    }


def _age_hours(start: Optional[str]) -> Optional[float]:
    if not start:
        return None
    try:
        start_dt = dt.datetime.fromisoformat(start)
    except ValueError:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=dt.timezone.utc)
    return round(max(0.0, (dt.datetime.now(dt.timezone.utc) - start_dt).total_seconds() / 3600.0), 1)


def _minutes_since(stamp: Optional[str]) -> Optional[int]:
    hours = _age_hours(stamp)
    return int(hours * 60) if hours is not None else None


def orders_view(payload: dict[str, Any], *, hide_stock: bool = True, hide_rma: bool = True) -> dict[str, Any]:
    """Orders-page view of a payload. ``hide_stock`` removes supply holds (see
    ``readiness.STOCK_REASONS``); ``hide_rma`` drops RMA / warranty orders, which
    are worked outside the picklist. Summary tiles and owner chips are recomputed
    from what is left. The stored payload is never mutated."""
    orders = list(payload.get("orders") or [])
    rma_hidden = 0
    if hide_rma:
        kept = [o for o in orders if not (o.get("flags") or {}).get("is_rma")]
        rma_hidden = len(orders) - len(kept)
        orders = kept
    if hide_stock:
        orders = readiness.without_reasons(orders, readiness.STOCK_REASONS)
    summary = dict(payload.get("summary") or {})
    summary.update(readiness.summarize_orders(orders))
    return {
        **payload, "orders": orders, "summary": summary,
        "stock_holds_hidden": bool(hide_stock), "rma_hidden": rma_hidden if hide_rma else 0,
    }


def hide_stock_holds(payload: dict[str, Any]) -> dict[str, Any]:
    return orders_view(payload, hide_stock=True, hide_rma=False)


def filter_orders(orders: Iterable[dict[str, Any]], *, owner: Optional[str] = None,
                  state: Optional[str] = None, reason: Optional[str] = None,
                  customer: Optional[str] = None, query: Optional[str] = None,
                  firearms_only: bool = False, due_before: Optional[str] = None,
                  due_after: Optional[str] = None, blocking_only: bool = False) -> list[dict[str, Any]]:
    owner = (owner or "").strip().lower() or None
    state = (state or "").strip().upper() or None
    reason = (reason or "").strip().lower() or None
    customer = (customer or "").strip().upper() or None
    query = (query or "").strip().upper() or None
    out = []
    for order in orders:
        if owner and owner not in (order.get("owner_teams") or []):
            continue
        if blocking_only and not any(
            h.get("blocking") and (not owner or h.get("owner_team") == owner)
            for h in order.get("holds", [])
        ):
            continue
        if state and order.get("state") != state:
            continue
        if reason and reason not in {h.get("reason_code") for h in order.get("holds", [])}:
            continue
        if customer and customer not in (order.get("customer_id") or "").upper() and customer not in (order.get("customer_name") or "").upper():
            continue
        if firearms_only and not order.get("firearms"):
            continue
        due = order.get("due") or ""
        if due_before and due and due > due_before:
            continue
        if due_after and due and due < due_after:
            continue
        if query:
            haystack = " ".join([
                order.get("order_id") or "", order.get("customer_id") or "", order.get("customer_name") or "",
                order.get("po_ref") or "", order.get("ship_to_id") or "",
                " ".join(ln.get("part_id") or "" for ln in order.get("lines", [])),
            ]).upper()
            if query not in haystack:
                continue
        out.append(order)
    return out


def order_detail(so: str) -> dict[str, Any]:
    """Live evaluation of one order plus stored hold history and pick status."""
    order_id = (so or "").strip().upper()
    detail: dict[str, Any] = {"order_id": order_id, "found": False, "error": None}
    if not order_id:
        detail["error"] = "Order id is required."
        return detail
    try:
        rows = _call("fetch_order_rows", order_id, default=[]) or []
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Order detail query failed for %s", order_id)
        detail["error"] = str(exc)
        rows = []
    if rows:
        gate = _safe("gate_decisions", default={}) or {}
        payload = readiness.evaluate_readiness(
            rows,
            today=_today(),
            gate_decisions=gate,
            picklist_orders=_safe("picklist_orders_today"),
            picklist_horizon=_safe("picklist_horizon"),
            manual_holds=_safe("manual_holds", default=[]) or [],
            config=_safe("config", default={}) or {},
        )
        if payload["orders"]:
            order = payload["orders"][0]
            detail.update(order)
            detail["found"] = True
            detail["live_evaluated_at"] = payload["evaluated_at"]
        try:
            locations = _call("fetch_order_locations", order_id, default=[]) or []
            detail["locations"] = readiness.group_locations(locations)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Order location query failed for %s", order_id)
            detail["locations"] = {}
            detail["locations_error"] = str(exc)
        for line in detail.get("lines", []):
            line["locations"] = detail["locations"].get(line["part_id"], {"bins": [], "by_class": {}, "pickable_qty": 0.0})
    # Stored punch list (open + history) regardless of ERP availability.
    open_rows = readiness_store.open_holds(cust_order_id=order_id)
    by_key = {(r["cust_order_id"], r["line_no"], r["reason_code"]): r for r in open_rows}
    for hold in detail.get("holds", []):
        row = by_key.get((hold["order_id"], str(hold.get("line_no") or ""), hold["reason_code"]))
        if row:
            hold["id"] = row["id"]
            hold["first_seen_at"] = row["first_seen_at"]
            hold["age_hours"] = _age_hours(row["first_seen_at"])
            hold["acknowledged_at"] = row.get("acknowledged_at")
            hold["acknowledged_by"] = row.get("acknowledged_by")
    detail["hold_history"] = readiness_store.hold_history(order_id)
    detail["hold_events"] = readiness_store.events_for_order(order_id)
    detail["bin_class_labels"] = readiness.BIN_CLASS_LABELS
    try:
        detail["pick"] = _call("pick_status", order_id)
    except Exception:  # noqa: BLE001
        _logger.exception("Pick status lookup failed for %s", order_id)
        detail["pick"] = None
    detail["shipments"] = []
    detail["shipment_summary"] = shipments.summarize([])
    if _deps.get("fetch_order_shipments") is not None:
        try:
            packlists = shipments.group_packlists(_call("fetch_order_shipments", order_id, default=[]) or [])
            detail["shipments"] = packlists
            detail["shipment_summary"] = shipments.summarize(packlists)
            if packlists and not detail["found"] and not detail["error"]:
                # Order header may be archived; shipments still answer "did it ship?"
                detail.update({
                    "found": True,
                    "archived": True,
                    "customer_id": packlists[0]["customer_id"],
                    "customer_name": packlists[0]["customer_name"],
                    "order_status": "C",
                    "closed": True,
                    "state": "READY",
                    "holds": [],
                    "lines": [],
                    "blocking_count": 0,
                    "hold_count": 0,
                    "owner_teams": [],
                    "owner_labels": [],
                    "firearms": False,
                    "open_qty": 0.0,
                    "open_value": 0.0,
                    "due": None,
                    "promise_ship": None,
                    "promise_del": None,
                    "desired_ship": None,
                    "order_date": None,
                    "ship_via": "",
                    "ship_via_source": None,
                    "fob": "",
                    "salesrep_id": "",
                    "po_ref": "",
                    "discount_code": "",
                    "ship_to_id": "",
                    "ship_to": {"present": False},
                    "ffl": {},
                    "credit": {},
                    "docs": {"attachments": 0, "ffl_ez_check": 0, "ffl_master": 0},
                    "flags": {},
                    "gate": None,
                    "on_picklist": None,
                    "locations": {},
                    "live_evaluated_at": None,
                })
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Order shipments lookup failed for %s", order_id)
            detail["shipments_error"] = str(exc)
    return detail


def acknowledge(hold_id: int, *, actor: str, actor_team: Optional[str], note: Optional[str]) -> dict[str, Any]:
    return readiness_store.acknowledge_hold(hold_id, actor=actor, actor_team=actor_team, note=note)


def reason_options() -> list[dict[str, Any]]:
    return [
        {"code": code, "label": meta["label"], "owner": meta["owner"], "blocking": meta["blocking"]}
        for code, meta in readiness.HOLD_REASONS.items()
        if not meta.get("retired")
    ]
