"""Exact line-level reconciliation and shared validation, independent of Flask."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re

from fulfillment.model import digest


class Conflict(Exception):
    def __init__(self, message, code="conflict", status=409, details=None):
        self.message, self.code, self.status, self.details = message, code, status, details
        super().__init__(message)


def require(condition, message, code="conflict", status=409):
    if not condition:
        raise Conflict(message, code, status)


def quantity(value, *, zero=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (result == 0 and not zero) or result.as_tuple().exponent < -8:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise Conflict("Enter a positive quantity with at most eight decimal places.", "quantity", 400)


def identifier(value, name="Identifier"):
    result = str(value or "").strip().upper()
    require(bool(result) and len(result) <= 80 and not any(ord(c) < 32 for c in result),
            f"{name} is required (maximum 80 characters).", "invalid", 400)
    return result


def key(*parts):
    # Length-delimited JSON avoids collisions when identifiers contain punctuation.
    return digest(list(parts))


def trace_key(site, part, serial):
    return "trace:" + key(site, part, serial)


ADDRESS_FIELDS = ("name", "address1", "address2", "address3", "city", "state", "postal", "country")


def address(value):
    return {field: re.sub(r"\s+", " ", str(value.get(field) or "").strip().upper()) for field in ADDRESS_FIELDS}


def complete_address(value):
    return all(address(value)[field] for field in ("name", "address1", "city", "postal", "country"))


def line_manifest(work):
    return [{k: line[k] for k in ("id", "order_line", "part", "quantity", "uom", "serials", "warehouse", "location")}
            for line in work["lines"]]


def contents(work):
    """Compare serialized sets AND quantities by ERP order line, never just SKU."""
    result = {}
    for line in work["lines"]:
        k = (str(line["order_line"]), line["part"], line["uom"])
        entry = result.setdefault(k, {"quantity": Decimal(0), "serials": Counter()})
        entry["quantity"] += quantity(line["quantity"])
        entry["serials"].update(line["serials"])
    return result


def reconcile(work, document):
    differences = []
    if document.get("site") != work["site"] or document.get("order") != work["order"]:
        differences.append("Packlist belongs to a different site or order.")
    if document.get("status") not in ("A", "S", "P"):
        differences.append("Packlist is voided or its status is unsupported.")
    if not complete_address(document.get("destination", {})) or address(document.get("destination", {})) != address(work["destination"]):
        differences.append("Packlist destination differs from the checked-out destination.")
    expected, actual, bindings = contents(work), {}, []
    seen_lines = set()
    seen_serials = set()
    for line in document.get("lines", []):
        pl_line = str(line.get("packlist_line", ""))
        if not pl_line or pl_line in seen_lines:
            differences.append("Duplicate or missing VISUAL packlist-line identity.")
        seen_lines.add(pl_line)
        if line.get("order") != work["order"]:
            differences.append(f"Packlist line {pl_line} references another order.")
        k = (str(line.get("order_line")), line.get("part"), line.get("uom"))
        entry = actual.setdefault(k, {"quantity": Decimal(0), "serials": Counter()})
        entry["quantity"] += quantity(line.get("quantity"), zero=True)
        serials = line.get("serials", [])
        if not isinstance(serials, list):
            raise Conflict("Packlist serials must be a list.", "invalid", 400)
        entry["serials"].update(serials)
        for serial in serials:
            identity = (line.get("part"), serial)
            if identity in seen_serials:
                differences.append(f"Serial {serial} appears more than once.")
            seen_serials.add(identity)
        bindings.append({"packlist_line": pl_line, "order_line": str(line.get("order_line")),
                         "part": line.get("part"), "quantity": str(line.get("quantity")), "serials": serials})
    for k in sorted(set(expected) | set(actual), key=str):
        if expected.get(k) != actual.get(k):
            differences.append(f"Order line {k[0]} ({k[1]}): quantities or serials do not match.")
    return {"result": "mismatch" if differences else "pass", "differences": differences,
            "bindings": bindings, "fingerprint": digest(document)}


def packed_complete(work):
    totals, serials = {}, {}
    for package in work["packages"]:
        require(bool(package["contents"]), "Empty packages must be removed before release.")
        for item in package["contents"]:
            line_id = item["line_id"]
            totals[line_id] = totals.get(line_id, Decimal(0)) + quantity(item["quantity"])
            serials.setdefault(line_id, Counter()).update(item.get("serials", []))
    for line in work["lines"]:
        require(totals.get(line["id"], Decimal(0)) == quantity(line["quantity"]), "Every picked unit must be packed.")
        require(serials.get(line["id"], Counter()) == Counter(line["serials"]), "Packed serials must exactly match prescribed serials.")
    require(bool(work["packages"]), "Create at least one package.")
