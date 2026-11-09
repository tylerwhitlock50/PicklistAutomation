"""Atomic inbound inventory snapshots with a local-ledger concurrency fence.

The producer persists its complete source identity map and pending envelope.
This receiver never treats a source balance as permission to overwrite stock.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from fulfillment import inventory as inv, model as m
from fulfillment.domain import identifier, key, quantity, require

FRESHNESS_SECONDS = 300


def health(state):
    if not state.get("stream_id"):
        return True, state.get("status")
    observed = datetime.fromisoformat(state["observed_at"])
    fresh = (datetime.now(timezone.utc) - observed).total_seconds() <= FRESHNESS_SECONDS
    return fresh and not state.get("source_error"), ("source_error" if state.get("source_error") else
                                                     "stale" if not fresh else state["status"])


def require_healthy(conn):
    require(health(m.get_setting(conn, "inventory_sync", {}))[0],
            "The inventory source needs a fresh valid capture before shipping can continue.", "source_unavailable", 503)


def stock_version(conn):
    return m.digest([inv.fingerprint(conn), conn.execute(select(func.count()).select_from(m.inventory_ledger)).scalar()])


def status(conn):
    state = m.get_setting(conn, "inventory_sync", {})
    fresh, effective_status = health(state)
    return {**state, "connected": bool(state.get("stream_id")), "sequence": state.get("sequence", 0),
            "status": effective_status, "fresh": fresh, "freshness_seconds": FRESHNESS_SECONDS,
            "stock_version": stock_version(conn), "config": inv.config(conn),
            "issues": list(m.get_setting(conn, "inventory_sync_issues", {}).values())}


def blocked(conn):
    if not health(m.get_setting(conn, "inventory_sync", {}))[0]:
        return {s["id"] for s in inv.rows(conn, m.inventory_stock)}
    return set(m.get_setting(conn, "inventory_sync_blocked", []))


def report_failure(conn, payload):
    state = m.get_setting(conn, "inventory_sync", {})
    require(state and state["stream_id"] == payload.get("stream_id") and state["sequence"] == payload.get("sequence"),
            "Health report belongs to an older or different source capture.", "source_sequence")
    require(payload.get("source") == "visual" and payload.get("code") in ("capture_failed", "delivery_rejected", "delivery_pending", "delivery_confirmed"),
            "Invalid connector health report.", "invalid", 400)
    if payload["code"] == "delivery_confirmed":
        state["delivery_confirmed_sequence"] = state["sequence"]
        state["delivery_confirmed_at"] = m.now()
        if state.get("source_error", {}).get("code") == "delivery_pending":
            state.pop("source_error", None)
        m.put_setting(conn, "inventory_sync", state)
        return {"accepted": True, "delivery_confirmed_sequence": state["sequence"]}
    state["source_error"] = dict(code=payload["code"], at=m.now(),
        message="The source capture failed validation or delivery. Stock remains unavailable until a fresh valid batch arrives.")
    m.put_setting(conn, "inventory_sync", state)
    return {"accepted": True, "source_error": state["source_error"]}


def issue(conn, item):
    issues = m.get_setting(conn, "inventory_sync_issues", {})
    issues[item["id"]] = {**item, "at": m.now()}
    m.put_setting(conn, "inventory_sync_issues", issues)


def acknowledge(conn, actor, payload):
    issue_id = str(payload.get("id") or "")
    issues = m.get_setting(conn, "inventory_sync_issues", {})
    require(issue_id in issues, "Synchronization issue not found.", "not_found", 404)
    why = inv.reason(payload)
    resolved = {**issues.pop(issue_id), "resolved_at": m.now(), "resolved_by": actor["id"], "resolution": why}
    m.put_setting(conn, "inventory_sync_issues", issues)
    # The Service command event retains the resolution. Stock quarantines are
    # cleared only by a fresh comparison, never by acknowledging an alert.
    return resolved


def apply(service, conn, actor, payload):
    cfg = inv.warehouse(conn, payload)
    require(payload.get("source") == "visual", "A source batch must identify VISUAL.", "invalid", 400)
    batch_key, previous = inv.inbox(conn, "batch", payload)
    if previous is not None:
        return previous
    stream = identifier(payload.get("stream_id"), "Source stream")
    seq = payload.get("sequence")
    require(isinstance(seq, int) and not isinstance(seq, bool) and seq > 0, "Sequence must be a positive integer.", "invalid", 400)
    current = m.get_setting(conn, "inventory_sync", {})
    opening = payload.get("kind") == "opening"
    require(payload.get("kind") in ("opening", "delta"), "Invalid source batch kind.", "invalid", 400)
    require((opening and not current and seq == 1) or
            (not opening and current.get("stream_id") == stream and seq == current.get("sequence", 0) + 1),
            "Source stream or sequence differs. Restore the connector state before proceeding.", "source_sequence")
    require(payload.get("expected_stock_version") == stock_version(conn),
            "Stock changed during source capture. Capture again before comparing.", "capture_stale", 503)
    observed = inv.timestamp(payload.get("observed_at"), "Source capture time")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds()
    require(-60 <= age <= 120, "Source capture is stale. Capture again.", "capture_stale", 503)
    require(not current or observed > current["observed_at"], "Source capture must advance in time.", "capture_stale", 503)
    if opening:
        require(not cfg["enabled"] and not inv.rows(conn, m.inventory_stock), "Opening import requires disabled inventory and an empty local stock ledger.", "opening_stock")
        require(not any(w["state"] not in ("released", "cancelled") for w in service.list_work(conn)), "Complete active work before importing opening inventory.", "active_work")
        require(not m.get_setting(conn, "reservations", []), "Resolve legacy reservations before opening import.", "reserved")
    supplies, receipts, balances = payload.get("work_orders", []), payload.get("receipts", []), payload.get("balances")
    require(isinstance(supplies, list) and len(supplies) <= 5000 and isinstance(receipts, list) and len(receipts) <= 5000,
            "Source batch exceeds the bounded event limit.", "invalid", 400)
    require(isinstance(balances, list), "A complete source balance snapshot is required.", "invalid", 400)
    base = {k: cfg[k] for k in ("site", "warehouse", "location")}
    base["source"] = "visual"
    if opening:
        require(not receipts, "Opening balances replace pre-cutoff receipt events.", "opening_stock")
        receipts = [{**b, "event_id": "OPEN:" + key(stream, b.get("part"), b.get("uom")),
                     "reason": "Verified opening snapshot " + stream} for b in balances if quantity(b.get("quantity"), zero=True)]
    # Several physical movements may occur between captures. Repeated incoming
    # trace identities across distinct events need review as a group; numeric
    # transaction IDs cannot prove a safe receive/ship/return chronology.
    receipt_traces = {}
    for r in receipts:
        require(isinstance(r, dict), "Invalid receipt event.", "invalid", 400)
        part = identifier(r.get("part"), "Part")
        for serial in inv.serials(r):
            identity = (part, serial)
            receipt_traces[identity] = receipt_traces.get(identity, 0) + 1
    normalized, receipt_ids = [], set()
    linked = {}
    for r in receipts:
        require(isinstance(r, dict), "Invalid receipt event.", "invalid", 400)
        r = {**r, **base}
        if r.get("work_order"):
            r["work_order_id"] = key(cfg["site"], identifier(r["work_order"]))
        receipt_id, replay = inv.inbox(conn, "receipt", r)
        require(receipt_id not in receipt_ids, "Duplicate receipt event in source batch.", "duplicate")
        receipt_ids.add(receipt_id)
        if replay is None:
            part, uom = identifier(r.get("part"), "Part"), identifier(r.get("uom"), "Stocking unit")
            amount, values = quantity(r.get("quantity")), inv.serials(r)
            require(not values or amount == len(values), "Serialized quantity must equal the serial count.", "quantity", 400)
            existing = [s for s in inv.rows(conn, m.inventory_stock) if s["part"] == part]
            require(all(s["uom"] == uom and s["serialized"] == bool(values) for s in existing),
                    "Part stocking unit or serialization differs from the ledger.", "stock_identity")
            if not opening and (any(s["serial"] in values for s in existing)
                                or any(receipt_traces[(part, serial)] > 1 for serial in values)):
                inv.reason(r)
                # Rework/returns can legitimately revisit a known trace. Retain
                # the entire receipt as a review-only event, including mixed
                # old/new traces, so reconciliation can supply count evidence.
                # Never receive just its new portion or grow future supply.
                replay = inv.finish_inbox(conn, receipt_id, r,
                    dict(accepted=True, review_required=True, event_id=r["event_id"], quantity=str(amount),
                         items=[], part=part, uom=uom, work_order_id=r.get("work_order_id")))
                issue(conn, dict(id="RECEIPT:" + r["event_id"], kind="receipt_review", event_id=r["event_id"],
                                 part=part, uom=uom, quantity=str(amount), serials=values,
                                 work_order_id=r.get("work_order_id"),
                                 message="Arrival reuses a serial from inventory history or another receipt in this batch. Verify the return/rework and use a reviewed count adjustment; no stock was received."))
        # A replayed receipt does not increment received below, so it must not
        # inflate total work-order supply during the pre-receipt reconciliation.
        if r.get("work_order_id") and replay is None:
            linked[r["work_order_id"]] = linked.get(r["work_order_id"], Decimal(0)) + quantity(r["quantity"])
        normalized.append(r)
    source_ids, final_status = set(), {}
    for s in supplies:
        require(isinstance(s, dict), "Invalid supply event.", "invalid", 400)
        wo = identifier(s.get("work_order"), "Work order")
        source_id = key(cfg["site"], wo)
        require(source_id not in source_ids, "Duplicate work order in source batch.", "duplicate")
        source_ids.add(source_id)
        existing = conn.execute(select(m.inventory_supplies.c.document).where(m.inventory_supplies.c.id == source_id)).scalar()
        local_received = quantity(existing["received"], zero=True) if existing else Decimal(0)
        amount = local_received + quantity(s.get("remaining"), zero=True) + linked.get(source_id, Decimal(0))
        source_status = s.get("status")
        require(source_status in ("open", "closed", "cancelled"), "Invalid source work-order status.", "invalid", 400)
        data = {**s, **base, "work_order": wo, "quantity": str(amount), "event_id": "WO:" + key(stream, seq, wo),
                "source_revision": seq, "reason": "VISUAL source snapshot", "status": "open" if linked.get(source_id) else source_status}
        inv.sync_supply(conn, actor, data)
        final_status[source_id] = source_status
    require(set(linked).issubset(source_ids), "Every linked receipt must include current work-order evidence.", "supply")
    for r in normalized:
        result = inv.receive(conn, actor, r, stream=True)
        if not opening and not result.get("review_required") and not r.get("work_order_id"):
            # Keep the physical receipt usable but expose missing provenance for
            # future promises of this same part; never guess from a matching SKU.
            if any(a["source"] == "work_order" and a["part"] == r["part"] for a in inv.active_allocations(conn)):
                issue(conn, dict(id="RECEIPT:" + r["event_id"], kind="unlinked_receipt", event_id=r["event_id"],
                                 part=r["part"], quantity=r["quantity"], message="Arrival has no work-order link. Match the receipt to its production source if applicable."))
    for record_id, desired_status in final_status.items():
        s = inv.get(conn, m.inventory_supplies, record_id)
        if s["status"] != desired_status:
            s["status"], s["version"] = desired_status, s["version"] + 1
            inv.save(conn, m.inventory_supplies, s)
    reported = payload.get("issues", [])
    require(isinstance(reported, list) and len(reported) <= 5000, "Invalid source issue list.", "invalid", 400)
    for item in reported:
        require(isinstance(item, dict) and item.get("id") and item.get("message"), "Invalid source issue.", "invalid", 400)
        issue(conn, item)
    comparison = inv.reconcile(conn, actor, {**base, "event_id": "SNAP:" + key(stream, seq), "observed_at": observed, "balances": balances})
    negatives = {(d["part"], d["uom"], d["serial"]) for d in comparison["differences"] if Decimal(d["delta"]) < 0}
    m.put_setting(conn, "inventory_sync_blocked", [s["id"] for s in inv.rows(conn, m.inventory_stock)
        if (s["part"], s["uom"], s["serial"]) in negatives])
    evidence = retain_movements(conn, payload, stream, seq, observed, opening)
    state = dict(stream_id=stream, sequence=seq, observed_at=observed, last_success=m.now(),
                 status=comparison["status"], source_manifest=payload.get("source_manifest"),
                 reconciliation_id=comparison["id"], receipts=len(normalized), supplies=len(supplies),
                 balances=deepcopy(balances), movement_evidence=evidence)
    if opening:
        m.put_setting(conn, "inventory_opening", dict(stream_id=stream, observed_at=observed, balances=deepcopy(balances),
            stock=inv.rows(conn, m.inventory_stock), ledger_ids=[r["id"] for r in inv.rows(conn, m.inventory_ledger)],
            ledger_index={r["id"]: m.digest(r) for r in inv.rows(conn, m.inventory_ledger)},
            movement_index=movement_index(conn), evidence=deepcopy(evidence)))
    m.put_setting(conn, "inventory_sync", state)
    return inv.finish_inbox(conn, batch_key, payload, {"accepted": True, **state})



def movement_index(conn):
    return {r["source_id"]: dict(hash=r["hash"], deleted=r["deleted"]) for r in inv.rows(conn, m.inventory_source_movements)}


def retain_movements(conn, payload, stream, sequence, observed, opening):
    """Retain source versions independently of receipt idempotency and stock."""
    evidence = payload.get("movement_evidence", {})
    if evidence.get("version") != 2:
        return dict(version=1, complete=False, reason="Connector upgrade required for transaction evidence.")
    changes = payload.get("movement_changes")
    require(isinstance(changes, list) and len(changes) <= 100000,
            "Complete source movement changes must be a bounded list.", "invalid", 400)
    require(evidence.get("base_sequence") == sequence - 1 and evidence.get("pending_before_capture") is False,
            "Source evidence checkpoint is incomplete.", "source_sequence")
    seen = set()
    for change in changes:
        require(isinstance(change, dict), "Invalid movement evidence.", "invalid", 400)
        identity = identifier(change.get("id"), "Source movement")
        require(identity not in seen, "Duplicate source movement evidence.", "duplicate")
        seen.add(identity)
        record_id = key(stream, identity)
        old = conn.execute(select(m.inventory_source_movements.c.document).where(m.inventory_source_movements.c.id == record_id)).scalar()
        deleted = change.get("deleted")
        require(isinstance(deleted, bool), "Movement deletion must be explicit.", "invalid", 400)
        movement = deepcopy(change.get("movement")) if not deleted else deepcopy(old.get("movement") if old else None)
        if not deleted:
            require(isinstance(movement, dict) and str(movement.get("id")) == identity,
                    "Movement identity differs from evidence.", "invalid", 400)
            for field in ("part", "uom"):
                require(identifier(movement.get(field)) == movement[field], "Invalid movement stock identity.", "invalid", 400)
            try:
                amount = Decimal(str(movement.get("quantity")))
            except Exception:
                require(False, "Invalid movement quantity.", "invalid", 400)
            require(amount.is_finite() and amount.as_tuple().exponent >= -8, "Invalid movement quantity.", "invalid", 400)
            require(movement.get("kind") in ("receipt", "shipment", "other"), "Invalid source movement kind.", "invalid", 400)
            require(isinstance(movement.get("serials"), list), "Movement traces must be retained.", "invalid", 400)
            trace_ids = set()
            total = Decimal(0)
            for trace in movement["serials"]:
                require(isinstance(trace, dict), "Invalid movement trace.", "invalid", 400)
                serial = identifier(trace.get("serial"), "Movement serial")
                require(serial not in trace_ids, "Duplicate movement serial.", "duplicate")
                trace_ids.add(serial)
                try:
                    value = Decimal(str(trace.get("quantity")))
                except Exception:
                    require(False, "Invalid movement trace quantity.", "invalid", 400)
                require(value in (Decimal(1), Decimal(-1)), "Movement traces must have signed unit quantities.", "invalid", 400)
                total += value
            require(not trace_ids or total == amount, "Movement traces and quantity disagree.", "quantity", 400)
        movement_hash = m.digest(movement) if not deleted else (old or {}).get("hash", change.get("prior_hash"))
        if old and old["hash"] == movement_hash and old["deleted"] == deleted:
            continue
        version = dict(sequence=sequence, observed_at=observed, hash=movement_hash, deleted=deleted, movement=movement)
        record = dict(id=record_id, source_id=identity, stream_id=stream, movement=movement, hash=movement_hash,
                      deleted=deleted, opening=opening or bool((old or {}).get("opening")),
                      first_sequence=(old or {}).get("first_sequence", sequence),
                      first_observed_at=(old or {}).get("first_observed_at", observed),
                      versions=[*(old or {}).get("versions", []), version])
        inv.save(conn, m.inventory_source_movements, record)
        from fulfillment.daily_close import mark_source_impact
        mark_source_impact(conn, record, old, observed)
    rows = inv.rows(conn, m.inventory_source_movements)
    present = sorted((r["source_id"], r["hash"]) for r in rows if not r["deleted"])
    require(evidence.get("count") == len(present) and evidence.get("manifest") == m.digest(present),
            "Source movement manifest is incomplete or conflicts with retained history.", "source_evidence")
    return deepcopy(evidence)


def link_receipt(conn, actor, payload):
    """Attach existing free stock to a future promise, without another receipt."""
    event_id = identifier(payload.get("event_id"), "Receipt event ID")
    why = inv.reason(payload)
    receipt_id = key(inv.config(conn)["site"], "visual", "receipt", event_id)
    receipt = inv.get(conn, m.inventory_inbox, receipt_id)["response"]
    require(not receipt.get("review_required"),
            "This arrival requires a reviewed count adjustment; it cannot be linked as received work-order supply.", "receipt_review")
    require(not receipt.get("work_order_id"), "This receipt already has a work-order link.", "supply")
    supply = inv.get(conn, m.inventory_supplies, payload.get("work_order_id"))
    require(supply["version"] == payload.get("version"), "Work-order supply changed. Reload first.", "revision")
    require(supply["part"] == receipt.get("part") and supply["uom"] == receipt.get("uom"), "Receipt does not match the work order's part and stocking unit.", "supply")
    links = m.get_setting(conn, "inventory_receipt_links", {})
    require(event_id not in links, "This receipt has already been matched.", "duplicate")
    for item in receipt["items"]:
        require(item["stock_id"] not in blocked(conn), "Receipt stock is under source review. Restore a fresh matching source capture before linking.", "sync_variance")
        stock = inv.get(conn, m.inventory_stock, item["stock_id"])
        require(stock["serialized"] == supply["serialized"] and quantity(stock["quantity"], zero=True) - inv.committed(conn, stock["id"]) >= quantity(item["quantity"]),
                "Receipt stock is already consumed or committed. Resolve those allocations before matching.", "reserved")
    amount = quantity(receipt["quantity"])
    supply["received"] = str(quantity(supply["received"], zero=True) + amount)
    supply["quantity"] = str(quantity(supply["quantity"], zero=True) + amount)
    supply["version"] += 1
    inv.save(conn, m.inventory_supplies, supply)
    inv.promote(conn, supply, deepcopy(receipt["items"]), event_id)
    links[event_id] = dict(work_order_id=supply["id"], at=m.now(), actor=actor["id"], reason=why)
    m.put_setting(conn, "inventory_receipt_links", links)
    issues = m.get_setting(conn, "inventory_sync_issues", {})
    issues.pop("RECEIPT:" + event_id, None)
    m.put_setting(conn, "inventory_sync_issues", issues)
    return {"matched": True, "event_id": event_id, "work_order_id": supply["id"], "quantity": str(amount)}
