"""Durable read-only VISUAL demand mirror. It never creates a commercial order."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fulfillment import admin, inventory, model as m
from fulfillment.domain import Conflict, complete_address, identifier, quantity, require

FRESH_SECONDS = 300
CLOSED = {"C", "X", "closed", "cancelled", "missing"}


def get(conn, site, order):
    from sqlalchemy import select
    record = conn.execute(select(m.order_book.c.document).where(m.order_book.c.id == m.digest([site, order]))).scalar()
    return deepcopy(record) if record else None


def state(conn):
    result = deepcopy(m.get_setting(conn, "order_book_state", {}))
    stamp = result.get("observed_at")
    result["stale"] = not stamp or (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > FRESH_SECONDS
    result["available"] = bool(stamp) and not result["stale"] and not result.get("error")
    return result


def _normal(service, conn, raw):
    require(isinstance(raw, dict), "An order object is required.", "invalid", 400)
    order = deepcopy(raw)
    for field in ("site", "order", "customer_id"):
        order[field] = identifier(raw.get(field), field)
    expected = inventory.config(conn).get("site") or getattr(service.erp, "site", None)
    require(not expected or order["site"] == expected, "Order is outside this Dispatch site.", "scope")
    order["status"] = str(order.get("status", "R"))
    order["customer"] = str(order.get("customer") or order["customer_id"])
    order["ship_to"] = str(order.get("ship_to") or "")
    require(isinstance(order.get("destination"), dict), "An order destination object is required.", "invalid", 400)
    lines = order.get("lines")
    require(isinstance(lines, list) and len(lines) <= 5000, "Order lines must be a list of at most 5,000 lines.", "invalid", 400)
    seen = set()
    for line in lines:
        require(isinstance(line, dict), "Order line must be an object.", "invalid", 400)
        for field in ("order_line", "part", "uom"):
            line[field] = identifier(line.get(field), field)
        require(line["order_line"] not in seen, "Duplicate order line identity.", "duplicate")
        seen.add(line["order_line"])
        line["quantity"] = str(quantity(line.get("quantity"), zero=True))
        require(isinstance(line.get("serialized"), bool), "Order lines require serialized true or false.", "invalid", 400)
        line.setdefault("status", "A")
    order.setdefault("baseline", {l["order_line"]: {"ordered": l.get("ordered", l["quantity"]),
        "shipped": l.get("shipped", "0"), "part": l["part"], "uom": l["uom"]} for l in lines})
    order.setdefault("blocks", [])
    return order


def _candidate(order, line):
    return dict(CUST_ORDER_ID=order["order"], CUSTOMER_ID=order["customer_id"], CUSTOMER_NAME=order["customer"],
        SHIP_TO_ID=order["ship_to"], LINE_NO=line["order_line"], PART_ID=line["part"],
        ITEM_TYPE="guns" if line["serialized"] else "components", OPEN_QTY=line["quantity"], AVAILABLE_QTY=0,
        ORDER_RELEASED=int(order["status"] == "R"), LINE_AVAILABLE=int(line.get("status", "A") == "A"),
        CREDIT_APPROVED=int(order.get("credit_approved", False)), SHIP_TO_PRESENT=int(bool(order["ship_to"])),
        PROMISE_SHIP_DATE=line.get("promise_ship_date"), PROMISE_DEL_DATE=line.get("promise_del_date"),
        DESIRED_SHIP_DATE=order.get("desired_ship_date"), ORDER_DATE=order.get("order_date"))


def _conflicts(conn, order, previous):
    conflicts = []
    lines = {l["order_line"]: l for l in order["lines"]}
    allocations = [a for a in inventory.active_allocations(conn) if a.get("order") == order["order"]]
    all_allocations = {a["id"]: a for a in inventory.rows(conn, m.inventory_allocations)}
    destination_digest = m.digest(order.get("destination"))
    for allocation in allocations:
        reason = None
        line = lines.get(str(allocation.get("order_line"))) if allocation.get("order_line") else None
        matching = [l for l in lines.values() if l["part"] == allocation["part"] and l["uom"] == allocation["uom"] and l.get("status", "A") == "A"]
        eligible = [line] if line and line in matching else ([] if allocation.get("order_line") else matching)
        required = sum((inventory.remaining(a) for a in allocations if a["customer"] == allocation["customer"]
            and a["part"] == allocation["part"] and a["uom"] == allocation["uom"]
            and (not allocation.get("order_line") or a.get("order_line") in (None, "", allocation["order_line"]))), Decimal(0))
        if order["status"] in CLOSED:
            reason = "Order closed, cancelled or missing from complete source snapshot"
        elif allocation["customer"] != order["customer_id"]:
            reason = "Order customer changed"
        elif not eligible or required > sum((quantity(l["quantity"], zero=True) for l in eligible), Decimal(0)):
            reason = "Committed quantity exceeds remaining eligible demand"
        elif previous and previous["order"].get("destination") != order.get("destination") and allocation.get("order_review", {}).get("destination_digest") != destination_digest:
            reason = "Order destination changed; review the promise"
        if not reason and previous:
            lineage, ancestor = {allocation["id"]}, allocation
            while ancestor.get("parent_id") or ancestor.get("transfer_from"):
                parent_id = ancestor.get("parent_id") or ancestor.get("transfer_from")
                if parent_id in lineage or parent_id not in all_allocations:
                    break
                lineage.add(parent_id)
                ancestor = all_allocations[parent_id]
            retained = next((c for c in previous.get("conflicts", []) if c["allocation_id"] in lineage and "destination" in c["reason"].lower()), None)
            review = allocation.get("order_review", {})
            reviewed = retained and review.get("id") and review.get("id") != retained.get("order_review_id") and review.get("destination_digest") == destination_digest
            reason = retained["reason"] if retained and not reviewed else None
        if reason:
            conflicts.append(dict(allocation_id=allocation["id"], allocation_version=allocation["version"],
                order_review_id=allocation.get("order_review", {}).get("id"), destination_digest=destination_digest, reason=reason, owner="Inside Sales",
                next_action="Review and explicitly release or transfer the affected commitment; picked stock remains protected."))
    return conflicts


def _store(service, conn, raw, observed_at, source_revision, candidates=None, event_revision=None):
    order = _normal(service, conn, raw)
    identity = m.digest([order["site"], order["order"]])
    previous = get(conn, order["site"], order["order"])
    if previous and observed_at < previous["observed_at"]:
        return {**previous, "ignored_older_revision": True}
    facts_digest = m.digest(order)
    document = dict(id=identity, order=order, observed_at=observed_at, source_revision=source_revision,
        facts_digest=facts_digest, version=(previous or {}).get("version", 0) + int(not previous or previous["facts_digest"] != facts_digest),
        first_observed_at=(previous or {}).get("first_observed_at", observed_at),
        candidates=deepcopy(candidates) if candidates is not None else [_candidate(order, l) for l in order["lines"]],
        conflicts=_conflicts(conn, order, previous), last_event_revision=event_revision if event_revision is not None else (previous or {}).get("last_event_revision"),
        last_event_digest=(previous or {}).get("last_event_digest"))
    if previous and previous["facts_digest"] != facts_digest:
        document["previous_revision"] = dict(source_revision=previous["source_revision"], facts_digest=previous["facts_digest"], observed_at=previous["observed_at"])
    return inventory.save(conn, m.order_book, document)


def upsert(service, conn, actor, payload):
    """Machine ingestion: monotonic event revision plus observed-at ordering."""
    from fulfillment import planning
    event_id = identifier(payload.get("event_id"), "Source event ID")
    revision = payload.get("source_revision")
    require(type(revision) is int and revision >= 0, "source_revision must be a nonnegative integer.", "invalid", 400)
    observed = inventory.timestamp(payload.get("observed_at"), "observed_at")
    require(datetime.fromisoformat(observed) <= datetime.now(timezone.utc) + timedelta(seconds=30), "Order observation cannot be in the future.", "invalid", 400)
    raw = _normal(service, conn, payload.get("order"))
    token = "order-event:" + m.digest([raw["site"], event_id])
    existing = m.get_setting(conn, token)
    if existing:
        require(existing["digest"] == m.digest(payload), "Order event ID has conflicting contents.", "source_conflict")
        return existing["response"]
    previous = get(conn, raw["site"], raw["order"])
    if previous and previous.get("last_event_revision") is not None and revision <= previous["last_event_revision"]:
        require(revision != previous["last_event_revision"] or previous.get("last_event_digest") == m.digest(raw),
                "Same source revision has different order facts.", "source_conflict")
        result = {**previous, "ignored_older_revision": True}
    else:
        result = _store(service, conn, raw, observed, revision, None, revision)
        if not result.get("ignored_older_revision"):
            result["last_event_digest"] = m.digest(raw)
            inventory.save(conn, m.order_book, result)
            planning.enqueue(conn, actor, "order:upsert", raw["order"])
    m.put_setting(conn, token, dict(digest=m.digest(payload), response=result))
    # A single event is not proof that the complete order book is fresh.
    return result


def refresh(service, conn, actor):
    """Only a successful complete read may mark absent orders as missing."""
    from fulfillment import planning
    context = admin.context(conn)
    captured = service.erp.order_snapshot(context)
    require(captured.get("complete") is True and isinstance(captured.get("orders"), list), "Source order snapshot is incomplete.", "order_source", 503)
    observed = inventory.timestamp(captured.get("observed_at") or m.now(), "observed_at")
    rows = captured["orders"]
    keys = [(r["site"], r["order"]) for r in rows]
    require(len(keys) == len(set(keys)), "Source order snapshot has duplicate identities.", "order_source", 503)
    candidate_map = {}
    for row in captured.get("candidates", []):
        candidate_map.setdefault(row["CUST_ORDER_ID"], []).append(row)
    changes = []
    for raw in rows:
        previous = get(conn, raw["site"], raw["order"])
        record = _store(service, conn, raw, observed, m.digest(raw), candidate_map.get(raw["order"], []))
        if not previous or previous["facts_digest"] != record["facts_digest"]:
            changes.append(raw["order"])
    site = captured.get("site") or inventory.config(conn).get("site") or getattr(service.erp, "site", "DEMO")
    for previous in inventory.rows(conn, m.order_book):
        raw = previous["order"]
        if raw["site"] == site and (raw["site"], raw["order"]) not in keys and raw.get("status") not in CLOSED:
            raw = {**raw, "status": "missing", "blocks": ["Absent from complete VISUAL open-order snapshot; review closure/cancellation"]}
            _store(service, conn, raw, observed, m.digest(raw), [])
            changes.append(raw["order"])
    record = dict(site=site, observed_at=observed, complete=True, error=None, revision=m.digest([(r["site"], r["order"], m.digest(r)) for r in rows]),
                  history=captured.get("history", []), order_count=len(rows))
    m.put_setting(conn, "order_book_state", record)
    if changes:
        planning.enqueue(conn, actor, "order:snapshot", {"orders": changes})
    return record


def fail(conn, exc):
    current = m.get_setting(conn, "order_book_state", {})
    current.update(error={"at": m.now(), "code": getattr(exc, "code", "source_unavailable"),
        "message": exc.message if isinstance(exc, Conflict) else "VISUAL order refresh failed. Existing orders and commitments were retained."})
    m.put_setting(conn, "order_book_state", current)


def demand(service, conn, context):
    source = state(conn)
    require(source["available"], "VISUAL order mirror is stale or unavailable; refresh order intake before shipping.", "order_source", 503)
    works = service.list_work(conn)
    occupied = {w["order"] for w in works if w["state"] not in ("cancelled", "released")}
    local = datetime.now(ZoneInfo(context["timezone"])).date()
    through = local + timedelta(days=context["lookahead_days"])
    orders, candidates = [], []
    for record in sorted(inventory.rows(conn, m.order_book), key=lambda r: (r["order"].get("order_date") or "", r["order"]["order"])):
        order = deepcopy(record["order"])
        if order["status"] in CLOSED or order["order"] in occupied:
            continue
        # Released work remains unavailable until mirrored cumulative shipments confirm consumption.
        pending = False
        for work in works:
            if work["order"] != order["order"] or work["state"] != "released":
                continue
            for line in work["lines"]:
                n = str(line["order_line"])
                total = sum((quantity(l["quantity"]) for l in work["lines"] if str(l["order_line"]) == n), Decimal(0))
                if quantity(order.get("baseline", {}).get(n, {}).get("shipped", "0"), zero=True) < quantity(work.get("baseline", {}).get(n, {}).get("shipped", "0"), zero=True) + total:
                    pending = True
        if pending:
            continue
        rows = []
        for row in record["candidates"]:
            due = row.get("PROMISE_DEL_DATE") or row.get("PROMISE_SHIP_DATE") or row.get("DESIRED_SHIP_DATE")
            if due and datetime.fromisoformat(str(due)).date() > through:
                continue
            if quantity(row.get("OPEN_QTY", "0"), zero=True) > 0 and row.get("LINE_AVAILABLE", 1):
                rows.append(deepcopy(row))
        if not rows:
            continue
        allowed = {str(r["LINE_NO"]) for r in rows}
        order["lines"] = [l for l in order["lines"] if l["order_line"] in allowed]
        order["blocks"] = list(order.get("blocks", [])) + admin.order_blocks(context, order["order"], order.get("document_fingerprint", ""))
        if record["conflicts"]:
            order["blocks"].append("Commitment needs review after order change")
        order["order_revision"] = record["source_revision"]
        order["order_observed_at"] = record["observed_at"]
        orders.append(order)
        candidates.extend(rows)
    return {"orders": orders, "candidates": candidates, "history": source.get("history", [])}
