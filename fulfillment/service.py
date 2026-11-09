"""Transactional fulfillment state machine. All changes pass through command()."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from fulfillment import model as m
from fulfillment import admin
from fulfillment import inventory
from fulfillment.domain import (Conflict, address, complete_address, digest, identifier,
                                key, packed_complete, quantity, reconcile, require, trace_key)

TERMINAL = {"released", "cancelled"}


def event(conn, actor, action, detail, work_id=None):
    conn.execute(m.events.insert().values(id=str(uuid4()), work_id=work_id, at=m.now(),
                                         actor=actor, action=action, detail=detail))


def claim(conn, resource, work_id):
    owner = conn.execute(select(m.claims.c.work_id).where(m.claims.c.key == resource)).scalar()
    require(owner in (None, work_id), "That order, cart, operator, serial or label is already assigned.", "reserved")
    if owner is None:
        conn.execute(m.claims.insert().values(key=resource, work_id=work_id))


class Service:
    def __init__(self, database, erp, *, live=False, label_fresh_seconds=300):
        self.db, self.erp, self.live = database, erp, live
        self.erp.database = database
        self.label_fresh_seconds = label_fresh_seconds

    def list_work(self, conn):
        return [row.document for row in conn.execute(select(m.works).order_by(m.works.c.updated_at.desc()))]

    def get(self, conn, work_id):
        row = conn.execute(select(m.works.c.document).where(m.works.c.id == work_id)).first()
        require(row is not None, "Fulfillment not found.", "not_found", 404)
        return deepcopy(row[0])

    def orders(self, conn, context=None):
        context = context or admin.context(conn)
        if inventory.config(conn)["enabled"]:
            from fulfillment.local_planning import orders
            from fulfillment import order_book
            require(not self.live or bool(m.get_setting(conn, "order_book_state")),
                    "Import the complete VISUAL order book before live planning and checkout.", "order_source_unavailable", 503)
            demand = order_book.demand(self, conn, context) if m.get_setting(conn, "order_book_state") else None
            return orders(self, conn, context, demand=demand)
        return self.erp.available(self.list_work(conn), m.get_setting(conn, "reservations", []), context)

    def reconcile_allocations(self, conn, actor):
        from fulfillment import planning
        return planning.run(self, conn, actor)

    def command(self, actor, action, payload, request_id, work_id=None):
        require(isinstance(payload, dict), "A JSON object is required.", "invalid", 400)
        require(bool(request_id) and len(request_id) <= 100, "An Idempotency-Key is required.", "invalid", 400)
        request_key = f"{actor['id']}:{request_id}"
        audit_payload = {**payload, **({"pin": "[redacted]"} if "pin" in payload else {})}
        fingerprint = digest([action, work_id, audit_payload])
        # Failed domain commands also commit their audit and response. The savepoint
        # rolls back every partial write before the rejection is recorded.
        with self.db.transaction() as conn:
            previous = conn.execute(select(m.requests).where(m.requests.c.key == request_key)).first()
            if previous:
                require(previous.digest == fingerprint, "Request ID was reused with different contents.", "idempotency")
                pin_hash = m.get_setting(conn, "request-pin:" + digest(request_key))
                if pin_hash:
                    require(isinstance(payload.get("pin"), str) and check_password_hash(pin_hash, payload["pin"]), "Request ID was reused with different contents.", "idempotency")
                return previous.response, previous.status
            try:
                with conn.begin_nested():
                    if action.startswith("inventory:"):
                        result = inventory.execute(self, conn, actor, action.removeprefix("inventory:"), payload)
                    elif action.startswith("admin:"):
                        result = admin.execute(self, conn, actor, action.removeprefix("admin:"), payload)
                    elif action.startswith("workflow:"):
                        from fulfillment import workflow_requests
                        result = workflow_requests.execute(self, conn, actor, action.removeprefix("workflow:"), payload)
                    elif action.startswith("close:"):
                        from fulfillment import daily_close
                        result = daily_close.execute(self, conn, actor, action.removeprefix("close:"), payload)
                    elif action == "planning:run":
                        from fulfillment import planning
                        require(actor.get("role") in ("supervisor", "admin"), "Supervisor access required.", "role", 403)
                        require(inventory.reason(payload), "A planning reason is required.", "invalid", 400)
                        planning.enqueue(conn, actor["id"], "manual", payload.get("reason"))
                        result = planning.run(self, conn, actor["id"])
                    elif action == "orders:upsert":
                        from fulfillment import order_book
                        require(actor.get("role") == "order_integration", "Order integration access required.", "role", 403)
                        result = order_book.upsert(self, conn, actor, payload)
                    elif action == "checkout":
                        require(actor.get("role") in ("operator", "supervisor", "admin"), "Shipping access required.", "role", 403)
                        result = self.checkout(conn, actor, payload)
                    elif action == "labels":
                        result = self.ingest_label(conn, payload)
                    else:
                        require(actor.get("role") in ("operator", "supervisor", "admin"), "Shipping access required.", "role", 403)
                        work = self.get(conn, work_id)
                        require(payload.get("version") == work["version"], "This work changed. Reload before continuing.", "revision")
                        require(work["state"] not in TERMINAL, "This fulfillment is closed.", "closed")
                        self.apply(conn, actor, action, work, payload)
                        work["version"] += 1
                        work["updated_at"] = m.now()
                        conn.execute(m.works.update().where(m.works.c.id == work_id).values(
                            state=work["state"], version=work["version"], document=work, updated_at=work["updated_at"]))
                        result = work
                    # Inventory resources use their own identities (including
                    # 64-character hashes), not the fulfillment work-ID column.
                    event_work_id = work_id or (result.get("id") if action == "checkout" else None)
                    event_detail = audit_payload
                    if action.startswith("inventory:"):
                        resource_id = result.get("id") or result.get("allocation", {}).get("id")
                        if resource_id:
                            event_detail = {**audit_payload, "inventory_resource_id": resource_id}
                    elif action.startswith(("workflow:", "close:", "orders:", "planning:")):
                        resource_id = result.get("id") or result.get("planning_run", {}).get("id")
                        if resource_id:
                            event_detail = {**audit_payload, "resource_type": action.split(":")[0], "resource_id": resource_id}
                    event(conn, actor["id"], action, event_detail, event_work_id)
                    if action.startswith(("inventory:", "workflow:", "admin:")) or action in ("checkout", "release", "cancel", "putaway", "resolve"):
                        from fulfillment import planning
                        if action not in ("inventory:sync-health", "inventory:sync-ack", "inventory:reconcile", "admin:preview", "admin:account", "admin:reconcile"):
                            planning.enqueue(conn, actor["id"], action, result.get("id") or event_work_id)
                status = 200
                if action == "planning:run" and result.get("planning_run", {}).get("status") == "failed":
                    result["message"] = "Planning failed. Previous commitments are preserved; review the saved run and retry."
                    status = 503
            except Conflict as exc:
                result = {"error": exc.code, "message": exc.message, "details": exc.details}
                status = exc.status
                event(conn, actor["id"], "rejected:" + action, {"request": audit_payload, **result}, work_id)
            if status < 500:
                conn.execute(m.requests.insert().values(key=request_key, digest=fingerprint, response=result, status=status))
                if isinstance(payload.get("pin"), str):
                    m.put_setting(conn, "request-pin:" + digest(request_key), generate_password_hash(payload["pin"]))
            return result, status

    def checkout(self, conn, actor, payload):
        order_id, cart = identifier(payload.get("order"), "Order"), identifier(payload.get("cart"), "Cart")
        available = self.orders(conn)
        order = next((o for o in available["orders"] if o["order"] == order_id), None)
        require(order is not None, "Order is no longer eligible. Refresh the queue.", "ineligible")
        # Persist accumulation reservations at the operational boundary, not on GET.
        m.put_setting(conn, "reservations", available.get("reservations", []))
        require(complete_address(order["destination"]), "Order has an incomplete destination.")
        work_id = str(uuid4())
        for resource in ("order:" + key(order["site"], order_id), "cart:" + key(cart), "picker:" + key(actor["id"])):
            claim(conn, resource, work_id)
        lines = deepcopy(order["lines"])
        require(bool(lines), "No physical lines are available.")
        prescribed = set()
        for index, line in enumerate(lines):
            line.update(id=str(index + 1), picked="0", picked_serials=[])
            require(bool(line["order_line"]) and bool(line["uom"]), "ERP line identity and stocking unit are required.")
            amount = quantity(line["quantity"])
            if line["serialized"]:
                require(amount == len(line["serials"]) and len(set(line["serials"])) == len(line["serials"]),
                        "Each serialized unit needs one prescribed serial.")
                for serial in line["serials"]:
                    trace = trace_key(order["site"], line["part"], serial)
                    require(trace not in prescribed, "A serial cannot be prescribed on two lines.")
                    prescribed.add(trace)
                    claim(conn, trace, work_id)
        stamp = m.now()
        work = {**order, "id": work_id, "cart": cart, "operator": actor["id"], "state": "picking", "version": 1,
                "lines": lines, "packages": [], "verification": None, "packlist": None,
                "exception": None, "created_at": stamp, "updated_at": stamp, "station": None}
        # The mirror is planning evidence. A new credit hold, changed destination
        # or cancelled line must stop checkout before stock custody is committed.
        self.erp.check_order(work)
        from fulfillment import planning
        work["planning_evidence"] = planning.provenance(conn, order)
        administration = admin.load(conn)
        consumed = False
        for control in administration["controls"]:
            if control["kind"] == "early_release" and control["order"] == order_id and admin.active(control):
                control["consumed_by"] = work_id
                consumed = True
        if consumed:
            administration["version"] += 1
            m.put_setting(conn, "administration", administration)
        conn.execute(m.works.insert().values(id=work_id, state=work["state"], version=1, document=work, updated_at=stamp))
        if inventory.config(conn)["enabled"]:
            inventory.checkout(conn, work)
            conn.execute(m.works.update().where(m.works.c.id == work_id).values(document=work))
        return work

    def apply(self, conn, actor, action, work, payload):
        supervisor = actor.get("role") in ("supervisor", "admin")
        # Handoff is the sole operator action permitted to a different signed-in person.
        require(action == "handoff" or work["operator"] == actor["id"] or supervisor,
                "Only the assigned operator or a supervisor can change this work.", "owner", 403)
        if work["exception"] and action not in ("resolve", "cancel", "putaway", "transfer", "substitute"):
            raise Conflict("Resolve the open exception before continuing.", "exception")
        if action == "pick":
            require(work["state"] == "picking", "Picking is already finished.")
            line = self.line(work, payload)
            require(identifier(payload.get("location"), "Location") == line["location"], "Scan the prescribed location.")
            if work.get("inventory_owned"):
                self.erp.check_order(work)
                inventory.check_pick(conn, work, line, payload)
            else:
                self.erp.check_pick(work, line, payload)
            amount, serial = self.scan_amount(line, payload)
            require(quantity(line["picked"], zero=True) + amount <= quantity(line["quantity"]), "Quantity exceeds the prescribed pick.")
            if serial:
                require(serial not in line["picked_serials"], "This serial has already been picked.", "duplicate")
                line["picked_serials"].append(serial)
            line["picked"] = str(quantity(line["picked"], zero=True) + amount)
        elif action == "finish-pick":
            require(work["state"] == "picking", "Work is not being picked.")
            require(all(quantity(l["picked"], zero=True) == quantity(l["quantity"]) for l in work["lines"]), "Pick every prescribed quantity first.")
            self.erp.check_order(work)
            work["state"] = "ready_for_pack"
            conn.execute(m.claims.delete().where(m.claims.c.key == "picker:" + key(work["operator"])))
        elif action == "handoff":
            require(work["state"] == "ready_for_pack", "Cart is not ready for packing.")
            require(identifier(payload.get("cart"), "Cart") == work["cart"], "Scan this order's cart.")
            work.update(operator=actor["id"], station=identifier(payload.get("station"), "Station"), state="packing")
        elif action == "package":
            require(work["state"] == "packing", "Work must be at the packing station.")
            barcode = identifier(payload.get("barcode"), "Package barcode")
            claim(conn, "package:" + key(barcode), work["id"])
            require(not any(p["id"] == barcode for p in work["packages"]), "That package already exists.")
            work["packages"].append({"id": barcode, "contents": [], "tracking": None})
            work["verification"] = None
        elif action == "pack":
            require(work["state"] == "packing", "Work must be at the packing station.")
            line, package = self.line(work, payload), self.package(work, payload)
            amount, serial = self.scan_amount(line, payload)
            total = sum((quantity(i["quantity"]) for p in work["packages"] for i in p["contents"] if i["line_id"] == line["id"]), Decimal(0))
            require(total + amount <= quantity(line["picked"], zero=True), "Cannot pack more than was picked.")
            if serial:
                require(serial in line["picked_serials"], "That serial was not picked.")
                require(not any(serial in i["serials"] and i["line_id"] == line["id"] for p in work["packages"] for i in p["contents"]), "Serial is already packed.", "duplicate")
            package["contents"].append({"line_id": line["id"], "quantity": str(amount), "serials": [serial] if serial else []})
            self.invalidate(work, package)
        elif action == "reset-package":
            require(work["state"] == "packing", "Work must be at the packing station.")
            package = self.package(work, payload)
            require(bool(str(payload.get("reason", "")).strip()), "A correction reason is required.")
            self.invalidate(work, package)
            package["contents"] = []
        elif action == "remove-package":
            require(work["state"] == "packing", "Work must be at the packing station.")
            package = self.package(work, payload)
            require(not package["contents"] and not package["tracking"], "Reset the empty package before removing it.")
            work["packages"].remove(package)
            conn.execute(m.claims.delete().where(m.claims.c.key == "package:" + key(package["id"])))
            work["verification"] = None
        elif action == "verify":
            require(work["state"] == "packing", "Work must be packed first.")
            packed_complete(work)
            packlist = identifier(payload.get("packlist"), "Packlist")
            document = self.erp.packlist(packlist, work)
            result = reconcile(work, document)
            require(result["result"] == "pass", "VISUAL does not match the packed order. " + " ".join(result["differences"]), "packlist_mismatch")
            if work.get("packlist") and work["packlist"] != packlist:
                # Release the old binding only after an explicit ERP void.
                old = self.erp.packlist(work["packlist"], work)
                require(old.get("status") in ("X", "V"), "Void the previous packlist in VISUAL before replacing it.")
                conn.execute(m.claims.delete().where(m.claims.c.key == "packlist:" + key(work["site"], work["packlist"])))
                for p in work["packages"]:
                    p["tracking"] = None
            claim(conn, "packlist:" + key(work["site"], packlist), work["id"])
            work["packlist"] = packlist
            work["verification"] = {**result, "at": m.now(), "actor": actor["id"]}
        elif action == "label":
            require(work["state"] == "packing" and work["verification"], "Verify the saved VISUAL packlist first.")
            package = self.package(work, payload)
            tracking = identifier(payload.get("tracking"), "Tracking number")
            self.check_label(conn, work, package, tracking)
            require(not any(p["id"] != package["id"] and p["tracking"] == tracking for p in work["packages"]), "That label is on another package.")
            claim(conn, "tracking:" + key(tracking), work["id"])
            package["tracking"] = tracking
        elif action == "release":
            require(work["state"] == "packing" and work["verification"], "Verify the saved VISUAL packlist first.")
            packed_complete(work)
            self.erp.check_order(work, final=True)
            document = self.erp.packlist(work["packlist"], work)
            result = reconcile(work, document)
            require(result["result"] == "pass" and result["fingerprint"] == work["verification"]["fingerprint"],
                    "VISUAL changed or no longer matches. Verify it again.", "stale_verification")
            for package in work["packages"]:
                require(bool(package["tracking"]), "Every package needs a scanned label.")
                self.check_label(conn, work, package, package["tracking"])
            if work.get("inventory_owned"):
                inventory.release(conn, actor, work)
            work["state"], work["released_at"] = "released", m.now()
            # Keep trace/order/label history protected. Queue excludes released work
            # until ERP cumulative shipment quantities confirm consumption.
            for resource in ("order:" + key(work["site"], work["order"]), "cart:" + key(work["cart"]), "picker:" + key(work["operator"])):
                conn.execute(m.claims.delete().where(m.claims.c.key == resource).where(m.claims.c.work_id == work["id"]))
        elif action == "exception":
            reason = str(payload.get("reason", "")).strip()
            require(bool(reason), "Describe the problem.")
            work["exception"] = {"reason": reason, "at": m.now(), "actor": actor["id"]}
        elif action == "resolve":
            require(supervisor, "A supervisor must resolve exceptions.", "role", 403)
            require(bool(str(payload.get("reason", "")).strip()), "Record how the problem was resolved.")
            work["exception"] = None
        elif action == "transfer":
            require(supervisor, "A supervisor must authorize reassignment.", "role", 403)
            target = identifier(payload.get("operator"), "Operator")
            require(conn.execute(select(m.users.c.id).where(m.users.c.id == target).where(m.users.c.enabled.is_(True))).first(), "Unknown operator.")
            require(bool(str(payload.get("reason", "")).strip()), "A handoff reason is required.")
            if work["state"] == "picking":
                claim(conn, "picker:" + key(target), work["id"])
                if target != work["operator"]:
                    conn.execute(m.claims.delete().where(m.claims.c.key == "picker:" + key(work["operator"])))
            work["operator"] = target
        elif action == "substitute":
            require(supervisor and work["state"] == "picking", "A supervisor can substitute only during picking.", "role", 403)
            require(not work.get("inventory_owned"), "Return this work to stock and replan to change a locally reserved serial.", "inventory_substitution")
            line = self.line(work, payload)
            old, new = identifier(payload.get("old_serial")), identifier(payload.get("serial"))
            require(bool(str(payload.get("reason", "")).strip()), "A substitution reason is required.")
            require(old in line["serials"] and old not in line["picked_serials"], "Put away picked stock before replacing it.")
            require(not any(new in other["serials"] and other["part"] == line["part"] for other in work["lines"]),
                    "Replacement is already assigned to an order line.")
            self.erp.check_pick(work, line, {**payload, "serial": new})
            protected = m.get_setting(conn, "reservations", [])
            require(not any(r.get("serial_no") == new and r.get("part_id") == line["part"] and r.get("cust_order_id") != work["order"] for r in protected), "Replacement is protected for another order.")
            claim(conn, trace_key(work["site"], line["part"], new), work["id"])
            conn.execute(m.claims.delete().where(m.claims.c.key == trace_key(work["site"], line["part"], old)))
            line["serials"][line["serials"].index(old)] = new
            m.put_setting(conn, "reservations", [r for r in protected if not (r.get("serial_no") == old and r.get("part_id") == line["part"])])
            work["exception"] = None
        elif action == "cancel":
            require(supervisor, "A supervisor must authorize cancellation.", "role", 403)
            require(bool(str(payload.get("reason", "")).strip()), "A cancellation reason is required.")
            if work.get("packlist"):
                require(self.erp.packlist(work["packlist"], work).get("status") in ("X", "V"), "Void the packlist in VISUAL before cancellation.")
            work["state"] = "putaway"
            work["verification"] = None
            work["packages"] = []
            self.finish_putaway(conn, work)
        elif action == "putaway":
            require(work["state"] == "putaway", "This work is not awaiting putaway.")
            line = self.line(work, payload)
            require(identifier(payload.get("location")) == line["location"], "Scan the original bin.")
            amount, serial = self.scan_amount(line, payload)
            require(amount <= quantity(line["picked"], zero=True), "Putaway quantity exceeds remaining picked stock.")
            if serial:
                require(serial in line["picked_serials"], "Serial is not awaiting putaway.")
                line["picked_serials"].remove(serial)
            line["picked"] = str(quantity(line["picked"], zero=True) - amount)
            self.finish_putaway(conn, work)
        else:
            raise Conflict("Unknown action.", "not_found", 404)

    @staticmethod
    def finish_putaway(conn, work):
        if all(quantity(l["picked"], zero=True) == 0 for l in work["lines"]):
            work["state"] = "cancelled"
            if work.get("inventory_owned"):
                inventory.cancel(conn, work)
            conn.execute(m.claims.delete().where(m.claims.c.work_id == work["id"]))
            m.put_setting(conn, "reservations", [r for r in m.get_setting(conn, "reservations", []) if r.get("cust_order_id") != work["order"]])

    @staticmethod
    def invalidate(work, package):
        work["verification"] = None
        package["tracking"] = None

    @staticmethod
    def line(work, payload):
        line = next((l for l in work["lines"] if l["id"] == str(payload.get("line_id"))), None)
        require(line is not None, "Choose the exact order line.", "line", 400)
        return line

    @staticmethod
    def package(work, payload):
        package = next((p for p in work["packages"] if p["id"] == identifier(payload.get("package"), "Package")), None)
        require(package is not None, "Scan a package belonging to this fulfillment.")
        return package

    @staticmethod
    def scan_amount(line, payload):
        if line["serialized"]:
            serial = identifier(payload.get("serial"), "Serial")
            require(serial in line["serials"], "Scan a prescribed serial for this order line.", "wrong_serial")
            return Decimal(1), serial
        require(identifier(payload.get("item"), "Part or UPC") in (line["part"], line.get("upc")), "Scanned item does not match this line.")
        return quantity(payload.get("quantity")), None

    def ingest_label(self, conn, payload):
        tracking = identifier(payload.get("tracking"), "Tracking")
        require(isinstance(payload.get("void"), bool), "void must be true or false.", "invalid", 400)
        for field in ("site", "order", "packlist", "package"):
            payload[field] = identifier(payload.get(field), field)
        require(complete_address(payload.get("destination", {})), "WorldShip must export a complete destination.", "destination", 400)
        try:
            exported = datetime.fromisoformat(payload["exported_at"])
            require(exported.tzinfo is not None and exported <= datetime.now(timezone.utc) + timedelta(minutes=1), "Export timestamp must include timezone and cannot be in the future.", "invalid", 400)
        except (KeyError, ValueError, TypeError):
            raise Conflict("A valid exported_at timestamp is required.", "invalid", 400)
        current = conn.execute(select(m.labels.c.document).where(m.labels.c.tracking == tracking)).scalar()
        if current:
            require(exported >= datetime.fromisoformat(current["exported_at"]), "Older label exports cannot overwrite newer records.")
            require(exported != datetime.fromisoformat(current["exported_at"]) or digest(current) == digest(payload), "Conflicting exports share a timestamp.")
        conn.execute(m.labels.delete().where(m.labels.c.tracking == tracking))
        conn.execute(m.labels.insert().values(tracking=tracking, document=payload, received_at=m.now()))
        return {"tracking": tracking, "accepted": True}

    def check_label(self, conn, work, package, tracking):
        row = conn.execute(select(m.labels).where(m.labels.c.tracking == tracking)).first()
        require(row is not None, "Waiting for the WorldShip export for this label.", "label_pending")
        label = row.document
        require(not label["void"], "This label has been voided.", "void_label")
        require(all(label[f] == v for f, v in (("site", work["site"]), ("order", work["order"]), ("packlist", work["packlist"]), ("package", package["id"]))), "Label belongs to another order, packlist or package.", "wrong_label")
        require(address(label["destination"]) == address(work["destination"]), "Label destination does not match the order.", "destination")
        if self.live:
            require((datetime.now(timezone.utc) - datetime.fromisoformat(row.received_at)).total_seconds() <= self.label_fresh_seconds,
                    "WorldShip label evidence is stale. Refresh the export, including void status.", "label_stale")
            self.erp.check_label(label)
