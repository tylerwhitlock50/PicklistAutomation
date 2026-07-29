"""Supply-to-demand allocation model for the allocation screen.

Pure module: takes the rows from sql/alloc_supply.sql and sql/alloc_demand.sql
and returns a JSON-safe dict. No Flask, no database access — unit-testable
with plain lists of dicts.

The model answers Inside Sales' question "when will this SKU be available and
who is ahead of me?":

  * Supply becomes a chronological list of events tagged by certainty:
    AVAILABLE (eligible on-hand) < RELEASED (released WOs) < FIRMED (firmed
    WOs) < PLANNED (master-schedule buckets beyond the netting fence).
  * Demand is every open CO line for the SKU. Lines failing the picklist's
    eligibility rules are badged with reasons and excluded from allocation
    but still shown.
  * Eligible demand is sorted by the same priority the guns picklist uses
    (sql/query_guns.sql): effective Promise Del (line -> header, NULLs last)
    -> effective Promise Ship (NULLs last) -> header desired-ship norm ->
    order date -> SO -> line. Units are then assigned to supply events in a
    single pass, so no unit of supply is ever allocated twice.

`overrides` maps (cust_order_id, line_no) -> replacement LINE-level Promise
Del value (None = line value cleared, falling back to the header). That is
exactly what a save would write, so previews are what-if runs of this
function — nothing here mutates its inputs.

The projected date is an *estimated availability date*, never a promised
ship date; every line carries the certainty of the supply behind it.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

CERTAINTY_RANK = {"AVAILABLE": 0, "RELEASED": 1, "FIRMED": 2, "PLANNED": 3}
CLASS_CERTAINTY = {
    "ON_HAND": "AVAILABLE",
    "WO_RELEASED": "RELEASED",
    "WO_FIRMED": "FIRMED",
    "MPS": "PLANNED",
}


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


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Supply
# ---------------------------------------------------------------------------
def _build_supply(supply_rows: list[dict], today: date) -> dict:
    """Coerce raw supply rows into sorted events + informational stock."""
    on_hand_bins: list[dict] = []
    wo_events: list[dict] = []
    mps_rows: list[dict] = []
    informational: list[dict] = []

    for row in supply_rows:
        cls = _text(row.get("SUPPLY_CLASS"))
        qty = _int(row.get("QTY"))
        if not cls or qty <= 0:
            continue
        supply_id = _text(row.get("SUPPLY_ID")) or cls
        supply_date = _as_date(row.get("SUPPLY_DATE"))
        detail = _text(row.get("DETAIL"))

        if cls == "ON_HAND":
            on_hand_bins.append({"bin": supply_id, "qty": qty})
        elif cls == "ON_HAND_OTHER":
            informational.append({"location": supply_id, "qty": qty, "warehouse": detail})
        elif cls in ("WO_RELEASED", "WO_FIRMED"):
            wo_events.append({
                "class": cls,
                "supply_id": supply_id,
                "qty": qty,
                # A released WO with no dates at all is treated as due now.
                "date": supply_date or today,
                "certainty": CLASS_CERTAINTY[cls],
                "detail": detail,
            })
        elif cls == "MPS":
            mps_rows.append({
                "class": cls,
                "supply_id": supply_id,
                "qty": qty,
                "date": supply_date,
                "certainty": CLASS_CERTAINTY[cls],
                "detail": detail,
            })

    events: list[dict] = []
    if on_hand_bins:
        events.append({
            "class": "ON_HAND",
            "supply_id": "ON HAND",
            "qty": sum(b["qty"] for b in on_hand_bins),
            "date": today,
            "certainty": "AVAILABLE",
            "detail": ", ".join(f"{b['bin']} ({b['qty']})" for b in
                                sorted(on_hand_bins, key=lambda b: b["bin"])),
        })
    events.extend(wo_events)

    # Netting fence: a master-schedule bucket on/before the latest open-WO
    # want date is (at least partly) the plan the open WOs were cut from.
    # Counting both would double-count production, so those buckets are
    # dropped — the safe direction when the exact consumption rule is unknown.
    fence = max((e["date"] for e in wo_events), default=None)
    excluded_mps_qty = 0
    for row in mps_rows:
        if row["date"] is None:
            continue
        if fence is not None and row["date"] <= fence:
            excluded_mps_qty += row["qty"]
            continue
        events.append(row)

    events.sort(key=lambda e: (e["date"], CERTAINTY_RANK[e["certainty"]], e["supply_id"]))
    for seq, event in enumerate(events, start=1):
        event["seq"] = seq
        event["allocated_qty"] = 0

    return {
        "events": events,
        "informational": sorted(informational, key=lambda i: -i["qty"]),
        "netting_fence": fence,
        "excluded_mps_qty": excluded_mps_qty,
    }


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------
def _eligibility_reasons(line: dict, excluded_terms: tuple[str, ...]) -> list[str]:
    """Mirror the guns picklist's demand filters; one reason code per rule."""
    reasons: list[str] = []
    status = line["order_status"] or ""
    if status == "H":
        reasons.append("order_hold")
    elif status != "R":
        reasons.append("order_not_released")
    if (line["credit_status"] or "") != "A":
        reasons.append("credit_hold")
    po_ref = (line["customer_po_ref"] or "").upper()
    if (line["salesrep_id"] or "") == "RMA" or "RMA" in po_ref:
        reasons.append("rma")
    discount = (line["discount_code"] or "").upper()
    if "INTERNATIONAL" in discount or "EMPLOYEE" in discount:
        reasons.append("excluded_class")
    customer = (line["customer_id"] or "").upper()
    if any(term and term in customer for term in excluded_terms):
        reasons.append("excluded_customer")
    return reasons


