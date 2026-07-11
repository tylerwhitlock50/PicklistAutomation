"""Turn raw ERP serial-trace rows into a readable firearm history.

Pure module: takes the three DataFrames produced by the sql/serial_history_*.sql
queries and returns a JSON-safe dict (native Python types only). No Flask, no
database access — everything here is unit-testable with plain DataFrames.

VISUAL keeps one TRACE row per (part, serial). A firearm normally accumulates
several as it evolves — e.g. serialized receiver -> subassembly -> finished gun,
each step a work order that consumes the previous part and receives the next.
Those rows are chained here into a single "story" (part A issued to the same WO
part B was received from), so the user sees one combined timeline per physical
firearm. Only genuinely unrelated trace rows (true serial reuse) produce
multiple stories.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

# Bins where a built (finished-good) serial is expected to live — the audited
# universe, defined once in audit_universe.py.
from audit_universe import AUDITED_BINS as EXPECTED_GUN_BINS
from audit_universe import AUDITED_WAREHOUSES as EXPECTED_GUN_WAREHOUSES

VOIDED_SHIPPER_STATUSES = {"X", "V"}


# ---------------------------------------------------------------------------
# JSON-safe conversion
# ---------------------------------------------------------------------------
def _native(value: Any) -> Any:
    """Convert a pandas/numpy/Decimal scalar to a plain Python value."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _records(df: Optional[pd.DataFrame]) -> list[dict]:
    if df is None or df.empty:
        return []
    return [
        {key: _native(val) for key, val in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _dt_iso(value: Any) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value if value else None


def _dt_display(value: Any) -> Optional[str]:
    """ERP datetimes are naive local values; show them without tz conversion."""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value else None


def _date_words(value: Any) -> Optional[str]:
    """June 8, 2026 — for the plain-English summary."""
    if isinstance(value, (datetime, date)):
        return f"{value.strftime('%B')} {value.day}, {value.year}"
    return None


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------
def _wo_id(row: dict) -> Optional[str]:
    base = row.get("WORKORDER_BASE_ID")
    if not base:
        return None
    parts = [str(base)]
    lot = row.get("WORKORDER_LOT_ID")
    if lot not in (None, ""):
        parts.append(str(lot))
    split = row.get("WORKORDER_SPLIT_ID")
    sub = row.get("WORKORDER_SUB_ID")
    if split not in (None, "", "0", 0):
        parts.append(str(split))
    if sub not in (None, "", "0", 0):
        parts.append(str(sub))
    return "/".join(parts)


def _loc(row: dict, wh_key: str = "WAREHOUSE_ID", loc_key: str = "LOCATION_ID") -> str:
    wh = row.get(wh_key) or ""
    loc = row.get(loc_key) or ""
    if wh and loc:
        return f"{wh} / {loc}"
    return wh or loc or ""


def _is_inbound(row: dict) -> bool:
    qty = row.get("TRACE_QTY")
    return qty is not None and qty > 0


def _tracking_numbers(ship: dict) -> list[str]:
    """Carrier tracking for a shipment row: UPS export rows first (already a
    comma-joined distinct list from SQL), else the packlist tracking UDF."""
    ups = ship.get("TRACKING_NUMBERS")
    if ups:
        return [t.strip() for t in str(ups).split(",") if t.strip()]
    udf = ship.get("UDF_TRACKING_NUMBER")
    if udf:
        return [str(udf).strip()]
    return []


# ---------------------------------------------------------------------------
# Lineage: chain trace matches into stories
# ---------------------------------------------------------------------------
def _wo_receipts(txns: list[dict]) -> set:
    return {
        _wo_id(r) for r in txns
        if _is_inbound(r) and r.get("CLASS") == "R" and _wo_id(r)
    }


def _wo_issues(txns: list[dict]) -> set:
    return {
        _wo_id(r) for r in txns
        if not _is_inbound(r) and r.get("CLASS") == "I" and _wo_id(r)
    }


def _group_stories(matches: list[dict]) -> list[list[dict]]:
    """Union-find over trace matches: link A->B when A was issued to a WO that
    B was received from (the part 'became' the next part). Each connected
    component is one physical-item story."""
    n = len(matches)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(n):
            if i != j and matches[i]["wo_issues"] & matches[j]["wo_receipts"]:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i, match in enumerate(matches):
        groups.setdefault(find(i), []).append(match)

    def first_txn_date(group: list[dict]):
        dates = [t["TRANSACTION_DATE"] for m in group for t in m["txns"] if t.get("TRANSACTION_DATE")]
        return min(dates) if dates else datetime.max

    return sorted(groups.values(), key=first_txn_date)


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------
def classify_events(txns: list[dict], shipments: list[dict]) -> tuple[list[dict], list[str]]:
    """Turn raw transaction rows into ordered timeline events.

    Returns (events, problem_codes) where problem_codes feeds detect_flags
    (e.g. an unpaired transfer leg).
    """
    live_ship_by_txn = {
        s["TRANSACTION_ID"]: s for s in shipments
        if (s.get("SHIPPER_STATUS") or "").upper() not in VOIDED_SHIPPER_STATUSES
    }
    # What part each work order consumed from this serial's trace — used to say
    # "part A became part B" on build receipts.
    consumed_part_by_wo = {
        _wo_id(r): r.get("PART_ID")
        for r in txns
        if not _is_inbound(r) and r.get("CLASS") == "I" and _wo_id(r)
    }
    built_part_by_wo = {
        _wo_id(r): r.get("PART_ID")
        for r in txns
        if _is_inbound(r) and r.get("CLASS") == "R" and _wo_id(r)
    }
    voided_ship_by_txn = {
        s["TRANSACTION_ID"]: s for s in shipments
        if (s.get("SHIPPER_STATUS") or "").upper() in VOIDED_SHIPPER_STATUSES
    }
    by_txn_id = {r["TRANSACTION_ID"]: r for r in txns}

    events: list[dict] = []
    problems: list[str] = []
    consumed_transfer_ids: set = set()

    def add(row: dict, event_type: str, label: str, reference: str = "",
            detail: str = "", location: Optional[str] = None,
            txn_ids: Optional[list] = None,
            tracking: Optional[list[str]] = None) -> None:
        events.append({
            "date": row.get("TRANSACTION_DATE"),
            "event_type": event_type,
            "label": label,
            "location": location if location is not None else _loc(row),
            "user": row.get("USER_ID") or "",
            "reference": reference,
            "detail": detail,
            "part_id": row.get("PART_ID"),
            "transaction_ids": txn_ids or [row["TRANSACTION_ID"]],
            "tracking": tracking or [],
        })

    for row in txns:
        txn_id = row["TRANSACTION_ID"]
        if txn_id in consumed_transfer_ids:
            continue
        wo = _wo_id(row)

        ship = live_ship_by_txn.get(txn_id)
        if ship is not None:
            customer = ship.get("CUSTOMER_NAME") or ship.get("CUSTOMER_ID") or "unknown customer"
            ref_bits = [b for b in (ship.get("CUST_ORDER_ID"), ship.get("PACKLIST_ID")) if b]
            detail_bits = []
            tracking = _tracking_numbers(ship)
            if tracking:
                detail_bits.append("Tracking " + ", ".join(tracking))
            if ship.get("WAYBILL_NUMBER"):
                detail_bits.append(f"Waybill {ship['WAYBILL_NUMBER']}")
            if ship.get("INVOICE_ID"):
                detail_bits.append(f"Invoice {ship['INVOICE_ID']}")
            add(row, "shipped", f"Shipped to {customer}",
                reference=" · ".join(str(b) for b in ref_bits),
                detail="; ".join(detail_bits),
                tracking=tracking)
            continue

        voided = voided_ship_by_txn.get(txn_id)
        if voided is not None:
            add(row, "other", "Shipment voided",
                reference=str(voided.get("PACKLIST_ID") or ""),
                detail="This packlist was cancelled/voided; the ship transaction remains in history.")
            continue

        if _is_inbound(row) and wo and row.get("CLASS") == "R":
            detail = ""
            if row.get("WO_PART_ID") and row.get("WO_PART_ID") != row.get("PART_ID"):
                detail = f"Work order part {row['WO_PART_ID']} differs from trace part {row['PART_ID']}."
            consumed = consumed_part_by_wo.get(wo)
            if consumed and consumed != row.get("PART_ID"):
                label = f"Built — {consumed} became {row.get('PART_ID')}"
            else:
                label = "Built — received from work order"
            add(row, "built", label, reference=f"WO {wo}", detail=detail)
            continue

        if not _is_inbound(row) and wo and row.get("CLASS") == "I":
            becomes = built_part_by_wo.get(wo)
            if becomes and becomes != row.get("PART_ID"):
                detail = f"Consumed to build {becomes}."
            else:
                detail = "Consumed as a component of the next build."
            add(row, "issued", f"Issued to work order {wo}",
                reference=f"WO {wo}", detail=detail)
            continue

        if _is_inbound(row) and row.get("PURC_ORDER_ID"):
            po = row["PURC_ORDER_ID"]
            add(row, "received", f"Received on purchase order {po}", reference=str(po))
            continue

        transfer_id = row.get("TRANSFER_TRANS_ID")
        if transfer_id:
            partner = by_txn_id.get(transfer_id)
            if partner is not None and partner.get("TRANSFER_TRANS_ID") == txn_id:
                consumed_transfer_ids.add(transfer_id)
                out_leg, in_leg = (row, partner) if not _is_inbound(row) else (partner, row)
                add(row, "moved", f"Moved {_loc(out_leg)} → {_loc(in_leg)}",
                    reference="Transfer", location=_loc(in_leg),
                    txn_ids=[out_leg["TRANSACTION_ID"], in_leg["TRANSACTION_ID"]])
            else:
                add(row, "moved", "Moved (partial transfer record)", reference="Transfer",
                    detail="The matching transfer leg was not found in this serial's trace history.")
                problems.append("unpaired_transfer")
            continue

        if row.get("CLASS") == "A":
            direction = "+" if _is_inbound(row) else "−"
            add(row, "adjusted", f"Inventory adjusted ({direction}{abs(row.get('TRACE_QTY') or 0):g})",
                reference="Adjustment")
            continue

        if row.get("CLASS") == "C":
            add(row, "adjusted", "Inventory correction", reference="Correction")
            continue

        if not _is_inbound(row) and row.get("CUST_ORDER_ID"):
            add(row, "issued", f"Issued against sales order {row['CUST_ORDER_ID']} (no packlist found)",
                reference=str(row["CUST_ORDER_ID"]))
            problems.append("shipped_no_customer")
            continue

        add(row, "other",
            f"Inventory transaction ({row.get('TYPE') or '?'}/{row.get('CLASS') or '?'})",
            detail=row.get("TRANS_DESCRIPTION") or "")

    events.sort(key=lambda e: (e["date"] or datetime.min, min(e["transaction_ids"])))
    return events, problems


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------
def compute_current_locations(txns: list[dict]) -> tuple[list[dict], bool]:
    """Sum trace qty per (part, warehouse, location); keep positive balances.
    Also reports whether any balance went negative (ledger inconsistency)."""
    sums: dict[tuple, float] = {}
    for row in txns:
        key = (row.get("PART_ID"), row.get("WAREHOUSE_ID"), row.get("LOCATION_ID"))
        sums[key] = sums.get(key, 0) + (row.get("TRACE_QTY") or 0)
    locations = [
        {"part_id": part, "warehouse_id": wh, "location_id": loc, "qty": qty}
        for (part, wh, loc), qty in sorted(sums.items(), key=lambda kv: str(kv[0]))
        if qty > 0
    ]
    has_negative = any(qty < 0 for qty in sums.values())
    return locations, has_negative


def derive_status(net_qty: float, locations: list[dict], events: list[dict]) -> str:
    if any(e["event_type"] == "shipped" for e in events):
        last_terminal = next(
            (e for e in reversed(events) if e["event_type"] in ("shipped", "built", "received")),
            None,
        )
        if last_terminal is not None and last_terminal["event_type"] == "shipped":
            return "shipped"
    if locations or net_qty > 0:
        return "in_stock"
    if events and events[-1]["event_type"] == "issued":
        return "in_wip"
    return "unknown"


def build_summary(serial: str, primary: dict, events: list[dict], status: str,
                  locations: list[dict]) -> str:
    sentences: list[str] = []

    part_bits = primary.get("PART_ID") or "an unknown part"
    desc = primary.get("PART_DESCRIPTION")
    ident = f"Serial number {serial} is part {part_bits}"
    if desc:
        ident += f" — {desc}"
    sentences.append(ident + ".")

    po_receipt = next((e for e in events if e["event_type"] == "received"), None)
    if po_receipt is not None:
        s = f"It was received on {po_receipt['reference']} into {po_receipt['location']}"
        if _date_words(po_receipt["date"]):
            s += f" on {_date_words(po_receipt['date'])}"
        if po_receipt["user"]:
            s += f" by {po_receipt['user']}"
        sentences.append(s + ".")

    builds = [e for e in events if e["event_type"] == "built"]
    if builds:
        final_build = builds[-1]
        s = f"It was built on work order {final_build['reference'].replace('WO ', '')}"
        if final_build.get("part_id"):
            s = f"It was built as part {final_build['part_id']} on work order " \
                f"{final_build['reference'].replace('WO ', '')}"
        s += f" and received into {final_build['location']}"
        if _date_words(final_build["date"]):
            s += f" on {_date_words(final_build['date'])}"
        if final_build["user"]:
            s += f" by {final_build['user']}"
        if len(builds) > 1:
            s += f" (after passing through {len(builds) - 1} earlier build"
            s += "s)" if len(builds) > 2 else ")"
        sentences.append(s + ".")

    ships = [e for e in events if e["event_type"] == "shipped"]
    last_ship = ships[-1] if ships else None

    moves = [e for e in events if e["event_type"] == "moved"]
    if builds:
        final_build_date = builds[-1]["date"] or datetime.min
        moves = [m for m in moves if (m["date"] or datetime.min) >= final_build_date]
    if moves:
        last_move = moves[-1]
        if last_ship is None or (last_move["date"] or datetime.min) <= (last_ship["date"] or datetime.min):
            s = f"It was last moved to {last_move['location']}"
            if _date_words(last_move["date"]):
                s += f" on {_date_words(last_move['date'])}"
            sentences.append(s + ".")

    if last_ship is not None:
        s = last_ship["label"]
        if _date_words(last_ship["date"]):
            s += f" on {_date_words(last_ship['date'])}"
        if last_ship["reference"]:
            s += f" under {last_ship['reference']}"
        if last_ship["user"]:
            s += f", shipped by {last_ship['user']}"
        if last_ship.get("tracking"):
            s += f" (tracking {', '.join(last_ship['tracking'])})"
        sentences.append(s + ".")
    elif status == "in_stock" and locations:
        spots = ", ".join(_loc(l, "warehouse_id", "location_id") for l in locations)
        sentences.append(f"It is currently on hand in {spots}.")
    elif status == "in_wip":
        last_issue = next((e for e in reversed(events) if e["event_type"] == "issued"), None)
        if last_issue is not None:
            sentences.append(
                f"It was last issued to work order "
                f"{last_issue['reference'].replace('WO ', '')} and has not been "
                f"received back — it is likely in work-in-process."
            )
    elif not events:
        sentences.append("No inventory transactions were found for this serial.")
    else:
        sentences.append("It has no recorded shipment and no current on-hand balance.")

    return " ".join(sentences)


def detect_flags(story: dict) -> list[dict]:
    flags: list[dict] = []
    events = story["events"]
    locations = story["locations"]
    net_qty = story["net_qty"]
    shipments = story["shipments"]
    problems = set(story["problems"])

    has_build = any(e["event_type"] == "built" for e in events)
    has_receipt = has_build or any(e["event_type"] == "received" for e in events)
    live_ships = [s for s in shipments
                  if (s.get("SHIPPER_STATUS") or "").upper() not in VOIDED_SHIPPER_STATUSES]

    def flag(code: str, severity: str, message: str) -> None:
        flags.append({"code": code, "severity": severity, "message": message})

    if not has_build and not live_ships:
        flag("no_finished_good", "info",
             "This serial was never received from a work order — it is not "
             "attached to a finished-good build.")

    if has_receipt and net_qty > 0 and not live_ships:
        flag("never_shipped", "info",
             "This serial was received into inventory and is still in stock — "
             "it has never shipped.")

    if "shipped_no_customer" in problems or any(
        s for s in live_ships if not (s.get("CUSTOMER_NAME") or s.get("CUSTOMER_ID"))
    ):
        flag("shipped_no_customer", "warning",
             "This serial left inventory against a sales order, but no clear "
             "customer/packlist record was found.")

    mismatches = {
        (e["part_id"], e["detail"]) for e in events
        if e["event_type"] == "built" and e["detail"].startswith("Work order part")
    }
    if mismatches:
        flag("wo_part_mismatch", "warning",
             "A build receipt's work-order part does not match the traced part — "
             "possible conflicting product references.")

    if has_build and locations:
        unexpected = [
            l for l in locations
            if (l["warehouse_id"], l["location_id"]) not in EXPECTED_GUN_BINS
            and l["warehouse_id"] not in EXPECTED_GUN_WAREHOUSES
        ]
        if unexpected:
            spots = ", ".join(_loc(l, "warehouse_id", "location_id") for l in unexpected)
            flag("unexpected_location", "warning",
                 f"A built firearm is on hand outside the expected serialized "
                 f"bins: {spots}.")

    lineage = story.get("parts_lineage") or []
    stuck_stages = [p for p in lineage[:-1] if p["stage_status"] == "on_hand"]
    if stuck_stages:
        parts = ", ".join(str(p["part_id"]) for p in stuck_stages)
        flag("lineage_not_cleared", "warning",
             f"An earlier build stage still shows on hand ({parts}) — it was "
             "consumed into a later build but never cleared out of inventory.")

    if net_qty > 0 and not events:
        flag("incomplete_history", "warning",
             "The trace shows an on-hand balance but no inventory transactions "
             "were found.")
    if "unpaired_transfer" in problems:
        flag("incomplete_history", "warning",
             "A location transfer is missing its matching leg — the movement "
             "history may be incomplete.")
    if story["has_negative_balance"]:
        flag("incomplete_history", "warning",
             "A location balance for this serial went negative — the ledger "
             "history looks inconsistent.")

    if any((s.get("SHIPPER_STATUS") or "").upper() in VOIDED_SHIPPER_STATUSES
           for s in shipments):
        flag("voided_shipment", "info",
             "A voided/cancelled packlist exists in this serial's history.")

    return flags


# ---------------------------------------------------------------------------
# Detail sections
# ---------------------------------------------------------------------------
def _detail_sections(txns: list[dict], shipments: list[dict], events: list[dict]) -> dict:
    work_orders: dict[str, dict] = {}
    for row in txns:
        wo = _wo_id(row)
        if not wo:
            continue
        entry = work_orders.setdefault(wo, {
            "wo_id": wo,
            "wo_part_id": row.get("WO_PART_ID"),
            "status": row.get("WO_STATUS"),
            "create_date": _dt_display(row.get("WO_CREATE_DATE")),
            "close_date": _dt_display(row.get("WO_CLOSE_DATE")),
            "roles": set(),
            "users": set(),
        })
        entry["roles"].add("received from" if _is_inbound(row) else "issued to")
        if row.get("USER_ID"):
            entry["users"].add(row["USER_ID"])
    work_orders_out = [
        {**wo, "roles": " & ".join(sorted(wo["roles"])), "users": ", ".join(sorted(wo["users"]))}
        for wo in work_orders.values()
    ]

    movements = [
        {
            "date": _dt_iso(e["date"]),
            "date_display": _dt_display(e["date"]),
            "move": e["label"],
            "user": e["user"],
        }
        for e in events if e["event_type"] == "moved"
    ]

    seen_packlists = set()
    shipments_out = []
    orders: dict[str, dict] = {}
    for s in shipments:
        key = (s.get("PACKLIST_ID"), s.get("TRANSACTION_ID"))
        if key in seen_packlists:
            continue
        seen_packlists.add(key)
        shipments_out.append({
            "packlist_id": s.get("PACKLIST_ID"),
            "shipped_date": _dt_iso(s.get("SHIPPED_DATE")),
            "shipped_date_display": _dt_display(s.get("SHIPPED_DATE")),
            "status": s.get("SHIPPER_STATUS"),
            "voided": (s.get("SHIPPER_STATUS") or "").upper() in VOIDED_SHIPPER_STATUSES,
            "tracking_numbers": _tracking_numbers(s),
            "waybill": s.get("WAYBILL_NUMBER"),
            "invoice_id": s.get("INVOICE_ID"),
            "cust_order_id": s.get("CUST_ORDER_ID"),
            "customer_id": s.get("CUSTOMER_ID"),
            "customer_name": s.get("CUSTOMER_NAME"),
            "customer_po": s.get("CUSTOMER_PO_REF"),
        })
        so = s.get("CUST_ORDER_ID")
        if so and so not in orders:
            orders[so] = {
                "cust_order_id": so,
                "customer_id": s.get("CUSTOMER_ID"),
                "customer_name": s.get("CUSTOMER_NAME"),
                "customer_po": s.get("CUSTOMER_PO_REF"),
                "order_date": _dt_display(s.get("ORDER_DATE")),
            }

    raw = [
        {
            "transaction_id": row.get("TRANSACTION_ID"),
            "date": _dt_display(row.get("TRANSACTION_DATE")),
            "part_id": row.get("PART_ID"),
            "type": row.get("TYPE"),
            "class": row.get("CLASS"),
            "qty": row.get("TRACE_QTY"),
            "warehouse_id": row.get("WAREHOUSE_ID"),
            "location_id": row.get("LOCATION_ID"),
            "user_id": row.get("USER_ID"),
            "workorder": _wo_id(row),
            "purc_order": row.get("PURC_ORDER_ID"),
            "cust_order": row.get("CUST_ORDER_ID"),
            "transfer_trans_id": row.get("TRANSFER_TRANS_ID"),
        }
        for row in txns
    ]

    return {
        "work_orders": work_orders_out,
        "movements": movements,
        "shipments": shipments_out,
        "orders": list(orders.values()),
        "raw_transactions": raw,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_serial_history(
    serial: str,
    trace_df: pd.DataFrame,
    txns_df: pd.DataFrame,
    shipments_df: pd.DataFrame,
) -> dict:
    trace_rows = _records(trace_df)
    txn_rows = _records(txns_df)
    ship_rows = _records(shipments_df)

    txns_by_part: dict[tuple, list[dict]] = {}
    for row in txn_rows:
        txns_by_part.setdefault((row.get("PART_ID"), row.get("TRACE_ID")), []).append(row)
    ships_by_part: dict[tuple, list[dict]] = {}
    for row in ship_rows:
        ships_by_part.setdefault((row.get("PART_ID"), row.get("TRACE_ID")), []).append(row)

    matches = []
    for trace in trace_rows:
        key = (trace.get("PART_ID"), trace.get("TRACE_ID"))
        txns = sorted(
            txns_by_part.get(key, []),
            key=lambda r: (r.get("TRANSACTION_DATE") or datetime.min, r.get("TRANSACTION_ID") or 0),
        )
        matches.append({
            "trace": trace,
            "txns": txns,
            "shipments": ships_by_part.get(key, []),
            "wo_receipts": _wo_receipts(txns),
            "wo_issues": _wo_issues(txns),
        })

    stories_out = []
    for group in _group_stories(matches):
        txns = sorted(
            (t for m in group for t in m["txns"]),
            key=lambda r: (r.get("TRANSACTION_DATE") or datetime.min, r.get("TRANSACTION_ID") or 0),
        )
        shipments = sorted(
            (s for m in group for s in m["shipments"]),
            key=lambda s: (s.get("SHIPPED_DATE") or datetime.min),
        )
        events, problems = classify_events(txns, shipments)
        locations, has_negative = compute_current_locations(txns)
        net_qty = sum((m["trace"].get("IN_QTY") or 0) - (m["trace"].get("OUT_QTY") or 0)
                      for m in group)

        # Primary part: what shipped; else the last part built; else the most
        # recently active trace row.
        primary_trace = group[-1]["trace"]
        live_ship = next(
            (s for s in reversed(shipments)
             if (s.get("SHIPPER_STATUS") or "").upper() not in VOIDED_SHIPPER_STATUSES),
            None,
        )
        target_part = None
        if live_ship is not None:
            target_part = live_ship.get("PART_ID")
        else:
            last_build = next((e for e in reversed(events) if e["event_type"] == "built"), None)
            if last_build is not None:
                target_part = last_build["part_id"]
        if target_part is not None:
            primary_trace = next(
                (m["trace"] for m in group if m["trace"].get("PART_ID") == target_part),
                primary_trace,
            )

        status = derive_status(net_qty, locations, events)

        shipped_parts = {
            s.get("PART_ID") for s in shipments
            if (s.get("SHIPPER_STATUS") or "").upper() not in VOIDED_SHIPPER_STATUSES
        }
        parts_lineage = sorted(
            (
                {
                    "part_id": m["trace"].get("PART_ID"),
                    "part_description": m["trace"].get("PART_DESCRIPTION"),
                    "product_code": m["trace"].get("PRODUCT_CODE"),
                    "first_seen": _dt_display(m["txns"][0]["TRANSACTION_DATE"]) if m["txns"] else None,
                    "net_qty": (m["trace"].get("IN_QTY") or 0) - (m["trace"].get("OUT_QTY") or 0),
                    "on_hand": [
                        l for l in locations if l["part_id"] == m["trace"].get("PART_ID")
                    ],
                    "_sort": m["txns"][0]["TRANSACTION_DATE"] if m["txns"] else datetime.max,
                }
                for m in group
            ),
            key=lambda p: p["_sort"],
        )
        for p in parts_lineage:
            p.pop("_sort", None)
            if p["on_hand"] or p["net_qty"] > 0:
                p["stage_status"] = "on_hand"
            elif p["part_id"] in shipped_parts:
                p["stage_status"] = "shipped"
            else:
                p["stage_status"] = "cleared"

        story = {
            "events": events,
            "problems": problems,
            "locations": locations,
            "net_qty": net_qty,
            "shipments": shipments,
            "has_negative_balance": has_negative,
            "parts_lineage": parts_lineage,
        }
        flags = detect_flags(story)

        timeline = [
            {
                "date": _dt_iso(e["date"]),
                "date_display": _dt_display(e["date"]),
                "event_type": e["event_type"],
                "label": e["label"],
                "part_id": e["part_id"],
                "location": e["location"],
                "user": e["user"],
                "reference": e["reference"],
                "detail": e["detail"],
                "tracking": e.get("tracking") or [],
            }
            for e in events
        ]

        stories_out.append({
            "part_id": primary_trace.get("PART_ID"),
            "part_description": primary_trace.get("PART_DESCRIPTION"),
            "product_code": primary_trace.get("PRODUCT_CODE"),
            "trace_id": primary_trace.get("TRACE_ID"),
            "serial_no": primary_trace.get("SERIAL_NO") or serial,
            "net_qty": net_qty,
            "status": status,
            "current_locations": locations,
            "parts_lineage": parts_lineage,
            "summary": build_summary(serial, primary_trace, events, status, locations),
            "flags": flags,
            "timeline": timeline,
            "details": _detail_sections(txns, shipments, events),
        })

    top_flags: list[dict] = []
    if not stories_out:
        top_flags.append({
            "code": "not_found",
            "severity": "error",
            "message": f"Serial number {serial} was not found in the ERP.",
        })
    elif len(stories_out) > 1:
        top_flags.append({
            "code": "multiple_matches",
            "severity": "warning",
            "message": (
                f"This serial number matches {len(stories_out)} unrelated item "
                "records in the ERP (the serial was reused). All are shown below."
            ),
        })

    return {"serial": serial, "flags": top_flags, "matches": stories_out}
