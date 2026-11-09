"""Sales intake and independently reviewed operational requests.

Every mutation runs under Service.command's warehouse transaction/savepoint.
Drafts and submissions never reserve inventory. Approval executes the existing
inventory guards and stores its outcome in the same transaction as the review.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select

from fulfillment import admin, commitments, inventory as inv, model as m
from fulfillment.domain import Conflict, address, complete_address, identifier, quantity, require


HUMAN_ROLES = ("sales", "operator", "supervisor", "admin")
REVIEW_ROLES = ("supervisor", "admin")
KINDS = {
    "allocation": "allocate",
    "transfer": "allocation-transfer",
    "allocation_change": "allocation-update",
    "adjustment": "adjust",
    "early_release": "control",
}
TERM_FIELDS = {
    "allocation": {"source", "source_id", "part", "uom", "customer", "order", "order_line", "quantity", "hold", "review_at", "priority", "need_by", "request_reference", "reason"},
    "transfer": {"id", "version", "quantity", "customer", "order", "order_line", "hold", "review_at", "priority", "need_by", "request_reference", "reason"},
    "allocation_change": {"id", "version", "hold", "status", "review_at", "priority", "need_by", "request_reference", "reason"},
    "adjustment": {"part", "uom", "quantity", "serials", "reason"},
    "early_release": {"order", "expires_at", "reason"},
}


def human(conn, actor, *, reviewer=False, admin_only=False):
    user = conn.execute(select(m.users).where(m.users.c.id == actor.get("id"))).first()
    allowed = ("admin",) if admin_only else REVIEW_ROLES if reviewer else HUMAN_ROLES
    require(user and user.enabled and user.role in allowed and actor.get("role") == user.role,
            "An active authorized human account is required.", "role", 403)
    return {"id": user.id, "name": user.name, "role": user.role}


def revision(record, payload):
    require(type(payload.get("version")) is int and payload["version"] == record["version"],
            "This request changed. Reload and review its current terms.", "revision")


def owner(record, actor):
    require(record["requester"] == actor["id"] or actor["role"] == "admin",
            "Only the requester or an administrator may edit or cancel this request.", "role", 403)


def transition(record, actor, action, reason, *, status=None, detail=None):
    """Append complete revision evidence; no endpoint edits prior history entries."""
    stamp = m.now()
    if status:
        record["status"] = status
    record["version"] += 1
    record["updated_at"] = stamp
    record.setdefault("history", []).append(dict(at=stamp, actor=actor["id"], role=actor["role"],
        action=action, reason=reason, version=record["version"], status=record["status"],
        detail=deepcopy(detail or {})))
    return record


def new_record(actor):
    return dict(id=str(uuid4()), version=0, requester=actor["id"], requester_name=actor["name"],
                status="draft", created_at=m.now(), history=[])


def snapshot(conn):
    sales = inv.rows(conn, m.sales_requests)
    requests = inv.rows(conn, m.approval_requests)
    allocations = {a["id"]: a for a in inv.rows(conn, m.inventory_allocations)}
    for request in requests:
        allocation_id = request.get("allocation_id")
        if allocation_id in allocations:
            allocation = allocations[allocation_id]
            request["progress"] = {k: deepcopy(allocation[k]) for k in ("id", "status", "source", "quantity", "fulfilled", "hold", "version")}
            request["progress"]["children"] = [
                {k: a[k] for k in ("id", "status", "source", "quantity", "fulfilled", "hold")}
                for a in allocations.values() if a.get("parent_id") == allocation_id]
    return dict(sales_requests=sorted(sales, key=lambda r: r["created_at"], reverse=True),
                approval_requests=sorted(requests, key=lambda r: r["created_at"], reverse=True),
                kinds=list(KINDS), as_of=m.now())


def sales_terms(conn, payload):
    customer = identifier(payload.get("customer"), "Customer")
    site = identifier(payload.get("site"), "Site")
    require(site == inv.config(conn)["site"], "Sales request must belong to the shipping site.", "scope")
    reference = str(payload.get("customer_reference", "") or "").strip()
    require(0 < len(reference) <= 200 and not any(ord(c) < 32 for c in reference),
            "Record a customer PO/reference (maximum 200 characters).", "invalid", 400)
    destination = payload.get("destination")
    require(isinstance(destination, dict) and complete_address(destination), "Record the complete requested destination.", "invalid", 400)
    lines = payload.get("lines")
    require(isinstance(lines, list) and 0 < len(lines) <= 200, "Provide 1–200 requested lines.", "invalid", 400)
    normalized = []
    for index, line in enumerate(lines):
        require(isinstance(line, dict), "Each requested line must be an object.", "invalid", 400)
        normalized.append(dict(id=identifier(line.get("id", str(index + 1)), "Request line"),
            part=identifier(line.get("part"), "Part"), uom=identifier(line.get("uom"), "Stocking unit"),
            quantity=str(quantity(line.get("quantity")))))
    require(len({line["id"] for line in normalized}) == len(normalized), "Request line identities must be unique.", "duplicate", 400)
    metadata = commitments.metadata({"need_by": payload.get("need_by", "")})
    return dict(customer=customer, site=site, customer_reference=reference, destination=address(destination),
                need_by=metadata["need_by"], lines=normalized, reason=inv.reason(payload))


def duplicates(conn, request, binding_order=None):
    ref = request["customer_reference"].casefold()
    matches = [r["id"] for r in inv.rows(conn, m.sales_requests)
               if r["id"] != request["id"] and r["status"] != "cancelled"
               and r["customer"] == request["customer"] and r["site"] == request["site"]
               and r["customer_reference"].casefold() == ref]
    for record in inv.rows(conn, m.order_book):
        order = record["order"]
        if (order["site"] == request["site"] and order["customer_id"] == request["customer"]
                and order["order"] != binding_order and str(order.get("customer_reference", "")).strip().casefold() == ref):
            matches.append("visual:" + order["order"])
    return sorted(matches)


def sales_allocation_id(conn, allocation):
    if "sales_request_id" in allocation:
        return allocation["sales_request_id"]
    request = next((r for r in inv.rows(conn, m.approval_requests) if r["id"] == allocation.get("approval_request_id")), None)
    return (request or {}).get("sales_request_id", "")


def sales_allocations(conn, sales_id, *, active=True):
    records = inv.active_allocations(conn) if active else inv.rows(conn, m.inventory_allocations)
    return [a for a in records if sales_allocation_id(conn, a) == sales_id]


def sales_accounted_quantity(conn, sale, part, uom):
    total = Decimal(0)
    for allocation in sales_allocations(conn, sale["id"], active=False):
        if allocation["part"] != part or allocation["uom"] != uom:
            continue
        # Future fulfilled means promoted, not shipped: its stock children carry
        # the same sales identity and count their quantity exactly once instead.
        if allocation["source"] == "stock":
            total += quantity(allocation["fulfilled"], zero=True)
        if allocation["status"] == "active":
            total += inv.remaining(allocation)
    return total


def shipped_quantity(line):
    return quantity(line.get("shipped", "0"), zero=True)


def inspect_open_order(service, order_id):
    identity = identifier(order_id, "VISUAL order")
    try:
        order = service.erp.inspect_order(identity)
    except Conflict:
        raise
    except Exception as exc:
        raise Conflict("The VISUAL order cannot be rechecked. Restore the source and retry this review.", "order_source", 503) from exc
    status = str(order.get("status", order.get("order_status", ""))).upper()
    require(status not in ("C", "X", "CLOSED", "CANCELLED", "CANCELED", "CANCEL"),
            "The VISUAL order is closed or cancelled.", "order_closed")
    require(not order.get("cancelled") and not order.get("closed"), "The VISUAL order is closed or cancelled.", "order_closed")
    return order


def open_lines(order):
    return [line for line in order.get("lines", [])
            if str(line.get("status", line.get("line_status", ""))).upper() not in ("C", "X", "CLOSED", "CANCELLED", "CANCELED")
            and not line.get("cancelled") and line.get("line_available", True)
            and quantity(line.get("quantity", "0"), zero=True) > 0]


def sales(service, conn, actor, action, payload):
    if action == "sales-create":
        record = {**new_record(actor), **sales_terms(conn, payload)}
        transition(record, actor, "created", record["reason"], detail={k: record[k] for k in ("customer", "site", "customer_reference", "destination", "need_by", "lines")})
    else:
        record = inv.get(conn, m.sales_requests, identifier(payload.get("id"), "Sales request").lower())
        revision(record, payload)
        why = inv.reason(payload)
        if action == "sales-bind":
            human(conn, actor, reviewer=True)
            require(record["status"] == "awaiting_visual_order", "Submit the sales request before binding an ERP order.", "state")
            duplicate_ids = duplicates(conn, record, identifier(payload.get("order"), "VISUAL order"))
            if duplicate_ids:
                require(actor["id"] != record["requester"], "Another authorized person must review duplicate customer references.", "self_approval", 403)
                duplicate_reason = str(payload.get("duplicate_reference_review", "")).strip()
                require(3 <= len(duplicate_reason) <= 1000, "Review the duplicate customer references and record the disposition.", "duplicate_reference")
            else:
                duplicate_reason = ""
            order = inspect_open_order(service, payload.get("order"))
            require(order["site"] == record["site"] and order.get("customer_id") == record["customer"],
                    "The VISUAL order belongs to another customer or site.", "allocation_owner")
            require("customer_reference" not in order or str(order["customer_reference"] or "").strip().casefold() == record["customer_reference"].casefold(),
                    "The VISUAL customer PO/reference differs from the submitted request.", "customer_reference")
            require(address(order.get("destination", {})) == record["destination"],
                    "VISUAL destination differs from the submitted request. Correct and resubmit the request first.", "destination")
            bindings = payload.get("line_bindings")
            require(isinstance(bindings, list) and len(bindings) == len(record["lines"]), "Bind every requested line to an exact VISUAL line.", "invalid", 400)
            mapping = {}
            for binding in bindings:
                require(isinstance(binding, dict), "Line binding must be an object.", "invalid", 400)
                request_line, order_line = identifier(binding.get("request_line")), identifier(binding.get("order_line"))
                require(request_line not in mapping, "Each requested line must be bound once.", "duplicate", 400)
                mapping[request_line] = order_line
            require(set(mapping) == {line["id"] for line in record["lines"]}, "Line bindings do not match this request revision.", "revision")
            valid_lines = {str(line["order_line"]): line for line in open_lines(order)}
            totals = {}
            for line in record["lines"]:
                target = valid_lines.get(mapping[line["id"]])
                require(target and target["part"] == line["part"] and target["uom"] == line["uom"],
                        "Each binding must match an open VISUAL part and stocking unit.", "order_line")
                line_id = str(target["order_line"])
                totals[line_id] = totals.get(line_id, Decimal(0)) + quantity(line["quantity"])
            cursors = {line_id: shipped_quantity(line) for line_id, line in valid_lines.items()}
            for previous in inv.rows(conn, m.sales_requests):
                if previous["id"] != record["id"] and previous["status"] == "bound" and previous["site"] == record["site"] and previous.get("order") == order["order"]:
                    for binding in previous["line_bindings"]:
                        line_id = binding["order_line"]
                        if line_id not in totals:
                            continue
                        amount = quantity(binding["quantity"])
                        if "shipment_end" in binding and line_id in valid_lines:
                            end = quantity(binding["shipment_end"], zero=True)
                            amount = min(amount, max(Decimal(0), end - shipped_quantity(valid_lines[line_id])))
                            cursors[line_id] = max(cursors[line_id], end)
                        if amount:
                            totals[line_id] = totals.get(line_id, Decimal(0)) + amount
                            if "shipment_end" not in binding and line_id in cursors:
                                # Older records without cumulative shipped proof
                                # remain conservatively occupied; never infer a
                                # shipment from a reduction in open quantity.
                                cursors[line_id] += amount
            require(all(line in valid_lines and amount <= quantity(valid_lines[line]["quantity"], zero=True) for line, amount in totals.items()),
                    "Bound requests exceed the remaining VISUAL line demand.", "order_quantity")
            assigned = []
            for line in record["lines"]:
                line_id = mapping[line["id"]]
                start, end = cursors[line_id], cursors[line_id] + quantity(line["quantity"])
                assigned.append(dict(request_line=line["id"], order_line=line_id, part=line["part"], uom=line["uom"],
                    quantity=line["quantity"], source_shipped_at_binding=str(shipped_quantity(valid_lines[line_id])),
                    shipment_start=str(start), shipment_end=str(end)))
                cursors[line_id] = end
            record.update(order=order["order"], line_bindings=assigned,
                          bound_at=m.now(), bound_by=actor["id"], source_fingerprint=m.digest(order),
                          duplicate_references=duplicate_ids, duplicate_reference_review=duplicate_reason)
            transition(record, actor, "bound", why, status="bound", detail={k: record[k] for k in ("order", "line_bindings", "source_fingerprint", "duplicate_references", "duplicate_reference_review")})
        else:
            owner(record, actor)
            require(record["status"] in ("draft", "awaiting_visual_order"), "This sales request is already bound or cancelled.", "state")
            if action == "sales-update":
                require(not sales_allocations(conn, record["id"]), "Review and release linked commitments before revising this sales request.", "reserved")
                record.update(sales_terms(conn, payload))
                transition(record, actor, "revised", why, status="draft", detail={k: record[k] for k in ("customer", "site", "customer_reference", "destination", "need_by", "lines")})
            elif action == "sales-submit":
                require(record["status"] == "draft", "This sales request was already submitted.", "state")
                record["duplicate_references"] = duplicates(conn, record)
                transition(record, actor, "submitted", why, status="awaiting_visual_order", detail={"duplicate_references": record["duplicate_references"]})
            elif action == "sales-cancel":
                require(not sales_allocations(conn, record["id"]), "Review and release linked commitments before cancelling this sales request.", "reserved")
                transition(record, actor, "cancelled", why, status="cancelled")
            else:
                require(False, "Unknown sales request command.", "not_found", 404)
    return inv.save(conn, m.sales_requests, record)


def normalized_terms(kind, payload):
    require(isinstance(kind, str) and kind in KINDS, "Choose a supported approval request kind.", "invalid", 400)
    terms = payload.get("terms")
    require(isinstance(terms, dict), "Provide the exact requested terms.", "invalid", 400)
    require(not set(terms) - TERM_FIELDS[kind], "Request contains unsupported terms.", "invalid", 400)
    terms = deepcopy(terms)
    terms["reason"] = inv.reason(terms)
    if kind in ("allocation", "transfer"):
        terms.update(customer=identifier(terms.get("customer"), "Customer"), quantity=str(quantity(terms.get("quantity"))))
        for field in ("order", "order_line"):
            terms[field] = identifier(terms[field], field) if terms.get(field) else ""
        require(not terms["order_line"] or terms["order"], "An order-line reservation needs an order.", "invalid", 400)
    if kind in ("allocation", "adjustment"):
        for field in ("part", "uom"):
            terms[field] = identifier(terms.get(field), field)
    if kind == "allocation":
        require(terms.get("source") in ("stock", "work_order"), "Choose stock or work_order supply.", "invalid", 400)
        if terms["source"] == "work_order":
            terms["source_id"] = identifier(terms.get("source_id"), "Supply").lower()
        terms.setdefault("hold", True)
    if kind in ("transfer", "allocation_change"):
        terms["id"] = identifier(terms.get("id"), "Allocation").lower()
        require(type(terms.get("version")) is int and terms["version"] > 0, "Record the allocation version being reviewed.", "invalid", 400)
    if kind in ("allocation", "transfer", "allocation_change"):
        if "hold" in terms:
            require(type(terms["hold"]) is bool, "hold must be true or false.", "invalid", 400)
        if "review_at" in terms:
            terms["review_at"] = inv.timestamp(terms["review_at"], "Review date", optional=True)
        if kind == "allocation" or any(k in terms for k in ("priority", "need_by", "request_reference")):
            metadata = commitments.metadata(terms)
            for field, value in metadata.items():
                if kind == "allocation" or field in terms:
                    terms[field] = value
    if kind == "allocation_change":
        require(any(k in terms for k in ("hold", "status", "review_at", "priority", "need_by", "request_reference")), "Choose an allocation change.", "invalid", 400)
        require("status" not in terms or terms["status"] == "released", "Only an explicit release is permitted.", "invalid", 400)
    if kind == "adjustment":
        try:
            delta = Decimal(str(terms.get("quantity")))
            require(delta.is_finite() and delta != 0, "Adjustment quantity must be nonzero.", "quantity", 400)
            quantity(abs(delta))
        except InvalidOperation:
            require(False, "Enter a valid adjustment quantity.", "quantity", 400)
        terms["quantity"] = str(delta)
        terms["serials"] = inv.serials(terms)
        require(not terms["serials"] or abs(delta) == len(terms["serials"]), "Adjustment quantity must equal the serial count.", "quantity", 400)
    if kind == "early_release":
        terms["order"] = identifier(terms.get("order"), "Order")
        terms["expires_at"] = admin.expiry(terms.get("expires_at"), required=True)
    return terms


def related_sales(conn, sales_id, terms, kind):
    if not sales_id:
        return ""
    sale = inv.get(conn, m.sales_requests, identifier(sales_id, "Sales request").lower())
    require(sale["status"] in ("awaiting_visual_order", "bound"), "Link a submitted, active sales request.", "sales_request")
    require(kind == "allocation" and sale["customer"] == terms["customer"] and sale["site"] == inv.config(conn)["site"],
            "A sales request may link only to its customer's allocation.", "sales_request")
    require(sum((quantity(line["quantity"]) for line in sale["lines"] if line["part"] == terms["part"] and line["uom"] == terms["uom"]), Decimal(0)) >= quantity(terms["quantity"]),
            "The commitment must fit a requested part, stocking unit and quantity.", "sales_request")
    require(not terms.get("order") or sale["status"] == "bound" and terms["order"] == sale["order"],
            "An unbound sales draft cannot authorize an order-bound commitment.", "sales_request")
    if terms.get("order_line"):
        require(any(binding["order_line"] == terms["order_line"] and binding["part"] == terms["part"] and binding["uom"] == terms["uom"] for binding in sale["line_bindings"]),
                "The commitment line is not bound to this sales request.", "sales_request")
    return sale["id"]


def validate_order_capacity(service, conn, terms, *, original=None):
    if not terms.get("order"):
        return None
    order = inspect_open_order(service, terms["order"])
    require(order["site"] == inv.config(conn)["site"] and order.get("customer_id") == terms["customer"],
            "The order does not belong to the requested customer and site.", "allocation_owner")
    lines = [line for line in open_lines(order) if line["part"] == terms["part"] and line["uom"] == terms["uom"]
             and (not terms.get("order_line") or str(line["order_line"]) == terms["order_line"])]
    require(lines, "No matching open VISUAL order line remains.", "order_line")
    line_ids = {str(line["order_line"]) for line in lines}
    existing = Decimal(0)
    for allocation in inv.active_allocations(conn):
        if allocation["order"] != terms["order"] or allocation["part"] != terms["part"] or allocation["uom"] != terms["uom"]:
            continue
        if not allocation.get("order_line") or allocation["order_line"] in line_ids:
            existing += inv.remaining(allocation)
            if original and allocation["id"] == original["id"]:
                existing -= quantity(terms["quantity"])
    require(existing + quantity(terms["quantity"]) <= sum((quantity(line["quantity"]) for line in lines), Decimal(0)),
            "Commitments would exceed current open VISUAL demand. Review the requested quantity.", "order_quantity")
    return m.digest(order)


def request_evidence(conn, kind, terms):
    evidence = {"inventory_scope": inv.config(conn), "inventory_fingerprint": inv.fingerprint(conn)}
    if kind in ("transfer", "allocation_change"):
        evidence["allocation"] = inv.get(conn, m.inventory_allocations, terms["id"])
        require(evidence["allocation"]["version"] == terms["version"], "Allocation changed. Revise the request before submission.", "revision")
    if kind == "allocation" and terms["source"] == "work_order":
        evidence["supply"] = inv.get(conn, m.inventory_supplies, terms["source_id"])
    return evidence


def usable_commitment(conn, allocation):
    """Reassignment or removing a hold cannot certify an uncovered promise."""
    if allocation["source"] == "work_order":
        supply = inv.get(conn, m.inventory_supplies, allocation["source_id"])
        promises = sum((inv.remaining(a) for a in inv.active_allocations(conn)
                        if a["source"] == "work_order" and a["source_id"] == supply["id"]), Decimal(0))
        require(supply["status"] == "open" and quantity(supply["quantity"], zero=True) - quantity(supply["received"], zero=True) >= promises,
                "Future supply no longer covers these commitments. Review the production shortfall first.", "supply")
    else:
        from fulfillment import inventory_sync
        blocked = inventory_sync.blocked(conn)
        require(not any(item["stock_id"] in blocked for item in allocation.get("items", [])),
                "Committed stock has an unresolved source variance. Reconcile it before reassignment or hold release.", "source_unavailable")


def approve(service, conn, actor, record):
    kind, terms = record["kind"], deepcopy(record["terms"])
    human(conn, actor, reviewer=True, admin_only=kind == "early_release")
    require(actor["id"] != record["requester"], "The requester cannot approve their own request.", "self_approval", 403)
    require(record.get("submitted_digest") == m.digest([kind, terms, record.get("sales_request_id", "")]),
            "Submitted terms changed; revise and submit for fresh review.", "revision")
    scope = record["submission_evidence"]["inventory_scope"]
    require(all(inv.config(conn)[key] == scope[key] for key in ("site", "warehouse", "location")),
            "The shipping scope changed after submission. Review the request again.", "scope")
    related_sales(conn, record.get("sales_request_id"), terms, kind)
    if record.get("sales_request_id"):
        sale = inv.get(conn, m.sales_requests, record["sales_request_id"])
        limit = sum((quantity(line["quantity"]) for line in sale["lines"] if line["part"] == terms["part"] and line["uom"] == terms["uom"]), Decimal(0))
        promised = sales_accounted_quantity(conn, sale, terms["part"], terms["uom"])
        require(promised + quantity(terms["quantity"]) <= limit, "Approved commitments would exceed the sales request quantity.", "sales_request")
    if m.get_setting(conn, "inventory_sync") and (kind in ("allocation", "transfer", "adjustment", "early_release") or
            kind == "allocation_change" and terms.get("hold") is False and terms.get("status") != "released"):
        from fulfillment import inventory_sync
        inventory_sync.require_healthy(conn)
    if kind == "adjustment":
        require(record["submission_evidence"]["inventory_fingerprint"] == inv.fingerprint(conn),
                "Inventory changed after the adjustment request. Recount and resubmit the exact correction.", "revision")
    source_fingerprint = None
    if kind == "allocation":
        source_fingerprint = validate_order_capacity(service, conn, terms)
    elif kind == "transfer":
        original = inv.get(conn, m.inventory_allocations, terms["id"])
        usable_commitment(conn, original)
        source_fingerprint = validate_order_capacity(service, conn, {**terms, "part": original["part"], "uom": original["uom"]}, original=original)
    elif kind == "allocation_change" and terms.get("hold") is False and terms.get("status") != "released":
        original = inv.get(conn, m.inventory_allocations, terms["id"])
        usable_commitment(conn, original)
        source_fingerprint = validate_order_capacity(service, conn, {**original, "quantity": str(inv.remaining(original))}, original=original)
    if kind == "early_release":
        order = inspect_open_order(service, terms["order"])
        source_fingerprint = m.digest(order)
        require(order["site"] == inv.config(conn)["site"] and open_lines(order), "The order has no open demand at this site.", "order_closed")
        require(m.digest(order) == record["submission_evidence"].get("order_fingerprint"), "The order changed after this early-release request. Review and resubmit.", "revision")
        before = {control["id"] for control in admin.load(conn)["controls"]}
        result = admin.execute(service, conn, actor, "control", {**terms, "kind": "early_release", "version": admin.load(conn)["version"]})
        record["control_id"] = next(control["id"] for control in admin.load(conn)["controls"] if control["id"] not in before)
    else:
        # Preserve reviewed terms; provenance is stored on the resulting records.
        sale_id = record.get("sales_request_id", "")
        result = inv.execute(service, conn, actor, KINDS[kind], terms)
        allocation = result.get("allocation", result)
        if kind == "transfer":
            sale_id = allocation.get("sales_request_id", "")
        if allocation.get("id") and kind in ("allocation", "transfer", "allocation_change"):
            if kind != "allocation_change":
                allocation["approval_request_id"] = record["id"]
                allocation["sales_request_id"] = sale_id
            allocation["last_approval_request_id"] = record["id"]
            allocation.setdefault("approval_history", []).append(dict(request_id=record["id"], approved_by=actor["id"], at=m.now()))
            allocation["approved_by"] = actor["id"]
            inv.save(conn, m.inventory_allocations, allocation)
            record["allocation_id"] = allocation["id"]
    record["approval_result"] = deepcopy(result)
    record["approval_evidence"] = dict(order_fingerprint=source_fingerprint, inventory_fingerprint=inv.fingerprint(conn), at=m.now())
    return result


def requests(service, conn, actor, action, payload):
    if action == "request-create":
        kind = payload.get("kind")
        terms = normalized_terms(kind, payload)
        sales_id = related_sales(conn, payload.get("sales_request_id"), terms, kind)
        record = {**new_record(actor), "kind": kind, "terms": terms, "sales_request_id": sales_id}
        transition(record, actor, "created", inv.reason(payload), detail={"kind": kind, "terms": terms, "sales_request_id": sales_id})
    else:
        record = inv.get(conn, m.approval_requests, identifier(payload.get("id"), "Approval request").lower())
        revision(record, payload)
        why = inv.reason(payload)
        if action in ("request-approve", "request-reject"):
            human(conn, actor, reviewer=True, admin_only=record["kind"] == "early_release")
            require(record["status"] == "submitted", "Only a submitted request can be reviewed.", "state")
            require(actor["id"] != record["requester"], "The requester cannot review their own request.", "self_approval", 403)
            if action == "request-approve":
                approve(service, conn, actor, record)
                status = "approved"
                record.update(approved_by=actor["id"], approved_at=m.now())
            else:
                status = "rejected"
                record.update(rejected_by=actor["id"], rejected_at=m.now())
            transition(record, actor, status, why, status=status,
                       detail={"submitted_digest": record.get("submitted_digest"), "result": record.get("approval_result")})
        else:
            owner(record, actor)
            require(record["status"] in ("draft", "submitted"), "This request has already been reviewed or cancelled.", "state")
            if action == "request-update":
                kind = payload.get("kind", record["kind"])
                terms = normalized_terms(kind, payload)
                sales_id = related_sales(conn, payload.get("sales_request_id", record.get("sales_request_id")), terms, kind)
                record.update(kind=kind, terms=terms, sales_request_id=sales_id)
                record.pop("submitted_digest", None)
                record.pop("submission_evidence", None)
                transition(record, actor, "revised", why, status="draft", detail={"kind": kind, "terms": terms, "sales_request_id": sales_id})
            elif action == "request-submit":
                require(record["status"] == "draft", "This request was already submitted.", "state")
                related_sales(conn, record.get("sales_request_id"), record["terms"], record["kind"])
                evidence = request_evidence(conn, record["kind"], record["terms"])
                if record["kind"] == "early_release":
                    evidence["order_fingerprint"] = m.digest(inspect_open_order(service, record["terms"]["order"]))
                record.update(submitted_at=m.now(), submitted_digest=m.digest([record["kind"], record["terms"], record["sales_request_id"]]), submission_evidence=evidence)
                transition(record, actor, "submitted", why, status="submitted", detail={"terms": record["terms"], "digest": record["submitted_digest"], "evidence": evidence})
            elif action == "request-cancel":
                transition(record, actor, "cancelled", why, status="cancelled")
            else:
                require(False, "Unknown approval request command.", "not_found", 404)
    return inv.save(conn, m.approval_requests, record)


def execute(service, conn, actor, action, payload):
    actor = human(conn, actor)
    if action.startswith("sales-"):
        return sales(service, conn, actor, action, payload)
    if action.startswith("request-"):
        return requests(service, conn, actor, action, payload)
    require(False, "Unknown workflow command.", "not_found", 404)