def _coerce_demand(demand_rows: list[dict], excluded_terms: tuple[str, ...],
                   overrides: dict) -> list[dict]:
    lines: list[dict] = []
    for row in demand_rows:
        so = _text(row.get("CUST_ORDER_ID"))
        line_no = _int(row.get("LINE_NO"))
        if not so:
            continue
        open_qty = _int(row.get("OPEN_QTY"))
        if open_qty <= 0:
            continue

        line = {
            "so": so,
            "line_no": line_no,
            "customer_id": _text(row.get("CUSTOMER_ID")),
            "customer_name": _text(row.get("CUSTOMER_NAME")),
            "order_date": _as_date(row.get("ORDER_DATE")),
            "order_qty": _int(row.get("ORDER_QTY")),
            "shipped_qty": _int(row.get("SHIPPED_QTY")),
            "open_qty": open_qty,
            "hdr_desired": _as_date(row.get("HDR_DESIRED_SHIP_DATE")),
            "line_desired": _as_date(row.get("LINE_DESIRED_SHIP_DATE")),
            "hdr_promise_ship": _as_date(row.get("HDR_PROMISE_SHIP_DATE")),
            "line_promise_ship": _as_date(row.get("LINE_PROMISE_SHIP_DATE")),
            "hdr_promise_del": _as_date(row.get("HDR_PROMISE_DEL_DATE")),
            "line_promise_del": _as_date(row.get("LINE_PROMISE_DEL_DATE")),
            "order_status": _text(row.get("ORDER_STATUS")),
            "credit_status": _text(row.get("CREDIT_STATUS")),
            "salesrep_id": _text(row.get("SALESREP_ID")),
            "customer_po_ref": _text(row.get("CUSTOMER_PO_REF")),
            "discount_code": _text(row.get("DISCOUNT_CODE")),
        }

        key = (so, line_no)
        line["overridden"] = key in overrides
        line_del = overrides[key] if line["overridden"] else line["line_promise_del"]
        line["eff_promise_del"] = line_del if line_del is not None else line["hdr_promise_del"]
        if line["overridden"]:
            line["promise_del_source"] = "line" if line_del is not None else (
                "header" if line["hdr_promise_del"] is not None else None)
        elif line["line_promise_del"] is not None:
            line["promise_del_source"] = "line"
        elif line["hdr_promise_del"] is not None:
            line["promise_del_source"] = "header"
        else:
            line["promise_del_source"] = None
        line["eff_promise_ship"] = (line["line_promise_ship"]
                                    if line["line_promise_ship"] is not None
                                    else line["hdr_promise_ship"])
        line["reasons"] = _eligibility_reasons(line, excluded_terms)
        line["eligible"] = not line["reasons"]
        lines.append(line)
    return lines


