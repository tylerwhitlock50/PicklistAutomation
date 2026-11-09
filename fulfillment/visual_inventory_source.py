"""Complete, read-only VISUAL capture for the durable Dispatch inventory bridge.

Every query runs in one SERIALIZABLE SQL Server transaction. No maximum-ID cursor
is used: the consumer retains complete movement identities/fingerprints and can
therefore detect late lower IDs, removals and changed historical movements.

TRACE_PROFILE.SERIAL describes an optional trace dimension in this installation;
it is NOT a reliable serialized-part flag. A trace profile requires trace data,
and Dispatch accepts it only when each physical trace movement/balance is a unit.
Unsupported lot quantities or disagreement between ledgers fail closed.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from fulfillment.domain import Conflict, identifier, require


SQL_ROOT = Path(__file__).resolve().parent / "sql"
WO_FIELDS = ("WORKORDER_TYPE", "WORKORDER_BASE_ID", "WORKORDER_LOT_ID",
             "WORKORDER_SPLIT_ID", "WORKORDER_SUB_ID")


def fail(message):
    raise Conflict(message, "inventory_source", 503)


def number(value, label):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        fail(label + " is not a valid source quantity.")
    if not value.is_finite() or value.as_tuple().exponent < -8:
        fail(label + " is not a finite stocking quantity with at most eight decimals.")
    return value


def encoded(value):
    """Stable decimal representation, including equivalent SQL scale changes."""
    if not value:
        return "0"
    return format(value.normalize(), "f")


def canonical_work_order(values, *, optional=False):
    values = [str(v).strip().upper() if v is not None else "" for v in values]
    if optional and not any(values):
        return ""
    if len(values) != 5 or not all(values):
        fail("A VISUAL work-order reference is missing part of its five-column identity.")
    result = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if len(result) > 80 or any(ord(c) < 32 for c in result):
        fail("A VISUAL work-order identity exceeds Dispatch's 80-character identity limit.")
    return result


def source_identifier(value, label):
    try:
        return identifier(value, label)
    except Conflict:
        fail(label + " is missing or invalid in the VISUAL inventory source.")


def metadata(row):
    part = source_identifier(row.get("PART_ID"), "Part")
    uom = source_identifier(row.get("STOCK_UM"), "Stocking unit")
    return part, uom, bool(row.get("TRACE_PROFILE_PART_ID"))


def observed_timestamp(value):
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            fail("VISUAL did not return a valid observation timestamp.")
    # The query explicitly calls SYSUTCDATETIME(); SQL Server datetime2 has no tz.
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()


def source_date(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        fail("A work-order due date is invalid in VISUAL.")


class VisualInventorySource:
    """Capture one explicitly configured site/warehouse/location without writes.

    connection is a SQLAlchemy SQL Server URL. Construction does not connect.
    max_rows bounds each complete result set (including joined movement traces);
    crossing it rejects the capture instead of delivering a truncated snapshot.
    """

    def __init__(self, connection, scope, max_rows=100000):
        require(bool(connection), "A read-only VISUAL source connection is required.", "configuration", 503)
        require(isinstance(scope, dict), "Configure an inventory source boundary.", "configuration", 503)
        self.scope = {field: identifier(scope.get(field), field) for field in ("site", "warehouse", "location")}
        require(isinstance(max_rows, int) and not isinstance(max_rows, bool) and max_rows > 0,
                "The inventory source row limit must be a positive integer.", "configuration", 503)
        self.max_rows = max_rows
        self.engine = create_engine(connection, pool_pre_ping=True, isolation_level="SERIALIZABLE")

    def _read(self, conn, filename):
        query = (SQL_ROOT / filename).read_text(encoding="utf-8")
        result = conn.execute(text(query), self.scope)
        try:
            rows = result.mappings().fetchmany(self.max_rows + 1)
        finally:
            result.close()
        if len(rows) > self.max_rows:
            fail("VISUAL inventory capture exceeds its row limit. Increase the reviewed limit; a partial capture cannot synchronize stock.")
        return [dict(row) for row in rows]

    def capture(self):
        with self.engine.begin() as conn:
            boundary = self._read(conn, "inventory_sync_scope.sql")
            if len(boundary) != 1:
                fail("The configured inventory location is missing or does not belong to the configured VISUAL site.")
            stocks = self._read(conn, "inventory_sync_balances.sql")
            traces = self._read(conn, "inventory_sync_traces.sql")
            history = self._read(conn, "inventory_sync_movements.sql")
            orders = self._read(conn, "inventory_sync_work_orders.sql")
            observation = self._read(conn, "inventory_sync_observed.sql")
            if len(observation) != 1:
                fail("VISUAL did not return a unique observation timestamp.")
            # Validate before committing the read transaction, so malformed or
            # incomplete source evidence never escapes as a usable capture.
            movements, movement_totals = self._movements(history)
            balances = self._balances(stocks, traces, movement_totals)
            work_orders = self._work_orders(orders)
            return dict(scope=dict(self.scope), observed_at=observed_timestamp(observation[0]["OBSERVED_AT"]),
                        balances=balances, work_orders=work_orders, movements=movements,
                        evidence_version=2, complete=True)

    def _balances(self, stocks, traces, movement_totals):
        by_part, trace_by_part = {}, defaultdict(dict)
        for row in stocks:
            part, uom, traced = metadata(row)
            if part in by_part:
                fail("VISUAL returned duplicate stock rows inside the configured inventory boundary.")
            amount = number(row.get("QTY"), "PART_LOCATION.QTY")
            if amount < 0:
                fail("VISUAL has negative stock in the configured location; resolve the source discrepancy first.")
            # The first app-owned location has no independent quality-hold
            # bucket. Do not turn held/transit ERP stock into free local stock.
            if amount and (str(row.get("STATUS", "")).strip() != "A"
                           or str(row.get("LOCKED", "")).strip() != "N"
                           or str(row.get("TRANSIT", "")).strip() != "N"
                           or row.get("HOLD_REASON_ID")):
                fail("The configured VISUAL location contains held, locked, transit or unavailable stock.")
            by_part[part] = dict(part=part, uom=uom, amount=amount, traced=traced)
        for row in traces:
            part = source_identifier(row.get("PART_ID"), "Traced part")
            serial = source_identifier(row.get("TRACE_ID"), "Trace identity")
            amount = number(row.get("NET_QTY"), "Trace location balance")
            if amount != 1:
                fail("VISUAL contains non-unit or negative trace balances; Dispatch requires one unit per trace.")
            if serial in trace_by_part[part]:
                fail("VISUAL returned a duplicate current trace identity.")
            trace_by_part[part][serial] = amount
        result = []
        for part in sorted(set(by_part) | set(trace_by_part) | set(movement_totals)):
            stock = by_part.get(part)
            ledger_uom, ledger_qty = movement_totals.get(part, (None, Decimal(0)))
            if stock is None:
                if trace_by_part.get(part) or ledger_qty:
                    fail("VISUAL movement/trace stock is missing from PART_LOCATION.")
                continue
            if ledger_uom and ledger_uom != stock["uom"]:
                fail("VISUAL stock and movement stocking units disagree.")
            if ledger_qty != stock["amount"]:
                fail("VISUAL inventory transaction totals and PART_LOCATION disagree. Reconcile source stock before synchronization.")
            serials = sorted(trace_by_part.get(part, {}))
            if stock["traced"]:
                if stock["amount"] != len(serials):
                    fail("VISUAL trace identities and PART_LOCATION quantity disagree.")
            elif serials:
                fail("VISUAL has trace stock without a matching part/site trace profile.")
            if stock["amount"]:
                result.append(dict(part=part, uom=stock["uom"], quantity=encoded(stock["amount"]), serials=serials))
        return result

    def _movements(self, rows):
        grouped = {}
        for row in rows:
            transaction_id = row.get("TRANSACTION_ID")
            if isinstance(transaction_id, bool) or not isinstance(transaction_id, int) or transaction_id < 0:
                fail("VISUAL returned an invalid inventory transaction identity.")
            header = {k: v for k, v in row.items() if k not in ("TRACE_ID", "TRACE_QTY")}
            group = grouped.setdefault(transaction_id, {"header": header, "traces": {}})
            if group["header"] != header:
                fail("VISUAL returned conflicting headers for one inventory transaction.")
            if row.get("TRACE_ID") is not None:
                serial = source_identifier(row["TRACE_ID"], "Trace identity")
                if serial in group["traces"]:
                    fail("VISUAL returned a duplicate transaction/part/trace identity.")
                group["traces"][serial] = number(row.get("TRACE_QTY"), "TRACE_INV_TRANS.QTY")
        result, totals = [], {}
        for transaction_id in sorted(grouped):
            row, traces = grouped[transaction_id]["header"], grouped[transaction_id]["traces"]
            part, uom, traced = metadata(row)
            direction = str(row.get("TYPE", "")).strip()
            category = str(row.get("CLASS", "")).strip()
            if direction not in ("I", "O") or category not in ("I", "A", "R", "C"):
                fail("VISUAL returned an unsupported inventory direction or class.")
            raw_qty = number(row.get("QTY"), "INVENTORY_TRANS.QTY")
            amount = raw_qty if direction == "I" else -raw_qty
            if any(abs(qty) != 1 for qty in traces.values()):
                fail("VISUAL contains non-unit trace movements; lot quantities are not supported by Dispatch inventory.")
            if traces and not traced:
                fail("VISUAL movement traces have no matching part/site trace profile.")
            if traced and sum(traces.values(), Decimal(0)) != amount:
                fail("VISUAL inventory movement quantity does not equal its signed trace quantities.")
            # Even balanced +1/-1 rows within a single transaction are ambiguous
            # for the unit receipt API; they must be corrected or separately reviewed.
            if traces and any(qty != (1 if amount > 0 else -1) for qty in traces.values()):
                fail("VISUAL contains mixed-direction traces in one movement.")
            work_order = canonical_work_order([row.get(field) for field in WO_FIELDS], optional=True)
            kind = "other"
            in_transit = str(row.get("XFER_IN_TRANSIT") or "").strip().upper() == "Y"
            if direction == "I" and raw_qty > 0 and not in_transit:
                if category == "R" and row.get("TRANSFER_TRANS_ID") is None and self._receipt_origin(row, work_order):
                    kind = "receipt"
                elif category == "A" and self._inbound_transfer(row, transaction_id, part, raw_qty):
                    kind = "receipt"
            elif direction == "O" and raw_qty > 0 and category == "I" and row.get("CUST_ORDER_ID") and not in_transit:
                kind = "shipment"
            existing_uom, existing_qty = totals.get(part, (uom, Decimal(0)))
            if existing_uom != uom:
                fail("VISUAL returned different stocking units for the same part.")
            totals[part] = (uom, existing_qty + amount)
            result.append(dict(id=str(transaction_id), part=part, uom=uom, quantity=encoded(amount),
                               serials=[dict(serial=serial, quantity=encoded(qty)) for serial, qty in sorted(traces.items())],
                               kind=kind, work_order=work_order,
                               direction=direction, category=category,
                               effective_date=source_date(row.get("TRANSACTION_DATE")),
                               source_created_local=str(row.get("CREATE_DATE") or ""),
                               customer_order=str(row.get("CUST_ORDER_ID") or "").strip().upper(),
                               customer_order_line=str(row.get("CUST_ORDER_LINE_NO") if row.get("CUST_ORDER_LINE_NO") is not None else ""),
                               document=str(row.get("PACKLIST_ID") or "").strip().upper(),
                               document_line=str(row.get("PACKLIST_LINE_NO") if row.get("PACKLIST_LINE_NO") is not None else ""),
                               purchase_order=str(row.get("PURC_ORDER_ID") or "").strip().upper(),
                               purchase_order_line=str(row.get("PURC_ORDER_LINE_NO") if row.get("PURC_ORDER_LINE_NO") is not None else ""),
                               transfer_id=str(row.get("TRANSFER_TRANS_ID") if row.get("TRANSFER_TRANS_ID") is not None else ""),
                               transfer_peer={field: str(row.get("PAIR_" + field.upper()) or "")
                                              for field in ("transaction_id", "site_id", "warehouse_id", "location_id", "type", "class", "part_id", "qty")}))
        return result, totals

    @staticmethod
    def _receipt_origin(row, work_order):
        # CLASS R describes a receipt, not why it arrived. A customer return or
        # unmatched source reference must not silently become shippable stock.
        # SQL verifies document existence against the same site and physical
        # part; PO verification includes the exact line and WO all five keys.
        if any(row.get(field) is not None and str(row[field]).strip()
               for field in ("CUST_ORDER_ID", "CUST_ORDER_LINE_NO")):
            return False
        purchase_id = str(row.get("PURC_ORDER_ID") or "").strip()
        purchase_line = row.get("PURC_ORDER_LINE_NO")
        has_purchase_reference = bool(purchase_id) or purchase_line is not None
        manufacturing = (bool(work_order) and str(row.get("WORKORDER_TYPE") or "").strip() == "W"
                         and row.get("VERIFIED_WORK_ORDER") == 1 and not has_purchase_reference)
        purchased = (bool(purchase_id) and purchase_line is not None
                     and row.get("VERIFIED_PURCHASE_ORDER_LINE") == 1 and not work_order)
        return manufacturing or purchased

    def _inbound_transfer(self, row, transaction_id, part, amount):
        if row.get("TRANSFER_TRANS_ID") is None:
            return False
        # Pair evidence is complete only when the source row points back and
        # matches the same physical part/quantity with the opposite direction.
        if (row.get("PAIR_TRANSACTION_ID") != row["TRANSFER_TRANS_ID"]
                or row.get("PAIR_TRANSFER_TRANS_ID") != transaction_id
                or row.get("PAIR_TYPE") != "O" or row.get("PAIR_CLASS") != "A"
                or str(row.get("PAIR_PART_ID") or "").strip().upper() != part
                or number(row.get("PAIR_QTY"), "Transfer counterpart quantity") != amount
                or str(row.get("PAIR_XFER_IN_TRANSIT") or "").strip().upper() == "Y"):
            return False
        counterpart = tuple(str(row.get("PAIR_" + field.upper()) or "").strip().upper()
                            for field in ("site_id", "warehouse_id", "location_id"))
        return all(counterpart) and counterpart != tuple(self.scope[f] for f in ("site", "warehouse", "location"))

    def _work_orders(self, rows):
        result, seen = [], set()
        for row in rows:
            values = [row.get(field) for field in ("TYPE", "BASE_ID", "LOT_ID", "SPLIT_ID", "SUB_ID")]
            identity = canonical_work_order(values)
            if identity in seen:
                fail("VISUAL returned duplicate five-part work-order identities.")
            seen.add(identity)
            part, uom, traced = metadata(row)
            desired = number(row.get("DESIRED_QTY"), "WORK_ORDER.DESIRED_QTY")
            received = number(row.get("RECEIVED_QTY"), "WORK_ORDER.RECEIVED_QTY")
            state = str(row.get("STATUS", "")).strip()
            if values[0] != "W" or state not in ("U", "F", "R", "C", "X") or desired < 0 or received < 0:
                fail("VISUAL returned unsupported work-order status or quantities.")
            eligible = state in ("F", "R") and str(values[4]).strip() == "0" and not row.get("HAS_DEMAND_LINK")
            remaining = max(Decimal(0), desired - received) if eligible else Decimal(0)
            if traced and remaining != int(remaining):
                fail("A traced work order has fractional remaining supply.")
            status = "cancelled" if state == "X" else "open" if eligible else "closed"
            result.append(dict(work_order=identity, part=part, uom=uom, serialized=traced,
                               remaining=encoded(remaining), status=status, due_date=source_date(row.get("DUE_DATE"))))
        return sorted(result, key=lambda row: row["work_order"])
