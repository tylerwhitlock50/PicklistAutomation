"""Dispatch-owned stock, commitments and receipt inbox.

All writes run inside Service.command's database transaction. No ERP writes and
no clock-driven release of customer inventory. Reconciliation is evidence, not
an instruction to replace the operational ledger.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select

from fulfillment import model as m
from fulfillment import commitments
from fulfillment.domain import Conflict, identifier, key, quantity, require, trace_key


def rows(conn, table):
    return [deepcopy(r[0]) for r in conn.execute(select(table.c.document))]


def get(conn, table, record_id):
    value = conn.execute(select(table.c.document).where(table.c.id == record_id)).scalar()
    require(value is not None, "Inventory record not found.", "not_found", 404)
    return deepcopy(value)


def save(conn, table, document):
    exists = conn.execute(select(table.c.id).where(table.c.id == document["id"])).first()
    if exists:
        conn.execute(table.update().where(table.c.id == document["id"]).values(document=document))
    else:
        conn.execute(table.insert().values(id=document["id"], document=document))
    return document


def config(conn):
    return m.get_setting(conn, "inventory", dict(enabled=False, site="", warehouse="SHIPPING", location="SHIPPING", version=0))


def reason(payload):
    value = str(payload.get("reason", "")).strip()
    require(0 < len(value) <= 1000, "Record a reason (up to 1,000 characters).", "invalid", 400)
    return value


def timestamp(value, name, *, optional=False):
    if not value and optional:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, name + " must include a timezone.", "invalid", 400)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        raise Conflict(name + " must be an ISO timestamp with timezone.", "invalid", 400)


def remaining(allocation):
    return quantity(allocation["quantity"], zero=True) - quantity(allocation.get("fulfilled", "0"), zero=True)


def active_allocations(conn):
    return [a for a in rows(conn, m.inventory_allocations) if a["status"] == "active" and remaining(a) > 0]


def lock_lines(conn):
    return [line for lock in rows(conn, m.inventory_locks) for line in lock["lines"]]


def committed(conn, stock_id):
    protected = sum((quantity(i["quantity"], zero=True) for a in active_allocations(conn) if a["source"] == "stock"
                     for i in a["items"] if i["stock_id"] == stock_id), Decimal(0))
    unlocked = sum((quantity(l["quantity"]) for l in lock_lines(conn)
                    if l["inventory_stock_id"] == stock_id and not l.get("inventory_allocation_id")), Decimal(0))
    return protected + unlocked


def fingerprint(conn):
    cfg = config(conn)
    return m.digest([[cfg[k] for k in ("site", "warehouse", "location")],
                     sorted([(r["id"], r["quantity"]) for r in rows(conn, m.inventory_stock)])])


def snapshot(conn):
    from fulfillment import inventory_sync
    blocked_stock = inventory_sync.blocked(conn)
    balances = rows(conn, m.inventory_stock)
    for row in balances:
        reserved = committed(conn, row["id"])
        row.update(committed=str(reserved), available=str(quantity(row["quantity"], zero=True) - reserved))
        row["sync_blocked"] = row["id"] in blocked_stock
        if row["sync_blocked"]:
            row["available"] = "0"
    allocations = rows(conn, m.inventory_allocations)
    for a in allocations:
        a["review_due"] = bool(a.get("review_at") and a["review_at"] <= m.now() and a["status"] == "active")
    supplies = rows(conn, m.inventory_supplies)
    for s in supplies:
        s["reserved"] = str(sum((remaining(a) for a in allocations if a["source"] == "work_order" and a["source_id"] == s["id"] and a["status"] == "active"), Decimal(0)))
        usable = quantity(s["quantity"], zero=True) - quantity(s["received"], zero=True) if s["status"] == "open" else Decimal(0)
        s["available"] = str(max(Decimal(0), usable - quantity(s["reserved"], zero=True)))
        s["shortfall"] = str(max(Decimal(0), quantity(s["reserved"], zero=True) - usable))
    commitments.enrich(conn, allocations, supplies, lock_lines(conn))
    return dict(config=config(conn), balances=sorted(balances, key=lambda b: (b["part"], b["serial"])),
                supplies=sorted(supplies, key=lambda s: (s.get("due_date") or "", s["work_order"])),
                allocations=sorted(allocations, key=lambda a: a["created_at"], reverse=True),
                ledger=sorted(rows(conn, m.inventory_ledger), key=lambda r: r["at"], reverse=True)[:250],
                reconciliations=sorted(rows(conn, m.inventory_reconciliations), key=lambda r: r["at"], reverse=True)[:50],
                sync=inventory_sync.status(conn), as_of=m.now())


def ledger(conn, actor, kind, stock, delta, reference, why):
    entry = dict(id=str(uuid4()), at=m.now(), actor=actor["id"], kind=kind, stock_id=stock["id"],
                 part=stock["part"], uom=stock["uom"], serial=stock["serial"], quantity=str(delta),
                 balance=stock["quantity"], reference=reference, reason=why)
    conn.execute(m.inventory_ledger.insert().values(id=entry["id"], document=entry))


def warehouse(conn, payload):
    cfg = config(conn)
    require(bool(cfg["site"]), "Configure the shipping warehouse first.", "configuration")
    for field in ("site", "warehouse", "location"):
        require(identifier(payload.get(field), field) == cfg[field], "Event is outside the configured shipping " + field + ".", "scope")
    return cfg


def serials(payload):
    values = payload.get("serials", [])
    require(isinstance(values, list) and len(values) <= 5000, "serials must be a list of at most 5,000 identities.", "invalid", 400)
    values = [identifier(v, "Serial") for v in values]
    require(len(values) == len(set(values)), "Duplicate serial in the receipt.", "duplicate")
    return values


def inbox(conn, kind, payload):
    event_id = identifier(payload.get("event_id"), "Source event ID")
    source = payload.get("source", "visual")
    require(source in ("manual", "visual"), "Source must be manual or visual.", "invalid", 400)
    record_id = key(config(conn)["site"], source, kind, event_id)
    previous = conn.execute(select(m.inventory_inbox.c.document).where(m.inventory_inbox.c.id == record_id)).scalar()
    if previous:
        require(previous["digest"] == m.digest(payload), "Source event ID has conflicting contents.", "source_conflict")
        return record_id, previous["response"]
    return record_id, None


def finish_inbox(conn, record_id, payload, response):
    conn.execute(m.inventory_inbox.insert().values(id=record_id, document=dict(id=record_id, at=m.now(), digest=m.digest(payload), response=response)))
    return response


def configure(service, conn, actor, payload):
    require(actor.get("role") == "admin", "An administrator must configure inventory.", "role", 403)
    current = config(conn)
    require(payload.get("version") == current["version"], "Inventory settings changed. Reload first.", "revision")
    reason(payload)
    enabled = payload.get("enabled")
    require(isinstance(enabled, bool), "enabled must be true or false.", "invalid", 400)
    proposed = {k: identifier(payload.get(k), k) for k in ("site", "warehouse", "location")}
    source_site = getattr(service.erp, "site", None)
    require(not source_site or proposed["site"] == source_site, "Inventory site must match the configured VISUAL site.", "scope")
    changed_scope = any(proposed[k] != current[k] for k in proposed)
    if changed_scope:
        require(not current["enabled"] and not enabled, "Save the warehouse boundary while disabled, then reconcile it before activation.", "reconciliation")
        require(not rows(conn, m.inventory_stock) and not rows(conn, m.inventory_supplies), "A warehouse with inventory history cannot be remapped.", "scope")
    if enabled != current["enabled"] or changed_scope:
        require(not any(w["state"] not in ("released", "cancelled") for w in service.list_work(conn)), "Finish or return active work before changing inventory ownership.", "active_work")
        require(not m.get_setting(conn, "reservations", []), "Resolve queued legacy reservations before changing inventory ownership.", "reserved")
    if current["enabled"] and not enabled:
        require(not active_allocations(conn) and not any(quantity(s["quantity"], zero=True) for s in rows(conn, m.inventory_stock)),
                "Inventory ownership cannot be disabled while stock or customer commitments remain. Complete a controlled inventory cutover.", "cutover")
    if enabled and not current["enabled"]:
        checks = rows(conn, m.inventory_reconciliations)
        require(checks, "Record a matching opening inventory reconciliation before activation.", "reconciliation")
        latest = max(checks, key=lambda r: r["at"])
        require(latest["status"] == "matched" and latest["fingerprint"] == fingerprint(conn), "Opening reconciliation must match the current ledger before activation.", "reconciliation")
        require(datetime.fromisoformat(latest["observed_at"]) >= datetime.now(timezone.utc) - timedelta(hours=1), "Opening evidence is older than one hour. Reconcile again.", "reconciliation")
        # A confirmed opening count may include a legitimate return from an
        # older ERP-stock-mode shipment. Adopt the present stock without keeping
        # that terminal fulfillment's historical claim as an active lock.
        for stock in rows(conn, m.inventory_stock):
            if not stock["serial"] or not quantity(stock["quantity"], zero=True):
                continue
            resource = trace_key(proposed["site"], stock["part"], stock["serial"])
            owner = conn.execute(select(m.claims.c.work_id).where(m.claims.c.key == resource)).scalar()
            if owner:
                owner_state = conn.execute(select(m.works.c.state).where(m.works.c.id == owner)).scalar()
                require(owner_state in ("released", "cancelled"), "Opening stock still belongs to active or unresolved work.", "reserved")
                conn.execute(m.claims.delete().where(m.claims.c.key == resource))
    updated = dict(**proposed, enabled=enabled, version=current["version"] + 1)
    m.put_setting(conn, "inventory", updated)
    return updated


def add_stock(conn, actor, part, uom, amount, values, reference, why, kind="receipt"):
    existing = [s for s in rows(conn, m.inventory_stock) if s["part"] == part]
    require(all(s["uom"] == uom and s["serialized"] == bool(values) for s in existing), "Part stocking unit or serialization differs from the ledger.", "stock_identity")
    require(not values or amount == len(values), "Serialized quantity must equal the serial count.", "quantity", 400)
    added = []
    for serial in values or [""]:
        stock_id = key(config(conn)["site"], part, uom, serial)
        stock = next((s for s in existing if s["id"] == stock_id), None)
        require(not serial or stock is None, "This serial already has inventory history. Use a reviewed return adjustment.", "duplicate")
        stock = stock or dict(id=stock_id, part=part, uom=uom, serial=serial, serialized=bool(values), quantity="0")
        delta = Decimal(1) if serial else amount
        stock["quantity"] = str(quantity(stock["quantity"], zero=True) + delta)
        save(conn, m.inventory_stock, stock)
        ledger(conn, actor, kind, stock, delta, reference, why)
        added.append({"stock_id": stock_id, "quantity": str(delta)})
    return added


def receive(conn, actor, payload, *, stream=False):
    require(stream or not m.get_setting(conn, "inventory_sync"),
            "The VISUAL connector owns receiving. Use the original source event or a reviewed inventory adjustment.", "source_owner")
    warehouse(conn, payload)
    event_key, previous = inbox(conn, "receipt", payload)
    if previous is not None:
        return previous
    why = reason(payload)
    part, uom = identifier(payload.get("part"), "Part"), identifier(payload.get("uom"), "Stocking unit")
    amount, values = quantity(payload.get("quantity")), serials(payload)
    supply = None
    if payload.get("work_order_id"):
        supply = get(conn, m.inventory_supplies, payload["work_order_id"])
        require(supply["status"] == "open" and supply["part"] == part and supply["uom"] == uom and supply["serialized"] == bool(values), "Receipt does not match an open work-order supply.", "supply")
        require(quantity(supply["received"], zero=True) + amount <= quantity(supply["quantity"], zero=True), "Receipt exceeds the work order's declared supply.", "supply")
    added = add_stock(conn, actor, part, uom, amount, values, payload["event_id"], why)
    if supply:
        supply["received"] = str(quantity(supply["received"], zero=True) + amount)
        supply["version"] += 1
        save(conn, m.inventory_supplies, supply)
        promote(conn, supply, added, payload["event_id"])
    return finish_inbox(conn, event_key, payload, dict(accepted=True, event_id=payload["event_id"], quantity=str(amount), items=added,
        part=part, uom=uom, work_order_id=payload.get("work_order_id")))


def supply(conn, actor, payload):
    require(bool(config(conn)["site"]), "Configure the shipping warehouse first.", "configuration")
    why = reason(payload)
    work_order = identifier(payload.get("work_order"), "Work order identity")
    record_id = key(config(conn)["site"], work_order)
    require(not payload.get("id") or payload["id"] == record_id, "Work-order ID must match its canonical site and work-order identity.", "supply")
    previous = conn.execute(select(m.inventory_supplies.c.document).where(m.inventory_supplies.c.id == record_id)).scalar()
    if previous:
        require(payload.get("version") == previous["version"], "Work-order supply changed. Reload first.", "revision")
    amount = quantity(payload.get("quantity"), zero=True)
    serialized = payload.get("serialized")
    require(isinstance(serialized, bool), "serialized must be true or false.", "invalid", 400)
    require(not serialized or amount == int(amount), "Serialized supply must be a whole number.", "quantity", 400)
    status = payload.get("status", "open")
    require(status in ("open", "closed", "cancelled"), "Invalid work-order status.", "invalid", 400)
    part, uom = identifier(payload.get("part"), "Part"), identifier(payload.get("uom"), "Stocking unit")
    received = quantity(previous["received"], zero=True) if previous else Decimal(0)
    if previous:
        require((work_order, part, uom, serialized) == (previous["work_order"], previous["part"], previous["uom"], previous["serialized"]), "Work-order identity and stocking units cannot change.", "supply")
    reserved = sum((remaining(a) for a in active_allocations(conn) if a["source"] == "work_order" and a["source_id"] == record_id), Decimal(0))
    require(amount >= received, "Total supply cannot be less than already received quantity.", "supply")
    due = str(payload.get("due_date") or "")
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            raise Conflict("Due date must use YYYY-MM-DD.", "invalid", 400)
    # Reductions/cancellation are source facts: retain promises and expose a
    # shortfall, never silently release a customer's promised future supply.
    result = dict(id=record_id, work_order=work_order, part=part, uom=uom, serialized=serialized,
                  quantity=str(amount), received=str(received), status=status, due_date=due,
                  version=(previous["version"] if previous else 0) + 1, reason=why,
                  shortfall=str(max(Decimal(0), reserved - (amount - received if status == "open" else Decimal(0)))))
    if previous:
        for field in ("source", "source_revision"):
            if field in previous:
                result[field] = previous[field]
    return save(conn, m.inventory_supplies, result)


def sync_supply(conn, actor, payload):
    warehouse(conn, payload)
    event_key, previous = inbox(conn, "work_order", payload)
    if previous is not None:
        return previous
    revision = payload.get("source_revision")
    require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
            "source_revision must be a positive monotonic integer.", "invalid", 400)
    record_id = key(config(conn)["site"], identifier(payload.get("work_order")))
    current = conn.execute(select(m.inventory_supplies.c.document).where(m.inventory_supplies.c.id == record_id)).scalar()
    require(not current or revision > current.get("source_revision", 0), "Stale or repeated work-order source revision.", "source_conflict")
    result = supply(conn, actor, {**payload, "id": record_id, "version": current["version"] if current else None})
    result["source_revision"] = revision
    result["source"] = "visual"
    save(conn, m.inventory_supplies, result)
    return finish_inbox(conn, event_key, payload, result)


def take_items(items, amount):
    selected = []
    for item in items:
        available = quantity(item["quantity"], zero=True)
        taken = min(available, amount)
        if taken:
            selected.append(dict(stock_id=item["stock_id"], quantity=str(taken)))
            item["quantity"] = str(available - taken)
            amount -= taken
        if not amount:
            break
    require(amount == 0, "Insufficient uncommitted stock.", "shortage")
    return selected


def promote(conn, supply, received_items, receipt_event_id):
    pool = deepcopy(received_items)
    for allocation in sorted(active_allocations(conn), key=commitments.priority_key):
        if allocation["source"] != "work_order" or allocation["source_id"] != supply["id"]:
            continue
        available = sum((quantity(i["quantity"], zero=True) for i in pool), Decimal(0))
        amount = min(remaining(allocation), available)
        if not amount:
            continue
        child = {**allocation, "id": str(uuid4()), "source": "stock", "source_id": "", "parent_id": allocation["id"],
                 "quantity": str(amount), "fulfilled": "0", "items": take_items(pool, amount), "version": 1, "created_at": m.now(),
                 "receipt_event_id": receipt_event_id}
        save(conn, m.inventory_allocations, child)
        allocation["fulfilled"] = str(quantity(allocation["fulfilled"], zero=True) + amount)
        allocation["version"] += 1
        if not remaining(allocation):
            allocation["status"] = "fulfilled"
        save(conn, m.inventory_allocations, allocation)


def allocate(conn, actor, payload):
    why = reason(payload)
    source = payload.get("source")
    require(source in ("stock", "work_order"), "Choose stock or work_order supply.", "invalid", 400)
    part, uom = identifier(payload.get("part"), "Part"), identifier(payload.get("uom"), "Stocking unit")
    amount = quantity(payload.get("quantity"))
    customer = identifier(payload.get("customer"), "Customer ID")
    hold = payload.get("hold", True)
    require(isinstance(hold, bool), "hold must be true or false.", "invalid", 400)
    order = identifier(payload["order"], "Order") if payload.get("order") else ""
    order_line = identifier(payload["order_line"], "Order line") if payload.get("order_line") else ""
    require(not order_line or order, "An order-line reservation needs an order.", "invalid", 400)
    items, source_id = [], ""
    if source == "work_order":
        s = get(conn, m.inventory_supplies, payload.get("source_id"))
        require(s["status"] == "open" and s["part"] == part and s["uom"] == uom, "Choose matching open work-order supply.", "supply")
        reserved = sum((remaining(a) for a in active_allocations(conn) if a["source"] == source and a["source_id"] == s["id"]), Decimal(0))
        require(amount <= quantity(s["quantity"], zero=True) - quantity(s["received"], zero=True) - reserved, "Work-order supply is already promised or unavailable.", "shortage")
        require(not s["serialized"] or amount == int(amount), "Reserve whole serialized units.", "quantity", 400)
        source_id = s["id"]
    else:
        from fulfillment import inventory_sync
        blocked_stock = inventory_sync.blocked(conn)
        stock = sorted([s for s in rows(conn, m.inventory_stock) if s["part"] == part and s["uom"] == uom and s["id"] not in blocked_stock], key=lambda s: s["id"])
        require(stock, "No matching physical inventory.", "shortage")
        require(not stock[0]["serialized"] or amount == int(amount), "Reserve whole serialized units.", "quantity", 400)
        pool = [dict(stock_id=s["id"], quantity=str(quantity(s["quantity"], zero=True) - committed(conn, s["id"]))) for s in stock]
        items = take_items(pool, amount)
    result = dict(id=str(uuid4()), source=source, source_id=source_id, part=part, uom=uom, customer=customer,
                  order=order, order_line=order_line, quantity=str(amount), fulfilled="0", hold=hold,
                  review_at=timestamp(payload.get("review_at"), "Review date", optional=True), reason=why,
                  status="active", version=1, created_at=m.now(), actor=actor["id"], items=items, **commitments.metadata(payload))
    return save(conn, m.inventory_allocations, result)


def update_allocation(conn, actor, payload):
    a = get(conn, m.inventory_allocations, payload.get("id"))
    require(a["version"] == payload.get("version"), "Allocation changed. Reload first.", "revision")
    require(a["status"] == "active", "This allocation is closed.", "closed")
    require(not any(l.get("inventory_allocation_id") == a["id"] for l in lock_lines(conn)), "Return checked-out stock before changing its allocation.", "active_work")
    a["reason"] = reason(payload)
    # Only a human-authorized allocation update acknowledges the currently
    # mirrored destination. Automatic receipt promotion merely inherits this
    # marker, so its version increment cannot clear an order-change review.
    if a.get("order"):
        from fulfillment import order_book
        mirrored = order_book.get(conn, config(conn)["site"], a["order"])
        if mirrored:
            a["order_review"] = dict(id=str(uuid4()), at=m.now(), actor=actor["id"], reason=a["reason"],
                destination_digest=m.digest(mirrored["order"].get("destination")))
    a.update(commitments.metadata(payload, a))
    if "hold" in payload:
        require(isinstance(payload["hold"], bool), "hold must be true or false.", "invalid", 400)
        a["hold"] = payload["hold"]
    if "review_at" in payload:
        a["review_at"] = timestamp(payload["review_at"], "Review date", optional=True)
    if "status" in payload:
        require(payload["status"] == "released", "Only an explicit release is permitted.", "invalid", 400)
        a["status"] = "released"
    a["version"] += 1
    return save(conn, m.inventory_allocations, a)


def accumulate(conn, actor, plans):
    """Persist the existing customer release rules' protected accumulation.

    Only uncommitted prescription buckets become new holds. Existing allocations
    remain sticky, including when order priority or release rules later change.
    """
    for plan in plans:
        grouped = {}
        for line in plan["lines"]:
            if line.get("inventory_allocation_id"):
                continue
            identity = (str(line["order_line"]), line["part"], line["uom"])
            grouped.setdefault(identity, []).append(line)
        for (order_line, part, uom), lines in grouped.items():
            items = []
            for line in lines:
                s = get(conn, m.inventory_stock, line["inventory_stock_id"])
                amount = quantity(line["quantity"])
                require(quantity(s["quantity"], zero=True) - committed(conn, s["id"]) >= amount, "Accumulation plan is stale.", "shortage")
                items.append(dict(stock_id=s["id"], quantity=str(amount)))
            amount = sum((quantity(i["quantity"]) for i in items), Decimal(0))
            save(conn, m.inventory_allocations, dict(id=str(uuid4()), source="stock", source_id="", part=part, uom=uom,
                 customer=identifier(plan["customer_id"]), order=plan["order"], order_line=order_line, quantity=str(amount),
                 fulfilled="0", hold=False, review_at=None, reason="Protected accumulation under customer release policy",
                 status="active", version=1, created_at=m.now(), actor=actor, items=items, policy_managed=True))


def plan_lines(conn, order, consumed=None):
    """Plan from owned stock; consumed is a planning-only shared stock/claim map."""
    consumed = consumed if consumed is not None else {}
    cfg, allocations, locks = config(conn), active_allocations(conn), lock_lines(conn)
    require(order["site"] == cfg["site"], "Order belongs to another inventory site.", "scope")
    from fulfillment import inventory_sync
    blocked_stock = inventory_sync.blocked(conn)
    stock = sorted([s for s in rows(conn, m.inventory_stock) if s["id"] not in blocked_stock], key=lambda s: s["id"])
    planned = []
    for demand in order["lines"]:
        needed = quantity(demand["quantity"], zero=True)
        customer = str(order.get("customer_id", "")).upper()
        own = [a for a in allocations if a["source"] == "stock" and a["customer"] == customer and not a["hold"]
               and (not a["order"] or a["order"] == order["order"])
               and (not a["order_line"] or a["order_line"] == str(demand["order_line"]))]
        for s in stock:
            if s["part"] != demand["part"] or s["uom"] != demand["uom"] or s["serialized"] != demand["serialized"]:
                continue
            buckets = []
            for a in sorted(own, key=lambda a: (not bool(a["order_line"]), not bool(a["order"]), commitments.priority_key(a))):
                protected = sum((quantity(i["quantity"], zero=True) for i in a["items"] if i["stock_id"] == s["id"]), Decimal(0))
                used = sum((quantity(l["quantity"]) for l in locks if l.get("inventory_allocation_id") == a["id"] and l["inventory_stock_id"] == s["id"]), Decimal(0))
                buckets.append((a["id"], protected - used))
            buckets.append(("", quantity(s["quantity"], zero=True) - committed(conn, s["id"])))
            for allocation_id, available in buckets:
                pool_key = s["id"] + ":" + allocation_id
                available -= consumed.get(pool_key, Decimal(0))
                take = min(needed, max(Decimal(0), available))
                if not take:
                    continue
                planned.append({**demand, "quantity": str(take), "serials": [s["serial"]] if s["serialized"] else [],
                                "warehouse": cfg["warehouse"], "location": cfg["location"],
                                "inventory_stock_id": s["id"], "inventory_allocation_id": allocation_id})
                consumed[pool_key] = consumed.get(pool_key, Decimal(0)) + take
                needed -= take
                if not needed:
                    break
            if not needed:
                break
    return planned


def checkout(conn, work):
    expected = plan_lines(conn, work)
    fields = ("order_line", "part", "uom", "quantity", "inventory_stock_id", "inventory_allocation_id")
    require([{f: l.get(f) for f in fields} for l in expected] == [{f: l.get(f) for f in fields} for l in work["lines"]], "Inventory commitments changed. Replan the order.", "shortage")
    save(conn, m.inventory_locks, {"id": work["id"], "lines": deepcopy(work["lines"]), "at": m.now()})
    work["inventory_owned"] = True


def check_pick(conn, work, line, payload):
    from fulfillment import inventory_sync
    inventory_sync.require_healthy(conn)
    require(line["inventory_stock_id"] not in inventory_sync.blocked(conn), "This stock is under source review. Resolve the discrepancy before picking.", "sync_variance")
    lock = get(conn, m.inventory_locks, work["id"])
    require(any(l["inventory_stock_id"] == line["inventory_stock_id"] and l["id"] == line["id"] for l in lock["lines"]), "Stock is not checked out to this work.", "reserved")
    s = get(conn, m.inventory_stock, line["inventory_stock_id"])
    require(quantity(s["quantity"], zero=True) >= quantity(line["quantity"]), "Checked-out inventory is no longer present.", "shortage")
    if s["serialized"]:
        require(identifier(payload.get("serial")) == s["serial"], "Serial does not match the local inventory claim.", "wrong_serial")


def release(conn, actor, work):
    from fulfillment import inventory_sync
    inventory_sync.require_healthy(conn)
    get(conn, m.inventory_locks, work["id"])
    for line in work["lines"]:
        stock = get(conn, m.inventory_stock, line["inventory_stock_id"])
        amount = quantity(line["quantity"])
        require(quantity(stock["quantity"], zero=True) >= amount, "Stock debit would be negative.", "shortage")
        stock["quantity"] = str(quantity(stock["quantity"], zero=True) - amount)
        save(conn, m.inventory_stock, stock)
        ledger(conn, actor, "shipment", stock, -amount, work["packlist"], "Released fulfillment " + work["id"])
        if line.get("inventory_allocation_id"):
            a = get(conn, m.inventory_allocations, line["inventory_allocation_id"])
            need = amount
            for item in a["items"]:
                if item["stock_id"] == stock["id"]:
                    take = min(quantity(item["quantity"], zero=True), need)
                    item["quantity"] = str(quantity(item["quantity"], zero=True) - take)
                    need -= take
            require(need == 0, "Shipment no longer matches its customer allocation.", "reserved")
            a["fulfilled"] = str(quantity(a["fulfilled"], zero=True) + amount)
            a["version"] += 1
            if not remaining(a):
                a["status"] = "fulfilled"
            save(conn, m.inventory_allocations, a)
    conn.execute(m.inventory_locks.delete().where(m.inventory_locks.c.id == work["id"]))


def cancel(conn, work):
    conn.execute(m.inventory_locks.delete().where(m.inventory_locks.c.id == work["id"]))


def adjust(conn, actor, payload):
    why = reason(payload)
    try:
        delta = Decimal(str(payload.get("quantity")))
        require(delta.is_finite() and delta != 0, "Adjustment quantity must be nonzero.", "quantity", 400)
        quantity(abs(delta))
    except InvalidOperation:
        raise Conflict("Invalid adjustment quantity.", "quantity", 400)
    part, uom, values = identifier(payload.get("part")), identifier(payload.get("uom")), serials(payload)
    require(not values or abs(delta) == len(values), "Serialized adjustment quantity must equal the serial count.", "quantity", 400)
    stock = [s for s in rows(conn, m.inventory_stock) if s["part"] == part and s["uom"] == uom]
    require(not stock or stock[0]["serialized"] == bool(values), "Adjustment must match existing inventory serialization.", "stock_identity")
    missing = [serial for serial in values or [""] if not any(s["serial"] == serial for s in stock)]
    evidence = None
    if missing:
        from fulfillment import inventory_sync
        source = m.get_setting(conn, "inventory_sync", {})
        require(delta > 0 and source.get("reconciliation_id"), "Use a receipt for new inventory identities; connected source corrections require current reconciliation evidence.", "stock_identity")
        inventory_sync.require_healthy(conn)
        evidence = get(conn, m.inventory_reconciliations, source["reconciliation_id"])
        require(evidence["fingerprint"] == fingerprint(conn), "Inventory changed after this comparison. Synchronize again before adding a corrected identity.", "stale_snapshot")
        differences = {(d["part"], d["uom"], d["serial"]): Decimal(d["delta"]) for d in evidence["differences"]}
        require(all(differences.get((part, uom, serial), Decimal(0)) >= (Decimal(1) if serial else delta) for serial in missing),
                "New identities must be present in the latest accepted VISUAL comparison. Verify the physical count and synchronize first.", "stock_identity")
    for serial in values or [""]:
        s = next((s for s in stock if s["serial"] == serial), None)
        if s is None:
            add_stock(conn, actor, part, uom, Decimal(1) if serial else delta, [serial] if serial else [],
                      evidence["id"], why, kind="adjustment")
            continue
        change = (Decimal(1) if delta > 0 else Decimal(-1)) if serial else delta
        balance = quantity(s["quantity"], zero=True) + change
        require(balance >= committed(conn, s["id"]) and (not serial or balance in (0, 1)), "Adjustment would consume protected stock or duplicate a serial. Resolve commitments first.", "reserved")
        if serial and change > 0:
            resource = trace_key(config(conn)["site"], part, serial)
            owner = conn.execute(select(m.claims.c.work_id).where(m.claims.c.key == resource)).scalar()
            if owner:
                owner_state = conn.execute(select(m.works.c.state).where(m.works.c.id == owner)).scalar()
                require(owner_state in ("released", "cancelled"), "The returned serial still belongs to active work.", "reserved")
                conn.execute(m.claims.delete().where(m.claims.c.key == resource))
        s["quantity"] = str(balance)
        save(conn, m.inventory_stock, s)
        ledger(conn, actor, "adjustment", s, change, why, why)
    return {"accepted": True}


def reconcile(conn, actor, payload):
    warehouse(conn, payload)
    event_key, previous = inbox(conn, "reconciliation", payload)
    if previous is not None:
        return previous
    observed = timestamp(payload.get("observed_at"), "Observation time")
    require(datetime.fromisoformat(observed) <= datetime.now(timezone.utc) + timedelta(minutes=1), "Snapshot cannot be in the future.", "invalid", 400)
    history = rows(conn, m.inventory_reconciliations)
    require(not history or observed > max(r["observed_at"] for r in history), "An older snapshot cannot replace newer reconciliation evidence.", "stale_snapshot")
    balances = payload.get("balances")
    require(isinstance(balances, list) and len(balances) <= 5000, "Provide a complete warehouse balances list (up to 5,000 rows).", "invalid", 400)
    external = {}
    for row in balances:
        require(isinstance(row, dict), "Invalid balance row.", "invalid", 400)
        part, uom, values = identifier(row.get("part")), identifier(row.get("uom")), serials(row)
        amount = quantity(row.get("quantity"), zero=True)
        require(not values or amount == len(values), "Snapshot serial quantity mismatch.", "quantity", 400)
        for serial in values or [""]:
            identity = (part, uom, serial)
            require(identity not in external, "Snapshot contains duplicate stock identities.", "duplicate")
            external[identity] = Decimal(1) if serial else amount
    local = {(s["part"], s["uom"], s["serial"]): quantity(s["quantity"], zero=True) for s in rows(conn, m.inventory_stock)}
    differences = [dict(part=k[0], uom=k[1], serial=k[2], local=str(local.get(k, 0)), visual=str(external.get(k, 0)),
                        delta=str(external.get(k, Decimal(0)) - local.get(k, Decimal(0))))
                   for k in sorted(set(local) | set(external)) if local.get(k, 0) != external.get(k, 0)]
    result = dict(id=str(uuid4()), at=m.now(), observed_at=observed, status="variance" if differences else "matched",
                  differences=differences, fingerprint=fingerprint(conn), event_id=payload["event_id"],
                  **{k: config(conn)[k] for k in ("site", "warehouse", "location")})
    save(conn, m.inventory_reconciliations, result)
    return finish_inbox(conn, event_key, payload, result)


def execute(service, conn, actor, action, payload):
    require(actor.get("role") in ("admin", "supervisor", "inventory_integration"), "A supervisor must change inventory or commitments.", "role", 403)
    if actor.get("role") == "inventory_integration":
        require(action in ("receive", "sync-supply", "reconcile", "sync-batch", "sync-health"), "Integration scope does not allow this command.", "role", 403)
    if action in ("sync-batch", "sync-health"):
        from fulfillment import inventory_sync
        require(actor.get("role") == "inventory_integration", "Use the scoped connector endpoint for source batches.", "role", 403)
        if action == "sync-health":
            return inventory_sync.report_failure(conn, payload)
        return inventory_sync.apply(service, conn, actor, payload)
    if action in ("sync-ack", "link-receipt"):
        from fulfillment import inventory_sync
        return (inventory_sync.acknowledge if action == "sync-ack" else inventory_sync.link_receipt)(conn, actor, payload)
    if action in ("supply", "sync-supply"):
        require(not m.get_setting(conn, "inventory_sync"), "The VISUAL connector owns work-order supply. Correct it at the source.", "source_owner")
    if action == "configure":
        return configure(service, conn, actor, payload)
    if action == "allocation-transfer":
        return commitments.transfer(service, conn, actor, payload)
    if action == "allocate":
        commitments.validate_target(service, conn, payload)
    actions = {"receive": receive, "supply": supply, "sync-supply": sync_supply, "allocate": allocate, "allocation-update": update_allocation,
               "adjust": adjust, "reconcile": reconcile}
    require(action in actions, "Unknown inventory action.", "not_found", 404)
    return actions[action](conn, actor, payload)
