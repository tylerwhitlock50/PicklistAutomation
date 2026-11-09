"""Promise metadata, risk explanations and atomic binding/reassignment.

Transfers move a promise directly to its successor under the warehouse mutex.
They never release stock into the public pool between owners.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fulfillment import model as m
from fulfillment.domain import identifier, quantity, require


def metadata(payload, current=None):
    current = current or {}
    priority = payload.get("priority", current.get("priority", 50))
    require(type(priority) is int and 0 <= priority <= 100,
            "Priority must be a whole number from 0 (urgent) to 100 (low).", "invalid", 400)
    reference = str(payload.get("request_reference", current.get("request_reference", "")) or "").strip()
    require(len(reference) <= 200 and not any(ord(c) < 32 for c in reference),
            "Request reference must be at most 200 characters without control characters.", "invalid", 400)
    needed = str(payload.get("need_by", current.get("need_by", "")) or "").strip()
    if needed:
        try:
            valid = date.fromisoformat(needed).isoformat() == needed
        except ValueError:
            valid = False
        require(valid, "Need-by date must use YYYY-MM-DD.", "invalid", 400)
    return dict(priority=priority, request_reference=reference, need_by=needed)


def priority_key(allocation):
    return (allocation.get("priority", 50), allocation.get("need_by") or "9999-12-31",
            allocation["created_at"], allocation["id"])


def validate_target(service, conn, payload):
    """Validate an explicit binding against current order ownership and line identity."""
    from fulfillment import inventory

    customer = identifier(payload.get("customer"), "Customer")
    if not payload.get("order"):
        require(not payload.get("order_line"), "An order-line reservation needs an order.", "invalid", 400)
        return
    target = service.erp.inspect_order(identifier(payload["order"], "Order"))
    require(target["site"] == inventory.config(conn)["site"] and target.get("customer_id") == customer,
            "This order does not belong to the specified customer and shipping site.", "allocation_owner")
    matches = [line for line in target["lines"]
               if line["part"] == identifier(payload.get("part")) and line["uom"] == identifier(payload.get("uom"))
               and (not payload.get("order_line") or str(line["order_line"]) == identifier(payload["order_line"]))]
    require(any(quantity(line["quantity"], zero=True) > 0 for line in matches),
            "The order has no matching open part, stocking unit and line.", "allocation_owner")


def transfer(service, conn, actor, payload):
    from fulfillment import inventory as inv

    original = inv.get(conn, m.inventory_allocations, payload.get("id"))
    require(payload.get("version") == original["version"], "Allocation changed. Reload first.", "revision")
    require(original["status"] == "active", "This allocation is closed.", "closed")
    why, amount = inv.reason(payload), quantity(payload.get("quantity"))
    locked = defaultdict(lambda: Decimal(0))
    for line in inv.lock_lines(conn):
        if line.get("inventory_allocation_id") == original["id"]:
            locked[line["inventory_stock_id"]] += quantity(line["quantity"])
    require(amount <= inv.remaining(original) - sum(locked.values(), Decimal(0)),
            "Transfer only the unconsumed quantity that is not checked out. Return picked stock first.", "active_work")
    customer = identifier(payload.get("customer"), "Customer")
    order = identifier(payload["order"], "Order") if payload.get("order") else ""
    order_line = identifier(payload["order_line"], "Order line") if payload.get("order_line") else ""
    hold = payload.get("hold", original["hold"])
    require(isinstance(hold, bool), "hold must be true or false.", "invalid", 400)
    review_at = inv.timestamp(payload.get("review_at", original.get("review_at")), "Review date", optional=True)
    fields = metadata(payload, original)
    validate_target(service, conn, dict(customer=customer, order=order, order_line=order_line,
                    part=original["part"], uom=original["uom"]))

    selected = []
    if original["source"] == "stock":
        pool = []
        for item in original["items"]:
            stock = inv.get(conn, m.inventory_stock, item["stock_id"])
            require(not stock["serialized"] or amount == int(amount), "Transfer whole serialized units.", "quantity", 400)
            pool.append(dict(stock_id=item["stock_id"], quantity=str(quantity(item["quantity"], zero=True) - locked[item["stock_id"]])))
        selected = inv.take_items(pool, amount)
        deductions = {item["stock_id"]: quantity(item["quantity"]) for item in selected}
        for item in original["items"]:
            item["quantity"] = str(quantity(item["quantity"], zero=True) - deductions.get(item["stock_id"], Decimal(0)))
        original["items"] = [item for item in original["items"] if quantity(item["quantity"], zero=True) > 0]
    else:
        source = inv.get(conn, m.inventory_supplies, original["source_id"])
        require(not source["serialized"] or amount == int(amount), "Transfer whole serialized units.", "quantity", 400)

    stamp, child_id = m.now(), str(uuid4())
    successor = {k: deepcopy(v) for k, v in original.items() if k not in ("transfers", "transferred_quantity")}
    successor.update(id=child_id, version=1, created_at=stamp, actor=actor["id"], quantity=str(amount), fulfilled="0",
                     status="active", customer=customer, order=order, order_line=order_line, items=selected,
                     hold=hold, review_at=review_at, reason=why, policy_managed=False, transfer_from=original["id"], **fields)
    # Preserve historical review provenance, but a promise reassigned to another
    # customer/order no longer consumes the original sales request's quantity.
    # This applies equally to direct supervisor and independently reviewed paths.
    sale_id = original.get("sales_request_id")
    if sale_id is None:
        approval = next((r for r in inv.rows(conn, m.approval_requests)
                         if r["id"] == original.get("approval_request_id")), None)
        sale_id = (approval or {}).get("sales_request_id", "")
    if sale_id:
        sale = inv.get(conn, m.sales_requests, sale_id)
        if customer != sale["customer"] or order and sale.get("order") != order:
            sale_id = ""
    successor["sales_request_id"] = sale_id or ""
    original["quantity"] = str(quantity(original["quantity"]) - amount)
    original["transferred_quantity"] = str(quantity(original.get("transferred_quantity", "0"), zero=True) + amount)
    original.setdefault("transfers", []).append(dict(id=child_id, at=stamp, actor=actor["id"], quantity=str(amount),
        customer=customer, order=order, order_line=order_line, reason=why))
    original["version"] += 1
    if inv.remaining(original) == 0:
        original["status"] = "transferred"
    inv.save(conn, m.inventory_allocations, original)
    inv.save(conn, m.inventory_allocations, successor)
    return {"allocation": successor, "source": original}


def enrich(conn, allocations, supplies, locks):
    """Explain risk from owned facts, without making the inventory view depend on ERP."""
    from fulfillment import inventory as inv, inventory_sync

    administration = m.get_setting(conn, "administration", {})
    timezone = administration.get("rules", {}).get("timezone", "America/Denver")
    today = datetime.now(ZoneInfo(timezone)).date().isoformat()
    sources = {source["id"]: source for source in supplies}
    blocked_stock = inventory_sync.blocked(conn)
    source_healthy = inventory_sync.health(m.get_setting(conn, "inventory_sync", {}))[0]
    budgets = {source["id"]: max(Decimal(0), quantity(source["quantity"], zero=True) - quantity(source["received"], zero=True))
               if source["status"] == "open" else Decimal(0) for source in supplies}
    for allocation in sorted(allocations, key=priority_key):
        allocation.update(metadata({}, allocation))
        remaining = inv.remaining(allocation) if allocation["status"] == "active" else Decimal(0)
        locked = sum((quantity(line["quantity"]) for line in locks if line.get("inventory_allocation_id") == allocation["id"]), Decimal(0))
        allocation.update(locked_quantity=str(locked), transferable_quantity=str(max(Decimal(0), remaining - locked)),
                          shortage="0", expected_arrival=None, risks=[], at_risk=False)
        if remaining <= 0:
            continue
        def risk(code, message):
            allocation["risks"].append(dict(code=code, message=message))
        if allocation.get("review_due"):
            risk("review_due", "The commitment review date has passed; ownership remains protected.")
        if allocation.get("need_by") and allocation["need_by"] < today:
            risk("need_by_passed", "The requested need-by date has passed with quantity still unfulfilled.")
        if allocation["source"] == "stock" and any(item["stock_id"] in blocked_stock for item in allocation.get("items", [])):
            risk("source_quarantine", "Committed stock is blocked by source synchronization; resolve the inventory alert before picking.")
        if allocation["source"] == "work_order":
            if not source_healthy:
                risk("source_unavailable", "A fresh valid VISUAL capture is required to confirm this work-order promise.")
            source = sources.get(allocation["source_id"])
            available = budgets.get(allocation["source_id"], Decimal(0))
            covered = min(remaining, available)
            budgets[allocation["source_id"]] = available - covered
            shortage = remaining - covered
            allocation["shortage"] = str(shortage)
            if not source:
                risk("missing_supply", "The referenced work order is missing; review this promise.")
            elif source["status"] != "open":
                risk("supply_unavailable", "The work order is " + source["status"] + "; its remaining promise is uncovered.")
            else:
                arrival = source.get("due_date") or None
                allocation["expected_arrival"] = arrival
                if arrival and arrival < today:
                    risk("supply_overdue", "Expected work-order arrival has passed; receipt is still outstanding.")
                if allocation.get("need_by") and arrival and arrival > allocation["need_by"]:
                    risk("arrival_after_need_by", "Expected work-order arrival is after the requested need-by date.")
                if allocation.get("need_by") and not arrival:
                    risk("arrival_unknown", "The work order has no expected arrival date for this dated promise.")
            if shortage > 0:
                risk("supply_shortfall", str(shortage) + " " + allocation["uom"] + " is uncovered after earlier-priority commitments.")
        allocation["at_risk"] = bool(allocation["risks"])
