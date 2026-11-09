"""Read-only local-stock planning with VISUAL order and release-policy checks.

The inventory ledger owns physical quantities and customer/order entitlements.
Virtual part identities give the existing release engine only the quantity each
exact order line may use. It cannot move protected stock between customers or
repeat the same SKU across lines. Checkout re-plans under the database mutex.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fulfillment.domain import complete_address, key, quantity, require
from fulfillment.erp import clean
from fulfillment.model import now
from fulfillment.rules.release_gate import evaluate_release_gate


def _trim(lines, decision, aliases):
    allowed = {(str(a["line_no"]), aliases[a["part_id"]][1]): Decimal(str(a["qty"]))
               for a in decision.get("allocations", [])}
    result = []
    for original in lines:
        identity = (str(original["order_line"]), original["part"])
        remaining = allowed.get(identity, Decimal(0))
        amount = min(quantity(original["quantity"]), remaining)
        if amount <= 0:
            continue
        line = deepcopy(original)
        line["quantity"] = str(amount)
        if line["serialized"]:
            require(amount == int(amount), "A serialized prescription must contain whole units.", "inventory", 503)
            line["serials"] = line["serials"][:int(amount)]
            require(len(line["serials"]) == int(amount), "Local serial prescription is incomplete.", "inventory", 503)
        allowed[identity] -= amount
        result.append(line)
    require(not any(v > Decimal("0.00000001") for v in allowed.values()),
            "Release quantity exceeds the local inventory prescription.", "inventory", 503)
    return result


def orders(service, conn, context, demand=None):
    from fulfillment import inventory

    configuration = inventory.config(conn)
    works = service.list_work(conn)
    demand = demand if demand is not None else service.erp.demand(works, context)
    manifests = {o["order"]: o for o in demand["orders"]}
    for order in manifests.values():
        require(order["site"] == configuration["site"], "Order belongs to a different inventory site.", "inventory_site", 503)
    local = datetime.now(ZoneInfo(context["timezone"]))
    history = list(demand.get("history", []))
    for work in works:
        if work["state"] != "cancelled":
            history.append({"CUSTOMER_ID": work.get("customer_id"), "SHIP_TO_ID": work["ship_to"],
                "LAST_SHIPPED_DATE": datetime.fromisoformat(work["created_at"]).astimezone(local.tzinfo).date()})

    aliases, rows = {}, []
    for original in demand["candidates"]:
        order = manifests.get(original["CUST_ORDER_ID"])
        if order is None:
            continue
        alias = key(order["order"], str(original["LINE_NO"]), original["PART_ID"]).upper()
        aliases[alias] = (order["order"], original["PART_ID"])
        row = {**original, "PART_ID": alias, "AVAILABLE_QTY": 0}
        if order.get("blocks") or not complete_address(order["destination"]):
            row["ORDER_RELEASED"] = 0
        rows.append(row)

    excluded, plans = set(), {}
    owned_allocations = {a["id"]: a for a in inventory.active_allocations(conn)}
    # An ordinary hold must not consume tentative supply. Remove only held
    # orders that actually took tentative stock, then let later demand try it.
    # Exclusions grow monotonically, so the pass count is bounded by demand.
    for _ in range(len(manifests) + 1):
        consumed, plans = {}, {}
        for order in manifests.values():
            if order["order"] in excluded or order.get("blocks") or not complete_address(order["destination"]):
                plans[order["order"]] = []
            else:
                plans[order["order"]] = inventory.plan_lines(conn, order, consumed=consumed)
        for row in rows:
            row["AVAILABLE_QTY"] = sum((quantity(line["quantity"]) for line in plans[row["CUST_ORDER_ID"]]
                if str(line["order_line"]) == str(row["LINE_NO"])), Decimal(0))
        gate_serials, reservations, serial_aliases = [], [], {}
        for order_id, lines in plans.items():
            for line in lines:
                part_alias = key(order_id, str(line["order_line"]), line["part"]).upper()
                allocation = owned_allocations.get(line.get("inventory_allocation_id"), {})
                for serial in line.get("serials", []):
                    serial_alias = key(order_id, line["order_line"], line["inventory_stock_id"], serial).upper()
                    serial_aliases[serial_alias] = serial
                    gate_serials.append(dict(PART_ID=part_alias, SERIAL_NO=serial_alias,
                                             WAREHOUSE_ID=line["warehouse"], LOCATION_ID=line["location"]))
                    if allocation.get("policy_managed"):
                        reservations.append(dict(part_id=part_alias, serial_no=serial_alias, cust_order_id=order_id,
                            line_no=str(line["order_line"]), first_assigned_at=allocation["created_at"],
                            accumulation_started_at=allocation["created_at"]))
        gate = evaluate_release_gate(rows, today=local.date(), mode="enforced", customer_policies=context["policies"],
            due_override_days=context["due_days"], exceptions=context["exceptions"], ship_to_history=history,
            serial_inventory=gate_serials, reservations=reservations, local_now=local,
            min_ship_to_cooldown_days=context["cooldown"], manual_holds=context["holds"])
        held = {d["order_id"] for d in gate["decisions"]
                if d["decision"] in ("BLOCKED", "HOLD") and plans[d["order_id"]]}
        if held - excluded:
            excluded.update(held)
            continue
        break

    released, accumulations = [], []
    for decision in gate["decisions"]:
        order = manifests[decision["order_id"]]
        # The legacy gate rounds presentation quantities to two decimals. It
        # decides eligibility, but a local order-line entitlement is the exact
        # quantity to prescribe (stocking units support eight decimal places).
        for allocation in decision.get("allocations", []):
            allocation["qty"] = str(sum((quantity(line["quantity"]) for line in plans[order["order"]]
                if str(line["order_line"]) == str(allocation["line_no"])
                and line["part"] == aliases[allocation["part_id"]][1]), Decimal(0)))
        prescribed = _trim(plans[order["order"]], decision, aliases) if decision["decision"] in ("RELEASE", "ACCUMULATING") else []
        for allocation in decision.get("allocations", []):
            allocation["part_id"] = aliases[allocation["part_id"]][1]
        for assignment in decision.get("serial_assignments", []):
            assignment["part_id"] = aliases[assignment["part_id"]][1]
            assignment["serial_no"] = serial_aliases[assignment["serial_no"]]
        decision["inventory_source"] = "app"
        decision["lines"] = []
        for wanted in order["lines"]:
            related = [a for a in owned_allocations.values() if a["customer"] == order["customer_id"]
                and a["part"] == wanted["part"] and a["uom"] == wanted["uom"]
                and a.get("order") in (None, "", order["order"])
                and a.get("order_line") in (None, "", wanted["order_line"])]
            available = sum((quantity(line["quantity"]) for line in plans[order["order"]]
                if str(line["order_line"]) == str(wanted["order_line"])), Decimal(0))
            decision["lines"].append(dict(order_line=wanted["order_line"], part=wanted["part"], uom=wanted["uom"],
                requested=wanted["quantity"], available=str(available),
                missing=str(max(Decimal(0), quantity(wanted["quantity"], zero=True) - available)),
                commitments=[dict(id=a["id"], source=a["source"], source_id=a.get("source_id"),
                    remaining=str(inventory.remaining(a)), hold=a["hold"], priority=a.get("priority"),
                    need_by=a.get("need_by"), review_at=a.get("review_at")) for a in related],
                competing_commitments=[dict(id=a["id"], customer=a["customer"], order=a.get("order"),
                    source=a["source"], remaining=str(inventory.remaining(a)), hold=a["hold"])
                    for a in owned_allocations.values() if a not in related and a["part"] == wanted["part"] and a["uom"] == wanted["uom"]]))
        if order.get("blocks"):
            decision.update(decision="BLOCKED", reason_code="hard_block", label="BLOCKED - " + ", ".join(order["blocks"]))
        elif not complete_address(order["destination"]):
            decision.update(decision="BLOCKED", reason_code="hard_block", label="BLOCKED - Incomplete destination")
        if decision["decision"] == "RELEASE":
            require(bool(prescribed), "A released order has no physical inventory.", "inventory", 503)
            result = deepcopy(order)
            result.pop("blocks", None)
            result.update(lines=prescribed, policy=clean(decision), policy_version=context["version"], inventory_source="app")
            released.append(result)
        elif decision["decision"] == "ACCUMULATING" and prescribed:
            accumulations.append({"order": order["order"], "customer_id": order.get("customer_id"),
                                  "lines": prescribed, "policy": clean(decision)})
    return clean({"orders": released, "reservations": [], "decisions": gate["decisions"], "as_of": now(),
                  "inventory_source": "app", "inventory_accumulations": accumulations})
