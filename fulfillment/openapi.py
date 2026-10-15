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
                     "security": [{"operatorCookie": []}] if security == "operator" else [{"integrationBearer": []}] if security == "machine" else [],
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
    return {"openapi": "3.1.0", "info": {"title": "Dispatch fulfillment API", "version": "1.0.0"}, "servers": [{"url": "/api/v1"}],
            "paths": paths, "components": {"schemas": schemas, "securitySchemes": {"operatorCookie": {"type": "apiKey", "in": "cookie", "name": "fulfillment_session"}, "integrationBearer": {"type": "http", "scheme": "bearer"}}}}
