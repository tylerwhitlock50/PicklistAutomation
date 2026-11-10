"""Stage inventory: what is sitting in the SHIPPING stage bins, and why.

Pure module (no Flask, no database). Two questions, two functions:

``summarize_flow`` — "how many guns went into stage and how many shipped,
each day?" from the rows of sql/stage_flow.sql.

``attribute_staged`` — "why are the guns in stage still here?" VISUAL does not
reserve a serial to a sales order, so a staged gun is matched to the open
orders that need its part (from the latest readiness snapshot).

Matching order: first to orders the release gate will let go on its own
(clear to ship, building a batch, gate holding), so those counts are the
most that can move without anyone fixing anything. Everything left goes to
the remaining orders oldest-due first, the way the floor stages, so an
overdue blocked order claims guns before an order due next month. Units
beyond every open order are "more than open orders need" / "no open order".
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Optional

from picklist.domain import readiness, release_gate

# --------------------------------------------------------------------------- flow

# MOVE_KIND (sql/stage_flow.sql) -> day field. Units, plus an order count for shipments.
FLOW_FIELDS = {
    "ARRIVED": "arrived",
    "SHIPPED": "shipped",
    "UNSHIPPED": "unshipped",
    "MOVED_OUT": "moved_out",
    "REVERSED": "reversed",
    "RESHUFFLED": "reshuffled",
    "SHIPPED_ALL": "shipped_all",
}
_ORDER_FIELDS = {"SHIPPED": "shipped_orders", "SHIPPED_ALL": "shipped_all_orders"}
_TOTAL_FIELDS = ("arrived", "shipped", "unshipped", "moved_out", "reversed", "shipped_all", "net")


def _as_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else number  # NaN -> 0


def _empty_day(day: dt.date, today: dt.date) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "date": day.isoformat(),
        "label": f"{day:%a} {day.month}/{day.day}",
        "weekday": day.weekday(),
        "is_today": day == today,
        "net": 0,
    }
    entry.update({field: 0 for field in FLOW_FIELDS.values()})
    entry.update({field: 0 for field in _ORDER_FIELDS.values()})
    return entry


def previous_working_day(today: dt.date) -> dt.date:
    """The weekday before ``today`` (Monday -> Friday)."""
    day = today - dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def summarize_flow(rows: Iterable[Mapping[str, Any]], *, today: dt.date, days: int = 14) -> dict[str, Any]:
    """Daily stage movement for the last ``days`` days.

    ``net`` = arrived - reversed + unshipped - shipped - moved_out: how much the
    stage pile grew that day. Stage-to-stage reshuffles are reported but never
    counted in or out. ``shipped_all`` is every serialized unit shipped that
    day from any location, so stage shipments can be read against total
    output. Weekend days with no movement are dropped.
    """
    days = max(1, int(days))
    start = today - dt.timedelta(days=days - 1)
    by_day: "OrderedDict[dt.date, dict[str, Any]]" = OrderedDict()
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        by_day[day] = _empty_day(day, today)

    for row in rows:
        day = _as_date(row.get("FLOW_DATE"))
        if day is None or day not in by_day:
            continue
        kind = str(row.get("MOVE_KIND") or "").strip().upper()
        field = FLOW_FIELDS.get(kind)
        if field is None:
            continue
        entry = by_day[day]
        entry[field] += int(round(_num(row.get("QTY"))))
        if kind in _ORDER_FIELDS:
            entry[_ORDER_FIELDS[kind]] += int(round(_num(row.get("ORDERS"))))

    for entry in by_day.values():
        entry["net"] = (entry["arrived"] - entry["reversed"] + entry["unshipped"]
                        - entry["shipped"] - entry["moved_out"])

    daily = [
        entry for day, entry in by_day.items()
        if day.weekday() < 5 or day == today or any(entry[f] for f in FLOW_FIELDS.values())
    ]

    prev_day = previous_working_day(today)
    week_start = today - dt.timedelta(days=6)
    week = [entry for day, entry in by_day.items() if day >= week_start]
    return {
        "days": daily,
        "window_days": days,
        "today": by_day.get(today) or _empty_day(today, today),
        "previous": by_day.get(prev_day) or _empty_day(prev_day, today),
        "last_7": {field: sum(e[field] for e in week) for field in _TOTAL_FIELDS},
        "max_daily": max([1] + [max(e["arrived"], e["shipped"]) for e in daily]),
    }


# --------------------------------------------------------------------------- attribution

# Display order. "tone" drives the colour on the Today bar and report.
BUCKETS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        ("clear", {"label": "Clear to ship", "tone": "good",
                   "help": "Open order with no hold, and the release gate says release. These could leave today."}),
        ("accumulating", {"label": "Building a batch", "tone": "info",
                          "help": "Release gate is accumulating stock for this order (daily store batch, threshold, completion). Releases on its own."}),
        ("gate_hold", {"label": "Release gate holding", "tone": "warn",
                       "help": "Release gate is holding the order: ship-to cooldown, waiting on a prior major order, scheduled sweep."}),
        ("not_due", {"label": "Staged early", "tone": "early",
                     "help": "The order is not due inside the release gate's window yet, so nothing will release it."}),
        ("gate_blind", {"label": "Gate can't see staged stock", "tone": "warn",
                        "help": "The release gate counts racks only, so it stopped at 'no supply' / 'waiting for completion' for this order. Its other policies (cooldown, batch threshold) were never applied, so when it would release is unknown."}),
        ("gate_blocked", {"label": "Gate hard block", "tone": "bad",
                          "help": "Release gate hard-blocks the order on a business rule: a line not available (closed or held), order not released, credit, ship-to."}),
        ("blocked_sales", {"label": "Waiting on Inside Sales", "tone": "bad",
                           "help": "Order readiness hold owned by Inside Sales: FFL, ship-to, not released, order hold, manual hold."}),
        ("blocked_finance", {"label": "Waiting on Finance", "tone": "bad",
                             "help": "Order readiness hold owned by Finance: credit status or credit limit."}),
        ("blocked_compliance", {"label": "Waiting on Compliance", "tone": "bad",
                                "help": "Order readiness hold owned by Compliance."}),
        ("blocked_production", {"label": "Waiting on Production", "tone": "bad",
                                "help": "Order readiness hold owned by Production."}),
        ("no_gate", {"label": "No release decision", "tone": "muted",
                     "help": "Order is due inside the release gate's window but the gate made no decision for it (gate off, failed, or order outside its scope)."}),
        ("blocked_shipping", {"label": "Waiting on Shipping", "tone": "bad",
                              "help": "Order readiness hold owned by Shipping."}),
        ("off_picklist", {"label": "RMA / international / excluded", "tone": "muted",
                          "help": "Order is worked outside the picklist (RMA, international, employee or excluded account)."}),
        ("extra", {"label": "More than open orders need", "tone": "muted",
                   "help": "This part has open orders, but fewer open units than are staged."}),
        ("no_order", {"label": "No open order", "tone": "muted",
                      "help": "No open order for this part due inside the readiness window."}),
    ]
)

# Allocation priority (see module docstring): orders the gate will move on
# its own first; every other bucket shares the last tier, ordered by due date.
_BUCKET_PRIORITY = {"clear": 0, "accumulating": 1, "gate_hold": 2}
_DEFAULT_PRIORITY = 3
# Headline groups; every bucket belongs to exactly one, so they sum to the total.
HEADLINE_GROUPS: "OrderedDict[str, tuple[str, ...]]" = OrderedDict([
    ("on_schedule", ("clear", "accumulating", "gate_hold")),   # the gate moves these on its own timing
    ("early", ("not_due",)),
    ("stuck", ("gate_blocked", "blocked_sales", "blocked_finance", "blocked_compliance",
               "blocked_production", "blocked_shipping")),
    ("outside", ("off_picklist",)),                              # worked outside the picklist on purpose
    ("no_decision", ("gate_blind", "no_gate")),                  # release timing unknown
    ("unmatched", ("extra", "no_order")),
])
# The gate's supply excludes stage bins, so these gate reasons on an order whose
# guns are already staged describe the gate's blind spot, not why they are here.
# The gate stops evaluating at the supply check, so its remaining policies
# (cooldown, batch threshold, ...) were never applied: release timing is unknown,
# never "clear".
_STAGE_BLIND_GATE_REASONS = frozenset({"no_supply"})
# Holding until complete: only real while the staged guns do not cover the order;
# once they do, it is the same blind spot.
_COMPLETION_REASON = "waiting_for_completion"
# Readiness reasons that mean the order is worked outside the picklist on purpose.
# Classified by reason, not owner: international exclusions are owned by Compliance.
_OFF_PICKLIST_REASONS = frozenset({"rma_excluded", "excluded_class"})
_GATE_BLIND_REASON = {"code": "gate_counts_racks_only",
                      "label": "Release gate counts racks only; its other policies were not applied"}
_REASON_ORDER = {code: index for index, code in enumerate(readiness.HOLD_REASONS)}
_IGNORED_REASONS = frozenset(readiness.STOCK_REASONS) | readiness.RETIRED_REASONS
_GATE_BUCKETS = {"RELEASE": "clear", "ACCUMULATING": "accumulating", "HOLD": "gate_hold", "BLOCKED": "gate_blocked"}


def _gate_reason(gate: Mapping[str, Any]) -> list[dict[str, str]]:
    """Gate decision -> reason rows. Hard blocks name the failing rule(s)."""
    code = str(gate.get("reason_code") or "").strip()
    label = str(gate.get("label") or "").strip()
    decision = str(gate.get("decision") or "").strip().upper()
    if decision == "BLOCKED" and " - " in label:
        # "BLOCKED - line not available, order not released" -> one row per rule.
        rules = [part.strip() for part in label.split(" - ", 1)[1].split(",") if part.strip()]
        if rules:
            return [{"code": f"hard_block:{rule}", "label": rule[:1].upper() + rule[1:]} for rule in rules]
    text = release_gate.REASON_CODES.get(code) or label or decision.title()
    return [{"code": code or decision.lower(), "label": text}]


def order_bucket(order: Mapping[str, Any], *, gate_horizon: Optional[dt.date] = None) -> tuple[str, list[dict[str, str]]]:
    """(bucket key, reasons that put it there) for one readiness order.

    Readiness holds win (they are specific and owned). Supply holds are
    ignored: readiness and the release gate count pickable racks only and
    exclude stage bins, so an order whose guns are already staged reads
    "no supply". Then the release-gate decision, used even when the gate runs
    advisory, because it is still what decides whether the order is released.
    No gate decision reads as "staged early" only when the order is due after
    ``gate_horizon`` (the gate's lookahead); otherwise it is "no decision", so
    a gate that is off or failed never makes due orders look early.
    """
    holds = [h for h in (order.get("holds") or []) if h.get("reason_code") not in _IGNORED_REASONS]
    blocking = [h for h in holds if h.get("blocking")]
    off_picklist = [h for h in blocking if h.get("reason_code") in _OFF_PICKLIST_REASONS]
    if off_picklist:
        return "off_picklist", [{"code": str(h.get("reason_code")), "label": str(h.get("label") or h.get("reason_code"))}
                                for h in off_picklist]
    if blocking:
        blocking.sort(key=lambda h: _REASON_ORDER.get(h.get("reason_code"), 999))
        owner = str(blocking[0].get("owner_team") or "sales")
        key = f"blocked_{owner}"
        if key not in BUCKETS:
            key = "blocked_sales"
        return key, [{"code": str(h.get("reason_code") or ""), "label": str(h.get("label") or h.get("reason_code") or "")}
                     for h in blocking]
    gate = order.get("gate") or {}
    decision = str(gate.get("decision") or "").strip().upper()
    if decision == "HOLD" and str(gate.get("reason_code") or "") in _STAGE_BLIND_GATE_REASONS:
        return "gate_blind", [dict(_GATE_BLIND_REASON)]
    if decision in _GATE_BUCKETS:
        key = _GATE_BUCKETS[decision]
        return key, ([] if key == "clear" else _gate_reason(gate))
    if not gate:
        due = order.get("due")
        due_date = _as_date(due)
        if gate_horizon is not None and due_date is not None and due_date > gate_horizon:
            return "not_due", [{"code": "outside_gate_window", "label": f"Order due {due}"}]
        return "no_gate", [{"code": "no_gate_decision", "label": "No release-gate decision"}]
    return "clear", []


def _due_key(order: Mapping[str, Any]) -> str:
    return str(order.get("due") or order.get("desired_ship") or "9999-12-31")


def attribute_staged(
    staged_by_part: Mapping[str, float],
    orders: Iterable[Mapping[str, Any]],
    *,
    descriptions: Optional[Mapping[str, str]] = None,
    gate_horizon: Optional[dt.date] = None,
) -> dict[str, Any]:
    """Explain every staged unit with a bucket from ``BUCKETS``.

    ``staged_by_part``: part_id -> units currently in stage.
    ``orders``: readiness snapshot orders (``readiness.evaluate_readiness``).
    ``gate_horizon``: last due date the release gate looks at (see order_bucket).
    """
    descriptions = {str(k).upper(): v for k, v in (descriptions or {}).items()}
    staged = {str(part).strip().upper(): int(round(_num(qty))) for part, qty in staged_by_part.items()}
    staged = {part: qty for part, qty in staged.items() if part and qty > 0}

    # part -> [(priority, due, order_id, line_no, open_qty)]; order facts kept per order.
    demand: dict[str, list[tuple]] = {}
    order_info: dict[str, dict[str, Any]] = {}
    for order in orders:
        if order.get("closed"):
            continue
        order_id = str(order.get("order_id") or "")
        bucket, reasons = order_bucket(order, gate_horizon=gate_horizon)
        priority = _BUCKET_PRIORITY.get(bucket, _DEFAULT_PRIORITY)
        open_lines = 0
        for line in order.get("lines") or []:
            part = str(line.get("part_id") or "").strip().upper()
            open_qty = int(round(_num(line.get("open_qty"))))
            if open_qty <= 0 or str(line.get("line_status") or "A").upper() == "X":
                continue
            open_lines += open_qty
            if part in staged:
                demand.setdefault(part, []).append(
                    (priority, _due_key(order), order_id, str(line.get("line_no") or ""), open_qty)
                )
        order_info[order_id] = {"order": order, "bucket": bucket, "reasons": reasons, "open_qty": open_lines}

    # Allocate: (order_id or None, part, qty, leftover bucket)
    allocations: list[tuple[Optional[str], str, int, Optional[str]]] = []
    by_part: list[dict[str, Any]] = []
    for part, qty in sorted(staged.items()):
        remaining = qty
        lines = sorted(demand.get(part, []), key=lambda d: d[:4])
        for _, _due, order_id, _line_no, open_qty in lines:
            if remaining <= 0:
                break
            take = min(remaining, open_qty)
            remaining -= take
            allocations.append((order_id, part, take, None))
        if remaining > 0:
            allocations.append((None, part, remaining, "extra" if lines else "no_order"))
        by_part.append({
            "part_id": part,
            "description": descriptions.get(part) or "",
            "staged": qty,
            "open_qty": sum(d[4] for d in lines),
            "buckets": {},
        })

    # A gate "waiting for completion" hold cannot see staged guns: when the
    # staged units already cover the order's whole open quantity, the hold is
    # the gate's blind spot. Its other policies were not applied, so this is
    # "timing unknown", not "clear".
    staged_on_order: dict[str, int] = {}
    for order_id, _part, take, _ in allocations:
        if order_id is not None:
            staged_on_order[order_id] = staged_on_order.get(order_id, 0) + take
    for order_id, info in order_info.items():
        if (info["bucket"] == "gate_hold"
                and any(r["code"] == _COMPLETION_REASON for r in info["reasons"])
                and info["open_qty"] > 0
                and staged_on_order.get(order_id, 0) >= info["open_qty"]):
            info["bucket"], info["reasons"] = "gate_blind", [dict(_GATE_BLIND_REASON)]

    bucket_qty = {key: 0 for key in BUCKETS}
    bucket_orders: dict[str, set] = {key: set() for key in BUCKETS}
    bucket_reasons: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in BUCKETS}
    by_order: dict[str, dict[str, Any]] = {}
    part_rows = {row["part_id"]: row for row in by_part}
    for order_id, part, take, leftover in allocations:
        if order_id is None:
            bucket = leftover
        else:
            info = order_info[order_id]
            order, bucket, reasons = info["order"], info["bucket"], info["reasons"]
            bucket_orders[bucket].add(order_id)
            for reason in reasons:
                entry = bucket_reasons[bucket].setdefault(
                    reason["code"], {"code": reason["code"], "label": reason["label"], "qty": 0, "orders": set()}
                )
                entry["orders"].add(order_id)
                entry["qty"] += take
            gate = order.get("gate") or {}
            row = by_order.setdefault(order_id, {
                "order_id": order_id,
                "customer_id": order.get("customer_id"),
                "customer_name": order.get("customer_name"),
                "state": order.get("state"),
                "bucket": bucket,
                "bucket_label": BUCKETS[bucket]["label"],
                "tone": BUCKETS[bucket]["tone"],
                "due": order.get("due"),
                "on_picklist": order.get("on_picklist"),
                "reasons": [r["label"] for r in reasons],
                "gate_label": gate.get("label"),
                "next_release_date": gate.get("next_release_date"),
                "open_qty": info["open_qty"],
                "staged_qty": 0,
                "parts": {},
            })
            row["staged_qty"] += take
            row["parts"][part] = row["parts"].get(part, 0) + take
        bucket_qty[bucket] += take
        part_buckets = part_rows[part]["buckets"]
        part_buckets[bucket] = part_buckets.get(bucket, 0) + take

    for row in by_part:
        row["unmatched"] = row["buckets"].get("extra", 0) + row["buckets"].get("no_order", 0)
        row["matched"] = row["staged"] - row["unmatched"]

    total = sum(staged.values())
    buckets = []
    for key, meta in BUCKETS.items():
        qty = bucket_qty[key]
        if not qty:
            continue
        reasons = sorted(
            ({"code": r["code"], "label": r["label"], "qty": r["qty"], "orders": len(r["orders"])}
             for r in bucket_reasons[key].values()),
            key=lambda r: (-r["qty"], r["label"]),
        )
        buckets.append({
            "key": key,
            "label": meta["label"],
            "tone": meta["tone"],
            "help": meta["help"],
            "qty": qty,
            "orders": len(bucket_orders[key]),
            "pct": round(100.0 * qty / total, 1) if total else 0.0,
            "reasons": reasons,
        })

    order_rows = sorted(
        by_order.values(),
        key=lambda r: (list(BUCKETS).index(r["bucket"]), -r["staged_qty"], r["order_id"]),
    )
    for row in order_rows:
        row["parts"] = [{"part_id": p, "qty": q} for p, q in sorted(row["parts"].items())]
    by_part.sort(key=lambda r: (-r["unmatched"], -r["staged"], r["part_id"]))

    result: dict[str, Any] = {
        "total": total,
        "clear_to_ship": bucket_qty["clear"],
        "buckets": buckets,
        "orders": order_rows,
        "parts": by_part,
    }
    for group, keys in HEADLINE_GROUPS.items():
        result[group] = sum(bucket_qty[k] for k in keys)
    return result