def _priority_key(line: dict, today: date):
    """Must match sql/query_guns.sql DemandRanges ORDER BY exactly."""
    return (
        line["eff_promise_del"] is None,
        line["eff_promise_del"] or date.min,
        line["eff_promise_ship"] is None,
        line["eff_promise_ship"] or date.min,
        line["hdr_desired"] or today,       # DESIRED_SHIP_DATE_NORM
        line["order_date"] or date.min,
        line["so"],
        line["line_no"],
    )


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def build_allocation(
    supply_rows: list[dict],
    demand_rows: list[dict],
    today: date,
    lookahead_days: int,
    excluded_customer_terms: tuple[str, ...] = (),
    overrides: Optional[dict] = None,
) -> dict:
    """The allocation payload for the allocation page / API."""
    overrides = overrides or {}
    terms = tuple((t or "").upper() for t in excluded_customer_terms if t)
    through_date = today + timedelta(days=lookahead_days)

    supply = _build_supply(supply_rows, today)
    events = supply["events"]
    lines = _coerce_demand(demand_rows, terms, overrides)

    eligible = sorted((l for l in lines if l["eligible"]),
                      key=lambda l: _priority_key(l, today))

    # Single pass: each eligible line consumes the next available supply units.
    event_idx = 0
    for position, line in enumerate(eligible, start=1):
        line["position"] = position
        need = line["open_qty"]
        allocations: list[dict] = []
        while need > 0 and event_idx < len(events):
            event = events[event_idx]
            available = event["qty"] - event["allocated_qty"]
            if available <= 0:
                event_idx += 1
                continue
            take = min(need, available)
            event["allocated_qty"] += take
            need -= take
            allocations.append({
                "class": event["class"],
                "supply_id": event["supply_id"],
                "date": _iso(event["date"]),
                "qty": take,
                "certainty": event["certainty"],
            })
        line["allocations"] = allocations
        covered = line["open_qty"] - need
        if need == 0:
            line["supply_status"] = "ALLOCATED"
            line["est_available"] = allocations[-1]["date"]
            line["est_certainty"] = max(
                (a["certainty"] for a in allocations), key=lambda c: CERTAINTY_RANK[c])
        elif covered > 0:
            # Partially covered: never fabricate a date for the uncovered tail.
            line["supply_status"] = "PARTIAL"
            line["est_available"] = None
            line["est_certainty"] = None
        else:
            line["supply_status"] = "NO_SUPPLY"
            line["est_available"] = None
            line["est_certainty"] = None
        line["covered_qty"] = covered

    for line in lines:
        if not line["eligible"]:
            line["position"] = None
            line["allocations"] = []
            line["supply_status"] = None
            line["est_available"] = None
            line["est_certainty"] = None
            line["covered_qty"] = 0
        horizon = (line["eff_promise_del"] or line["eff_promise_ship"]
                   or line["hdr_desired"] or today)
        line["in_picklist_window"] = horizon <= through_date

    # Ineligible lines keep the demand table readable by sorting on the same
    # key; they interleave visually but carry no position.
    display_lines = sorted(lines, key=lambda l: _priority_key(l, today))

    eligible_open_units = sum(l["open_qty"] for l in eligible)
    allocated_units = sum(l["covered_qty"] for l in eligible)

    return {
        "today": today.isoformat(),
        "lookahead_days": lookahead_days,
        "picklist_through_date": through_date.isoformat(),
        "supply": {
            "events": [{
                "seq": e["seq"],
                "class": e["class"],
                "supply_id": e["supply_id"],
                "date": _iso(e["date"]),
                "qty": e["qty"],
                "allocated_qty": e["allocated_qty"],
                "remaining_qty": e["qty"] - e["allocated_qty"],
                "certainty": e["certainty"],
                "detail": e["detail"],
            } for e in events],
            "informational": supply["informational"],
            "netting_fence": _iso(supply["netting_fence"]),
            "excluded_mps_qty": supply["excluded_mps_qty"],
            "total_eligible_qty": sum(e["qty"] for e in events),
        },
        "demand": {
            "lines": [{
                "position": l["position"],
                "so": l["so"],
                "line_no": l["line_no"],
                "customer_id": l["customer_id"],
                "customer_name": l["customer_name"],
                "order_date": _iso(l["order_date"]),
                "order_qty": l["order_qty"],
                "shipped_qty": l["shipped_qty"],
                "open_qty": l["open_qty"],
                "dates": {
                    "hdr_desired": _iso(l["hdr_desired"]),
                    "line_desired": _iso(l["line_desired"]),
                    "hdr_promise_ship": _iso(l["hdr_promise_ship"]),
                    "line_promise_ship": _iso(l["line_promise_ship"]),
                    "hdr_promise_del": _iso(l["hdr_promise_del"]),
                    "line_promise_del": _iso(l["line_promise_del"]),
                    "eff_promise_ship": _iso(l["eff_promise_ship"]),
                    "eff_promise_del": _iso(l["eff_promise_del"]),
                    "promise_del_source": l["promise_del_source"],
                    "overridden": l["overridden"],
                },
                "eligible": l["eligible"],
                "reasons": l["reasons"],
                "in_picklist_window": l["in_picklist_window"],
                "allocations": l["allocations"],
                "supply_status": l["supply_status"],
                "covered_qty": l["covered_qty"],
                "est_available": l["est_available"],
                "est_certainty": l["est_certainty"],
            } for l in display_lines],
            "eligible_lines": len(eligible),
            "eligible_open_units": eligible_open_units,
            "unallocated_units": eligible_open_units - allocated_units,
        },
    }


