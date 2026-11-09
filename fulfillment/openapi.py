"""Versioned public contract; command schemas mirror service inputs."""


def specification():
    string = {"type": "string"}
    quantity = {"type": "string", "pattern": r"^[0-9]+(\.[0-9]{1,8})?$", "description": "Decimal stocking-unit quantity; never a JSON float."}
    address = {"type": "object", "required": ["name", "address1", "city", "postal", "country"],
               "properties": {k: string for k in ("name", "address1", "address2", "address3", "city", "state", "postal", "country")}}
    schemas = {"Destination": address, "Error": {"type": "object", "required": ["error", "message"], "properties": {"error": string, "message": string, "details": {}}}}
    schemas["Line"] = {"type": "object", "required": ["id", "order_line", "part", "quantity", "uom", "serials", "warehouse", "location"],
                       "properties": {**{k: string for k in ("id", "order_line", "part", "uom", "warehouse", "location", "upc")},
                                      "quantity": quantity, "picked": quantity, "serialized": {"type": "boolean"},
                                      "serials": {"type": "array", "items": string}, "picked_serials": {"type": "array", "items": string}}}
    schemas["Fulfillment"] = {"type": "object", "required": ["id", "version", "state", "order", "site", "destination", "lines", "packages"],
        "properties": {**{k: string for k in ("id", "state", "order", "site", "operator", "cart")}, "version": {"type": "integer"},
                       "destination": {"$ref": "#/components/schemas/Destination"}, "lines": {"type": "array", "items": {"$ref": "#/components/schemas/Line"}},
                       "packages": {"type": "array", "items": {"type": "object"}}, "verification": {"type": ["object", "null"]}, "packlist": {"type": ["string", "null"]}}}
    schemas["Label"] = {"type": "object", "required": ["tracking", "site", "order", "packlist", "package", "void", "exported_at", "destination"],
        "properties": {**{k: string for k in ("tracking", "site", "order", "packlist", "package")}, "void": {"type": "boolean"},
                       "exported_at": {"type": "string", "format": "date-time"}, "destination": address}}
    schemas["Packlist"] = {"type": "object", "required": ["id", "site", "order", "status", "destination", "lines"],
        "properties": {**{k: string for k in ("id", "site", "order", "status")}, "destination": address,
                       "lines": {"type": "array", "items": {"type": "object", "required": ["packlist_line", "order", "order_line", "part", "uom", "quantity", "serials"],
                           "properties": {**{k: string for k in ("packlist_line", "order", "order_line", "part", "uom")}, "quantity": quantity, "serials": {"type": "array", "items": string}}}}}}
    paths = {}
    errors = {str(s): {"description": description, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
              for s, description in ((400, "Invalid input"), (401, "Authentication required"), (403, "Not authorized"), (404, "Not found"), (409, "State, reservation or revision conflict"), (429, "Sign-in throttled"), (503, "Source unavailable; never a pass"))}

    def endpoint(path, method, summary, *, properties=None, required=(), response=None, security="operator", mutation=False):
        operation = {"summary": summary, "operationId": method + path.replace("/", "_").replace("{", "").replace("}", "").replace("-", "_"),
                     "security": [{"operatorCookie": []}] if security == "operator" else [{"orderIntegrationBearer": []}] if security == "orders" else [{"integrationBearer": []}] if security == "machine" else [],
                     "responses": {"200": {"description": "Success", "content": {"application/json": {"schema": response or {"type": "object"}}}}, **errors}}
        parameters = []
        if "{id}" in path:
            parameters.append({"name": "id", "in": "path", "required": True, "schema": string})
        if mutation:
            parameters.append({"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 100}, "description": "Retry the identical request with the same key after an uncertain response."})
        if method == "post" and security == "operator":
            parameters.append({"name": "X-CSRF-Token", "in": "header", "required": True, "schema": string})
        if parameters:
            operation["parameters"] = parameters
        if properties is not None:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": properties, "required": list(required)}}}}
        paths.setdefault(path, {})[method] = operation

    endpoint("/auth/login", "post", "Individual operator PIN sign-in", properties={"operator": string, "pin": string}, required=("operator", "pin"), security="none")
    endpoint("/auth/me", "get", "Current identity, CSRF token and operating mode")
    endpoint("/auth/logout", "post", "Revoke current session", properties={})
    endpoint("/orders", "get", "Fresh eligible orders under enforced release policies")
    endpoint("/inventory", "get", "App-owned shipping stock, customer commitments, work-order supply and reconciliation evidence")
    endpoint("/inventory/demand", "get", "Stock-independent order/customer/line choices over a 365-day planning horizon; does not change the release policy")
    inventory_fields = {**{n: string for n in ("id", "site", "warehouse", "location", "part", "uom", "customer", "order", "order_line", "source_id", "work_order", "work_order_id", "event_id", "reason")},
                        "quantity": quantity, "version": {"type": "integer", "minimum": 0},
                        "source_revision": {"type": "integer", "minimum": 1},
                        "priority": {"type": "integer", "minimum": 0, "maximum": 100, "default": 50,
                                     "description": "Lower numbers promote future receipts first; then need-by date, creation time and allocation ID."},
                        "request_reference": {"type": ["string", "null"], "maxLength": 200},
                        "need_by": {"type": ["string", "null"], "format": "date"},
                        "source": {"enum": ["manual", "visual", "stock", "work_order"]},
                        "enabled": {"type": "boolean"}, "serialized": {"type": "boolean"}, "hold": {"type": "boolean"},
                        "status": {"enum": ["open", "closed", "cancelled", "released"]},
                        "serials": {"type": "array", "items": string}, "due_date": {"type": "string", "format": "date"},
                        "review_at": {"type": ["string", "null"], "format": "date-time"},
                        "observed_at": {"type": "string", "format": "date-time"},
                        "balances": {"type": "array", "items": {"type": "object", "required": ["part", "uom", "quantity"],
                                     "properties": {"part": string, "uom": string, "quantity": quantity, "serials": {"type": "array", "items": string}}}}}
    inventory_required = {
        "configure": ["version", "site", "warehouse", "location", "enabled", "reason"],
        "receive": ["event_id", "source", "site", "warehouse", "location", "part", "uom", "quantity", "reason"],
        "supply": ["work_order", "part", "uom", "serialized", "quantity", "status", "reason"],
        "allocate": ["source", "part", "uom", "customer", "quantity", "reason"],
        "allocation-update": ["id", "version", "reason"],
        "allocation-transfer": ["id", "version", "quantity", "customer", "reason"],
        "adjust": ["part", "uom", "quantity", "reason"],
        "reconcile": ["event_id", "source", "site", "warehouse", "location", "observed_at", "balances"],
        "sync-ack": ["id", "reason"],
        "link-receipt": ["event_id", "work_order_id", "version", "reason"],
    }
    for action, required in inventory_required.items():
        props = dict(inventory_fields)
        if action == "adjust":
            props["quantity"] = {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]{1,8})?$", "description": "Signed nonzero stocking quantity; cannot consume protected stock."}
        detail = (" Atomically binds, splits or reassigns only unconsumed unlocked quantity. Returns source and successor allocation; no stock enters the public pool."
                  if action == "allocation-transfer" else "")
        endpoint("/inventory/" + action, "post", "Supervisor inventory command: " + action + ". Configure requires administrator; holds never expire automatically." + detail,
                 properties=props, required=required, mutation=True)
    for route, action in (("receipts", "receive"), ("work-orders", "supply"), ("reconcile", "reconcile")):
        required = list(inventory_required[action])
        if route == "work-orders":
            required += ["event_id", "source", "source_revision", "site", "warehouse", "location"]
        endpoint("/integrations/inventory/" + route, "post", "Inbound VISUAL " + route + "; separate inventory bearer, source=visual, canonical source event ID. Reconciliation never overwrites stock.",
                 properties={**inventory_fields, "source": {"const": "visual"}}, required=required, mutation=True, security="machine")
    # The complete connector capture is fenced against both source sequence and local stock changes.
    schemas["InventorySyncIssue"] = {"type": "object", "required": ["id", "message"],
        "properties": {**{name: string for name in ("id", "kind", "event_id", "part", "message")},
                       "quantity": quantity, "at": {"type": "string", "format": "date-time"}}}
    schemas["InventorySyncState"] = {"type": "object",
        "required": ["connected", "sequence", "fresh", "freshness_seconds", "stock_version", "config", "issues"],
        "properties": {"connected": {"type": "boolean"}, "stream_id": string,
            "sequence": {"type": "integer", "minimum": 0}, "stock_version": string,
            "fresh": {"type": "boolean", "description": "False after a source failure or when the last observed capture is older than 300 seconds. Blocks source-owned planning, picking and shipping release."},
            "freshness_seconds": {"type": "integer", "const": 300},
            "status": {"enum": ["matched", "variance", "stale", "source_error", None]},
            **{name: {"type": "string", "format": "date-time"} for name in ("observed_at", "last_success")},
            "config": {"type": "object", "properties": {**{name: string for name in ("site", "warehouse", "location")}, "enabled": {"type": "boolean"}, "version": {"type": "integer"}}},
            "source_error": {"type": ["object", "null"], "properties": {"code": {"enum": ["capture_failed", "delivery_rejected", "delivery_pending", "delivery_confirmed"]}, "at": {"type": "string", "format": "date-time"}, "message": string}},
            "issues": {"type": "array", "items": {"$ref": "#/components/schemas/InventorySyncIssue"}}}}
    paths["/inventory"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "type": "object", "properties": {"sync": {"$ref": "#/components/schemas/InventorySyncState"}}}
    endpoint("/integrations/inventory/state", "get", "Current connector sequence, source health and local stock fence. Requires the separate inventory integration bearer.",
             security="machine", response={"$ref": "#/components/schemas/InventorySyncState"})
    receipt = {"type": "object", "required": ["event_id", "part", "uom", "quantity", "reason"],
        "properties": {**{name: string for name in ("event_id", "part", "uom", "work_order", "reason")},
                       "quantity": quantity, "serials": {"type": "array", "items": string, "maxItems": 5000}}}
    source_supply = {"type": "object", "required": ["work_order", "part", "uom", "remaining", "serialized", "status"],
        "properties": {**{name: string for name in ("work_order", "part", "uom")}, "remaining": quantity,
                       "serialized": {"type": "boolean"}, "status": {"enum": ["open", "closed", "cancelled"]},
                       "due_date": {"anyOf": [{"type": "string", "format": "date"}, {"const": ""}]}}}
    schemas["SourceMovement"] = {"type": "object", "required": ["id", "part", "uom", "quantity", "kind", "serials"],
        "properties": {**{name: string for name in ("id", "part", "uom", "work_order", "direction", "category", "effective_date", "source_created_local",
            "customer_order", "customer_order_line", "document", "document_line", "purchase_order", "purchase_order_line", "transfer_id")},
            "quantity": {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]{1,8})?$", "description": "Signed stocking quantity; movement identity and direction are retained even when net stock change is zero."},
            "kind": {"enum": ["receipt", "shipment", "other"]}, "transfer_peer": {"type": "object"},
            "serials": {"type": "array", "items": {"type": "object", "required": ["serial", "quantity"],
                "properties": {"serial": string, "quantity": {"enum": ["1", "-1"]}}}}}}
    schemas["MovementEvidence"] = {"type": "object", "required": ["version", "complete", "base_sequence", "pending_before_capture", "count", "manifest"],
        "properties": {"version": {"const": 2}, "complete": {"type": "boolean"}, "base_sequence": {"type": "integer", "minimum": 0},
            "pending_before_capture": {"const": False}, "count": {"type": "integer", "minimum": 0}, "manifest": string},
        "description": "Full retained movement identity manifest, including revisions/deletions. Version 1 or missing evidence cannot support a complete daily close."}
    schemas["MovementChange"] = {"type": "object", "required": ["id", "deleted"], "properties": {
        "id": string, "deleted": {"type": "boolean"}, "prior_hash": string, "movement": {"anyOf": [{"$ref": "#/components/schemas/SourceMovement"}, {"type": "null"}]}},
        "allOf": [{"if": {"properties": {"deleted": {"const": False}}}, "then": {"required": ["movement"], "properties": {"movement": {"$ref": "#/components/schemas/SourceMovement"}}}}]}
    batch_fields = {**{name: string for name in ("event_id", "site", "warehouse", "location", "stream_id", "expected_stock_version", "source_manifest")},
        "source": {"const": "visual"}, "kind": {"enum": ["opening", "delta"]}, "sequence": {"type": "integer", "minimum": 1},
        "observed_at": {"type": "string", "format": "date-time", "description": "Must advance beyond the previous capture and be no more than 120 seconds old or 60 seconds in the future."},
        "work_orders": {"type": "array", "items": source_supply, "maxItems": 5000},
        "receipts": {"type": "array", "items": receipt, "maxItems": 5000},
        "balances": {**inventory_fields["balances"], "maxItems": 5000},
        "issues": {"type": "array", "items": {"$ref": "#/components/schemas/InventorySyncIssue"}, "maxItems": 5000},
        "movement_evidence": {"$ref": "#/components/schemas/MovementEvidence"},
        "movement_changes": {"type": "array", "maxItems": 100000, "items": {"$ref": "#/components/schemas/MovementChange"}}}
    endpoint("/integrations/inventory/batches", "post", "Atomically ingest a VISUAL capture using the inventory bearer. Opening requires sequence 1, inactive inventory, an empty stock ledger and no active work or legacy reservations; opening receipts must be empty. Delta requires the current stream and next sequence. All linked receipts require current work-order evidence in the batch. A complete balance snapshot records variances without overwriting local stock. A fresh valid batch clears source errors; negative source variance quarantines affected stock.",
             security="machine", mutation=True, properties=batch_fields,
             required=["event_id", "source", "site", "warehouse", "location", "stream_id", "sequence", "kind", "observed_at", "expected_stock_version", "balances"])
    endpoint("/integrations/inventory/health", "post", "Report capture/delivery health using the inventory bearer. Stream and sequence must match the accepted batch. capture_failed, delivery_rejected or delivery_pending block stock use. delivery_confirmed acknowledges the durable connector checkpoint and clears only delivery_pending; a fresh valid capture is required to clear other source failures.",
             security="machine", mutation=True,
             properties={"event_id": string, "source": {"const": "visual"}, "stream_id": string, "sequence": {"type": "integer", "minimum": 1}, "code": {"enum": ["capture_failed", "delivery_rejected", "delivery_pending", "delivery_confirmed"]}},
             required=["event_id", "source", "stream_id", "sequence", "code"])
    paths["/inventory/sync-ack"]["post"]["summary"] = "Supervisor acknowledges a sync issue with an audited reason. Does not clear a stock quarantine or restore source freshness."
    paths["/inventory/link-receipt"]["post"]["summary"] = "Supervisor links existing free receipt stock to a work order using its current supply version. Promotes eligible promises without adding on-hand inventory; supports completed work-order lineage."

    for action, fields in {"sync-ack": ["id", "reason"], "link-receipt": ["event_id", "work_order_id", "version", "reason"]}.items():
        paths["/inventory/" + action]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"] = {field: inventory_fields[field] for field in fields}
    paths["/integrations/inventory/batches"]["post"]["requestBody"]["content"]["application/json"]["schema"]["allOf"] = [
        {"if": {"properties": {"kind": {"const": "opening"}}}, "then": {"properties": {"receipts": {"maxItems": 0}}}},
        {"if": {"required": ["movement_evidence"]}, "then": {"required": ["movement_changes"]}}]
    paths["/integrations/inventory/batches"]["post"]["x-max-request-bytes"] = 16 * 1024 * 1024
    paths["/integrations/inventory/batches"]["post"]["description"] = "Maximum JSON body 16 MiB; ordinary commands are limited to 2 MiB. Modern connectors send movement_evidence version 2 plus complete movement_changes. Legacy captures may compare balances but cannot produce a complete signed daily close."
    schemas["InventorySyncState"]["properties"].update(movement_evidence={"type": "object"}, delivery_confirmed_sequence={"type": "integer", "minimum": 1}, delivery_confirmed_at={"type": "string", "format": "date-time"})

    # Operating order-to-close contract. A draft is never an ERP sales order.
    revision = {"type": "integer", "minimum": 1}
    stamp = {"type": "string", "format": "date-time"}
    reason = {"type": "string", "minLength": 1, "maxLength": 1000}
    signed_quantity = {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]{1,8})?$"}
    identity_array = {"type": "array", "items": string, "uniqueItems": True}
    schemas["OrderLine"] = {"type": "object", "required": ["order_line", "part", "uom", "quantity", "serialized"],
        "properties": {**{name: string for name in ("order_line", "part", "uom", "status")},
            **{name: quantity for name in ("quantity", "ordered", "shipped")}, "serialized": {"type": "boolean"},
            **{name: {"type": ["string", "null"], "format": "date"} for name in ("promise_ship_date", "promise_del_date")}}}
    schemas["SourceOrder"] = {"type": "object", "required": ["site", "order", "customer_id", "destination", "lines"],
        "properties": {**{name: string for name in ("site", "order", "customer_id", "customer", "ship_to", "status", "customer_reference", "document_fingerprint")},
            "destination": address, "lines": {"type": "array", "maxItems": 5000, "items": {"$ref": "#/components/schemas/OrderLine"}},
            "credit_approved": {"type": "boolean", "default": False}, "blocks": {"type": "array", "items": string},
            "baseline": {"type": "object", "additionalProperties": {"type": "object", "properties": {
                "part": string, "uom": string, "ordered": quantity, "shipped": quantity}}},
            **{name: {"type": ["string", "null"], "format": "date"} for name in ("order_date", "desired_ship_date")}}}
    schemas["OrderMirror"] = {"type": "object", "required": ["id", "order", "source_revision", "observed_at", "version", "conflicts"],
        "properties": {"id": string, "order": {"$ref": "#/components/schemas/SourceOrder"}, "source_revision": {"type": ["string", "integer"]},
            "observed_at": stamp, "version": revision, "facts_digest": string, "conflicts": {"type": "array", "items": {"type": "object"}},
            "ignored_older_revision": {"type": "boolean"}}}
    endpoint("/integrations/orders", "post", "Record an existing VISUAL order/revision in Dispatch using the separate ORDER_TOKEN; never creates an ERP order. Repeated source events replay safely; older revisions/observations cannot overwrite newer facts. One event does not certify complete order-book freshness; periodic full capture repairs missed events.",
        security="orders", mutation=True, properties={"event_id": string, "source_revision": {"type": "integer", "minimum": 0},
            "observed_at": stamp, "order": {"$ref": "#/components/schemas/SourceOrder"}},
        required=["event_id", "source_revision", "observed_at", "order"], response={"$ref": "#/components/schemas/OrderMirror"})
    schemas["PlanningRunSummary"] = {"type": "object", "required": ["id", "status", "started_at"],
        "properties": {"id": string, "status": {"enum": ["running", "succeeded", "failed", "interrupted"]},
            "actor": string, "started_at": stamp, "finished_at": {"type": ["string", "null"], "format": "date-time"},
            "error": {"type": ["object", "null"]}, "triggers": {"type": "array", "items": {"type": "object"}}}}
    schemas["PlanningRun"] = {"allOf": [{"$ref": "#/components/schemas/PlanningRunSummary"}, {"type": "object", "properties": {
        "inputs": {"type": "object", "description": "Retained order facts, published policy, local stock/locks, source evidence, work and pre-run commitments."},
        "decisions": {"type": "array", "items": {"type": "object"}}, "allocation_changes": {"type": "array", "items": {"type": "object"}},
        "board": {"type": "array", "items": {"type": "object"}}, "result": {"type": "object"}}}]}
    schemas["PlanningResult"] = {"type": "object", "required": ["planning_run", "orders", "decisions"],
        "properties": {"planning_run": {"$ref": "#/components/schemas/PlanningRunSummary"}, "orders": {"type": "array", "items": {"type": "object"}},
            "decisions": {"type": "array", "items": {"type": "object"}}, "message": string}}
    schemas["OrderBoard"] = {"type": "object", "required": ["orders", "source", "pending_triggers", "needs_replanning", "as_of"],
        "properties": {"orders": {"type": "array", "items": {"type": "object", "required": ["order", "status", "label", "owner", "next_action"],
            "properties": {**{name: string for name in ("id", "order", "customer", "customer_id", "site", "status", "label", "owner", "next_action")},
                "decision": {"type": ["object", "null"]}, "commitments": {"type": "array", "items": {"type": "object"}},
                "observed_at": stamp, "decision_at": {"type": ["string", "null"]}, "planning_run_id": {"type": ["string", "null"]}}}},
            "source": {"type": "object"}, "pending_triggers": {"type": "array", "items": {"type": "object"}},
            "needs_replanning": {"type": "boolean"}, "latest_run": {"type": ["object", "null"]}, "as_of": stamp}}
    endpoint("/planning/board", "get", "Shipping and Sales: all mirrored orders with decision evidence, source age, blockers, responsible owner and next action. Ready suggestions are tentative until checkout; GET never changes commitments.", response={"$ref": "#/components/schemas/OrderBoard"})
    endpoint("/planning/runs", "get", "Last 200 planning attempts and pending trigger references; failures and interruptions retain previous commitments.", response={"type": "object", "required": ["runs", "total", "pending_triggers"], "properties": {
        "runs": {"type": "array", "items": {"$ref": "#/components/schemas/PlanningRunSummary"}}, "total": {"type": "integer"}, "pending_triggers": {"type": "array", "items": {"type": "object"}}}})
    endpoint("/planning/runs/{id}", "get", "Complete retained planning inputs, per-order decisions, trigger references and allocation before/after evidence.", response={"$ref": "#/components/schemas/PlanningRun"})
    endpoint("/planning/run", "post", "Supervisor/admin manual planning attempt. Refreshes the complete order mirror, preserves existing promises and records every decision. Shadow mode is read-only; failed attempts remain visible and return 503 after business changes roll back.",
        properties={"reason": reason}, required=["reason"], mutation=True, response={"$ref": "#/components/schemas/PlanningResult"})
    paths["/planning/run"]["post"]["responses"]["503"] = {"description": "Failed attempt retained, or source/cutover unavailable. Commitments remain protected; an unsuccessful attempt may be retried.", "content": {"application/json": {"schema": {"anyOf": [
        {"$ref": "#/components/schemas/PlanningResult"}, {"$ref": "#/components/schemas/Error"}]}}}}

    request_base = {"id": string, "version": revision, "reason": reason}
    sale_fields = {"customer": string, "site": string, "customer_reference": {"type": "string", "minLength": 1, "maxLength": 200},
        "destination": address, "need_by": {"type": ["string", "null"], "format": "date"}, "reason": reason,
        "lines": {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "object", "required": ["part", "uom", "quantity"],
            "properties": {"id": string, "part": string, "uom": string, "quantity": quantity}}}}
    schemas["SalesRequest"] = {"type": "object", "required": ["id", "version", "status", "requester", "customer", "site", "lines", "history"],
        "properties": {**sale_fields, "id": string, "version": revision, "requester": string,
            "status": {"enum": ["draft", "awaiting_visual_order", "bound", "cancelled"]}, "order": string,
            "history": {"type": "array", "items": {"type": "object"}}, "line_bindings": {"type": "array", "items": {"type": "object"}}}}
    term_fields = {
        "allocation": ["source", "source_id", "part", "uom", "customer", "order", "order_line", "quantity", "hold", "review_at", "priority", "need_by", "request_reference", "reason"],
        "transfer": ["id", "version", "quantity", "customer", "order", "order_line", "hold", "review_at", "priority", "need_by", "request_reference", "reason"],
        "allocation_change": ["id", "version", "hold", "status", "review_at", "priority", "need_by", "request_reference", "reason"],
        "adjustment": ["part", "uom", "quantity", "serials", "reason"],
        "early_release": ["order", "expires_at", "reason"],
    }
    term_required = {"allocation": ["source", "part", "uom", "customer", "quantity", "reason"],
        "transfer": ["id", "version", "quantity", "customer", "reason"], "allocation_change": ["id", "version", "reason"],
        "adjustment": ["part", "uom", "quantity", "reason"], "early_release": ["order", "expires_at", "reason"]}
    for kind, names in term_fields.items():
        props = {name: inventory_fields.get(name, string) for name in names}
        props.update(reason=reason)
        if "version" in props:
            props["version"] = revision
        if "hold" in props:
            props["hold"] = {"type": "boolean", "description": "Held inventory stays reserved after review_at. Expiry creates a review obligation; only an explicit authorized change removes the hold."}
        if kind == "allocation":
            props["source"] = {"enum": ["stock", "work_order"]}
        if kind == "allocation_change":
            props["status"] = {"const": "released"}
        if kind == "adjustment":
            props["quantity"] = signed_quantity
        if kind == "early_release":
            props["expires_at"] = stamp
        schemas["ApprovalTerms_" + kind] = {"type": "object", "additionalProperties": False, "properties": props, "required": term_required[kind]}
    schemas["ApprovalTerms_allocation"]["allOf"] = [{"if": {"properties": {"source": {"const": "work_order"}}}, "then": {"required": ["source_id"]}}]
    schemas["ApprovalTerms_allocation_change"]["anyOf"] = [{"required": [name]} for name in ("hold", "status", "review_at", "priority", "need_by", "request_reference")]
    terms = {"oneOf": [{"$ref": "#/components/schemas/ApprovalTerms_" + kind} for kind in term_fields]}
    approval_fields = {"kind": {"enum": list(term_fields)}, "terms": terms, "sales_request_id": string, "reason": reason}
    schemas["ApprovalRequest"] = {"type": "object", "required": ["id", "version", "status", "requester", "kind", "terms", "history"],
        "properties": {**approval_fields, "id": string, "version": revision, "requester": string,
            "status": {"enum": ["draft", "submitted", "approved", "rejected", "cancelled"]}, "allocation_id": string,
            "history": {"type": "array", "items": {"type": "object"}}, "approval_result": {"type": "object"}, "progress": {"type": "object"}}}
    endpoint("/workflow", "get", "Shipping and Sales request inbox, revision history and approved commitment progress.", response={"type": "object", "properties": {
        "sales_requests": {"type": "array", "items": {"$ref": "#/components/schemas/SalesRequest"}}, "approval_requests": {"type": "array", "items": {"$ref": "#/components/schemas/ApprovalRequest"}}, "kinds": {"type": "array", "items": string}, "as_of": stamp}})
    for action in ("create", "update", "submit", "cancel", "bind"):
        props = {**request_base, **sale_fields} if action in ("create", "update") else dict(request_base)
        required = ["customer", "site", "customer_reference", "destination", "lines", "reason"] if action in ("create", "update") else ["id", "version", "reason"]
        if action == "update":
            required += ["id", "version"]
        if action == "bind":
            props.update(order=string, duplicate_reference_review=reason, line_bindings={"type": "array", "minItems": 1, "maxItems": 200,
                "items": {"type": "object", "required": ["request_line", "order_line"], "properties": {"request_line": string, "order_line": string}}})
            required += ["order", "line_bindings"]
        endpoint("/workflow/sales-" + action, "post", "Sales intake: " + action + ". Drafts cannot ship or create a VISUAL order. Binding requires supervisor/admin and exact live customer/site/line/unit/quantity/destination evidence; duplicate customer references require independent review.",
            properties=props, required=required, mutation=True, response={"$ref": "#/components/schemas/SalesRequest"})
    for action in ("create", "update", "submit", "cancel", "approve", "reject"):
        props = {**request_base, **approval_fields} if action in ("create", "update") else dict(request_base)
        required = ["kind", "terms", "reason"] if action == "create" else ["id", "version", "terms", "reason"] if action == "update" else ["id", "version", "reason"]
        endpoint("/workflow/request-" + action, "post", "Operational request: " + action + ". Submission never reserves stock. Approval/rejection requires another supervisor/admin (early release requires admin), exact current revision and revalidated capacity. Approval and resulting commitment are atomic; shipment hard gates remain in force.",
            properties=props, required=required, mutation=True, response={"$ref": "#/components/schemas/ApprovalRequest"})
        if action in ("create", "update"):
            paths["/workflow/request-" + action]["post"]["requestBody"]["content"]["application/json"]["schema"]["allOf"] = [
                {"if": {"required": ["kind"], "properties": {"kind": {"const": kind}}}, "then": {"properties": {"terms": {"$ref": "#/components/schemas/ApprovalTerms_" + kind}}}}
                for kind in term_fields]

    schemas["DailyClose"] = {"type": "object", "required": ["id", "version", "business_date", "timezone", "cutoff", "evidence_status", "approval_status", "packet", "exceptions", "digest"],
        "properties": {"id": string, "version": revision, "business_date": {"type": "string", "format": "date"}, "timezone": string,
            "cutoff": stamp, "prepared_at": stamp, "prepared_by": string, "signed_at": stamp, "signed_by": string,
            "evidence_status": {"enum": ["incomplete", "exceptions", "balanced"]}, "approval_status": {"enum": ["draft", "signed", "superseded"]},
            "packet": {"type": "object", "description": "Frozen opening/closing stock, exact ledger equation, complete source versions, two-way movement links, shipment documents, carry-forward work and integration checkpoint."},
            "exceptions": {"type": "array", "items": {"type": "object"}}, "digest": string, "review_history": {"type": "array", "items": {"type": "object"}}}}
    schemas["MovementCorrespondence"] = {"type": "object", "required": ["id", "source_ids", "ledger_ids", "status", "reference", "reason", "created_by", "created_at"],
        "properties": {"id": string, "source_ids": identity_array, "ledger_ids": identity_array, "status": {"enum": ["proposed", "cancelled"]},
            "reference": string, "reason": reason, "metadata_only": {"type": "boolean"}, "created_by": string, "created_at": stamp,
            "source_current": {"type": "object"}, "source_basis": {"type": "object"}, "ledger_index": {"type": "object"},
            "quantity_evidence": {"type": "array", "items": {"type": "object"}}, "cancelled_by": string, "cancelled_at": stamp, "cancellation_reason": reason}}
    endpoint("/daily-close", "get", "Retained close summaries, review status and movement correspondences; signed with exceptions is never balanced.")
    endpoint("/daily-close/{id}/packet", "get", "Download the reproducible frozen close packet and digest as application/json attachment.", response={"$ref": "#/components/schemas/DailyClose"})
    close_fields = {**request_base, "pause_confirmed": {"const": True}, "business_date": {"type": "string", "format": "date"},
        "allow_exceptions": {"type": "boolean"}, "exception_id": string, "owner": string, "due_at": stamp,
        "disposition": {"enum": ["open", "accepted_exception"]}, "resolution_reference": {"type": "string", "maxLength": 1000}}
    close_commands = {"prepare": ["pause_confirmed", "reason"], "supersede": ["id", "version", "pause_confirmed", "reason"],
        "exception": ["id", "version", "exception_id", "owner", "due_at", "reason"], "sign": ["id", "version", "reason"]}
    for action, required in close_commands.items():
        endpoint("/daily-close/" + action, "post", "Supervisor/admin close command: " + action + ". Coordinate a warehouse movement pause. Signer must differ from preparer and correspondence authors; missing evidence prevents sign-off. Material changes require re-preparation; acknowledged differences remain explicit exceptions with owners, due dates and evidence references.",
            properties=close_fields, required=required, mutation=True, response={"$ref": "#/components/schemas/DailyClose"})
    endpoint("/daily-close/correspondence", "post", "Supervisor/admin proposes exact source-to-authorized-adjustment correspondence. Direction, units and serials must agree. Metadata-only review cannot net physical inbound/outbound movements to zero. Shipment gaps use verified fulfillment evidence, never balancing stock adjustments; another supervisor must sign the resulting close.",
        properties={"source_ids": {**identity_array, "minItems": 1, "maxItems": 100}, "ledger_ids": {**identity_array, "maxItems": 100},
            "reference": {"type": "string", "minLength": 1, "maxLength": 1000}, "reason": reason, "metadata_only": {"type": "boolean", "default": False}},
        required=["source_ids", "reference", "reason"], mutation=True, response={"$ref": "#/components/schemas/MovementCorrespondence"})
    endpoint("/daily-close/correspondence-cancel", "post", "Supervisor/admin cancels an unsigned correspondence with a reason. Evidence used by a signed or superseded close remains immutable.",
        properties={"id": string, "reason": reason}, required=["id", "reason"], mutation=True, response={"$ref": "#/components/schemas/MovementCorrespondence"})

    rules = {"type": "object", "additionalProperties": False,
             "required": ["policies", "due_days", "cooldown", "lookahead_days", "timezone", "document_checks_required", "component_codes", "excluded_customers"],
             "properties": {"policies": {"type": "object", "additionalProperties": {"type": "object"}},
                            **{n: {"type": "integer", "minimum": 0, "maximum": 30 if n == "cooldown" else 365} for n in ("due_days", "cooldown", "lookahead_days")},
                            "timezone": string, "document_checks_required": {"type": "boolean"},
                            **{n: {"type": "array", "items": string} for n in ("component_codes", "excluded_customers")}}}
    schemas["AdminRules"] = rules
    endpoint("/admin/state", "get", "Administrator only: saved rules, controls, reservations, accounts and recent audit. PIN hashes are never returned.")
    endpoint("/admin/orders", "get", "Administrator only: current release decisions and eligible prescriptions")
    endpoint("/admin/order/{id}", "get", "Administrator only: inspect an order and its document-review fingerprint")
    admin_fields = {"version": {"type": "integer", "minimum": 1}, "reason": string, "rules": rules, "preview": string,
                    "order": string, "kind": {"enum": ["hold", "early_release", "document_review"]},
                    "expires_at": {"type": ["string", "null"], "format": "date-time"}, "fingerprint": string,
                    "decision": {"enum": ["pass", "blocked"]}, "reference": string, "id": string,
                    "operator": string, "name": string, "role": {"enum": ["sales", "operator", "supervisor", "admin"]},
                    "enabled": {"type": "boolean"}, "pin": {"type": "string", "writeOnly": True, "pattern": "^[0-9]{6,32}$"}}
    for action, required in {
        "preview": ["rules"], "rules": ["rules", "preview", "reason"], "control": ["order", "kind", "reason"],
        "revoke": ["id", "reason"], "unassign": ["order", "reason"], "reconcile": ["reason"],
        "account": ["operator", "name", "role", "enabled", "reason"],
    }.items():
        endpoint("/admin/" + action, "post", "Administrator only: " + action + ". Exact administration revision and idempotency key required. Early release and document reviews require expiration; reviews also require fingerprint, reference and decision.",
                 properties=admin_fields, required=["version", *required], mutation=True)
    endpoint("/fulfillments", "get", "Fulfillment queue and history")
    for path in ("/fulfillments/{id}", "/fulfillments/{id}/manifest"):
        endpoint(path, "get", "Exact line, serial, location and package manifest", response={"$ref": "#/components/schemas/Fulfillment"})
    endpoint("/checkouts", "post", "Atomically claim an order and cart", properties={"order": string, "cart": string}, required=("order", "cart"), mutation=True, response={"$ref": "#/components/schemas/Fulfillment"})
    fields = {"pick": ("line_id", "location", "serial", "item", "quantity"), "pack": ("line_id", "package", "serial", "item", "quantity"),
              "putaway": ("line_id", "location", "serial", "item", "quantity"), "finish-pick": (), "handoff": ("cart", "station"),
              "package": ("barcode",), "reset-package": ("package", "reason"), "remove-package": ("package",),
              "verify": ("packlist",), "label": ("package", "tracking"), "release": (), "exception": ("reason",),
              "resolve": ("reason",), "transfer": ("operator", "reason"), "substitute": ("line_id", "old_serial", "serial", "reason"), "cancel": ("reason",)}
    for action, names in fields.items():
        props = {n: quantity if n == "quantity" else string for n in names}
        props["version"] = {"type": "integer", "minimum": 1}
        required = ["version"] + [n for n in names if n not in ("serial", "item", "quantity") or action == "substitute"]
        endpoint("/fulfillments/{id}/" + action, "post", action.replace("-", " ").capitalize() + ". Serialized scans require serial; quantity scans require item and quantity.",
                 properties=props, required=required, mutation=True, response={"$ref": "#/components/schemas/Fulfillment"})
    endpoint("/integrations/visual/fulfillments/{id}/manifest", "get", "Manifest for a scoped VISUAL integration credential", security="machine")
    endpoint("/integrations/visual/validate", "post", "Advisory candidate comparison or saved packlist read-back; never changes workflow state", security="machine",
             properties={"fulfillment_id": string, "version": {"type": "integer"}, "packlist": string, "candidate": {"$ref": "#/components/schemas/Packlist"}}, required=("fulfillment_id", "version"))
    endpoint("/integrations/worldship/labels", "post", "Ingest authoritative WorldShip package evidence using the separate label credential", security="machine",
             properties=schemas["Label"]["properties"], required=schemas["Label"]["required"], mutation=True)
    # The cloned inventory-audit API retains its established /api/audit paths.
    audit_routes = {
        "/audit/dashboard": ("get", "Locations, due cadence, active and recent inventory audits"),
        "/audit/sessions": ("post", "Start an inventory snapshot; target_kind: location, warehouse, tied or all"),
        "/audit/session/{id}": ("get", "Snapshot items, counts, unexpected scans and resolutions"),
        "/audit/session/{id}/state": ("get", "Current audit status and counts"),
        "/audit/analytics": ("get", "Accuracy, cadence, exceptions and current staging dwell"),
        **{f"/audit/session/{{id}}/{name}": ("post", description) for name, description in {
            "scan": "Classify a serial and location scan; identity comes from login",
            "resolve": "Append a manual resolution (serial, resolution, optional note)",
            "recheck": "Read current ERP evidence for unexpected scans; never writes ERP",
            "complete": "Atomically mark remaining in-scope serials missing and finalize counts",
        }.items()},
    }
    for path, (method, description) in audit_routes.items():
        endpoint(path, method, description, mutation=method == "post",
                 properties={"target_kind": string, "warehouse": string, "location": string,
                             "serial": string, "resolution": string, "note": string} if method == "post" else None)
        paths[path][method]["servers"] = [{"url": "/api"}]
    return {"openapi": "3.1.0", "info": {"title": "Dispatch fulfillment API", "version": "1.4.0"}, "servers": [{"url": "/api/v1"}],
            "paths": paths, "components": {"schemas": schemas, "securitySchemes": {"operatorCookie": {"type": "apiKey", "in": "cookie", "name": "fulfillment_session"}, "integrationBearer": {"type": "http", "scheme": "bearer", "description": "Use the endpoint-specific inventory, label or validation bearer; credentials are not interchangeable."}, "orderIntegrationBearer": {"type": "http", "scheme": "bearer", "description": "Separate FULFILLMENT_ORDER_TOKEN, at least 32 characters; authenticates only existing VISUAL order intake."}}}}