# ---------------------------------------------------------------------------
# Preview & suggest
# ---------------------------------------------------------------------------
def _position_of(result: dict, so: str, line_no: int) -> Optional[int]:
    for line in result["demand"]["lines"]:
        if line["so"] == so and line["line_no"] == line_no:
            return line["position"]
    return None


def preview_change(
    supply_rows: list[dict],
    demand_rows: list[dict],
    today: date,
    lookahead_days: int,
    excluded_customer_terms: tuple[str, ...],
    so: str,
    line_no: int,
    new_value: Optional[date],
) -> dict:
    """What-if run: replace the LINE-level Promise Del and rebuild. No writes."""
    baseline = build_allocation(supply_rows, demand_rows, today, lookahead_days,
                                excluded_customer_terms)
    changed = build_allocation(supply_rows, demand_rows, today, lookahead_days,
                               excluded_customer_terms,
                               overrides={(so, line_no): new_value})

    before = {(l["so"], l["line_no"]): l["position"]
              for l in baseline["demand"]["lines"]}
    displaced = []
    for line in changed["demand"]["lines"]:
        key = (line["so"], line["line_no"])
        old_pos = before.get(key)
        if old_pos != line["position"] and key != (so, line_no):
            displaced.append({"so": line["so"], "line_no": line["line_no"],
                              "from": old_pos, "to": line["position"]})

    return {
        "baseline_position": _position_of(baseline, so, line_no),
        "new_position": _position_of(changed, so, line_no),
        "displaced": displaced,
        "result": changed,
    }


def suggest_promise_del(
    supply_rows: list[dict],
    demand_rows: list[dict],
    today: date,
    lookahead_days: int,
    excluded_customer_terms: tuple[str, ...],
    so: str,
    line_no: int,
    target_position: int,
) -> dict:
    """Suggest a LINE-level Promise Del that lands the line near target_position.

    The source of truth stays the date + documented tie-breakers: candidates
    are derived from the date of the line currently occupying the target slot
    and verified with a what-if run. A target inside the blank-Promise-Del
    tail may not be exactly reachable with a date; the closest achievable
    position is returned so the UI can say so.
    """
    baseline = build_allocation(supply_rows, demand_rows, today, lookahead_days,
                                excluded_customer_terms)
    ordered = [l for l in baseline["demand"]["lines"]
               if l["position"] is not None
               and not (l["so"] == so and l["line_no"] == line_no)]
    ordered.sort(key=lambda l: l["position"])

    if target_position < 1 or target_position > len(ordered) + 1:
        return {"error": "position_unreachable",
                "message": f"Target position must be between 1 and {len(ordered) + 1}."}

    occupant = ordered[target_position - 1] if target_position <= len(ordered) else None
    candidates: list[Optional[date]] = []
    if occupant is None:
        # Aim for the very end: after every dated line.
        last_dated = max((_as_date(l["dates"]["eff_promise_del"]) for l in ordered
                          if l["dates"]["eff_promise_del"]), default=None)
        candidates.append((last_dated + timedelta(days=1)) if last_dated else today)
    else:
        occ_date = _as_date(occupant["dates"]["eff_promise_del"])
        if occ_date is not None:
            candidates.extend([occ_date - timedelta(days=1), occ_date])
        else:
            # Target sits in the blank-Promise-Del tail: any date beats the
            # tail, so aim just past the last dated line.
            last_dated = max((_as_date(l["dates"]["eff_promise_del"]) for l in ordered
                              if l["dates"]["eff_promise_del"]), default=None)
            candidates.append((last_dated + timedelta(days=1)) if last_dated else today)

    best: Optional[dict] = None
    for cand in candidates:
        preview = preview_change(supply_rows, demand_rows, today, lookahead_days,
                                 excluded_customer_terms, so, line_no, cand)
        predicted = preview["new_position"]
        entry = {"suggested_date": _iso(cand), "predicted_position": predicted}
        if predicted == target_position:
            return entry
        if best is None or (predicted is not None and best["predicted_position"] is not None
                            and abs(predicted - target_position)
                            < abs(best["predicted_position"] - target_position)):
            best = entry
    return best or {"error": "position_unreachable",
                    "message": "No date lands the line at that position."}
