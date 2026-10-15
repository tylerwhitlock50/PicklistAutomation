"""Read-only VISUAL boundary and deterministic training source.

No toolkit calls, ERP writes, scheduler, or legacy Flask application imports.
Production reads use SERIALIZABLE SQL Server transactions without NOLOCK.
"""
from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from fulfillment.domain import address, complete_address, identifier, key, quantity, require, Conflict
from fulfillment.model import now
from fulfillment.rules.release_gate import evaluate_release_gate
from fulfillment.rules.readiness import evaluate_readiness, STOCK_REASONS
from fulfillment.rules.pick_path import pick_line_sort_key
from fulfillment import admin
from fulfillment.model import digest

ROOT = Path(__file__).resolve().parent
COMPONENTS = "FG-COMP,FG-STOCK,FG-APPAREL,COMPONENT,FG-BASE,FG-RING,FG-BARREL,FG-MUZZLE"


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class DemoERP:
    """Explicit synthetic fixtures; never enabled by a production fallback."""
    def __init__(self):
        destination = dict(name="Demo Supply", address1="100 Example Way", address2="", address3="",
                           city="Denver", state="CO", postal="80202", country="US")
        self.orders = [{"site": "DEMO", "order": "SO-DEMO-100", "customer": "Demo Supply", "ship_to": "MAIN",
                        "destination": destination, "policy": {"decision": "RELEASE", "reason": "training"},
                        "lines": [dict(order_line="1", part="ASSEMBLY-A", quantity="2", uom="EA", upc="100001",
                                       serialized=True, serials=["DEMO-001", "DEMO-002"], warehouse="SHIPPING", location="R09S02"),
                                  dict(order_line="2", part="ACCESSORY-B", quantity="6", uom="EA", upc="100002",
                                       serialized=False, serials=[], warehouse="DISTRIBUTION", location="BIN-04")]}]
        self.packlists = {}

    def context(self):
        with self.database.engine.connect() as conn:
            return admin.context(conn)

    def inspect_order(self, order):
        result = next((deepcopy(o) for o in self.orders if o["order"] == order), None)
        require(result is not None, "Training order not found.", "not_found", 404)
        result["document_fingerprint"] = digest([result["order"], address(result["destination"])])
        result["document_context"] = {"training": "Synthetic order; no real documents"}
        return result

    def available(self, works, reservations, context=None):
        context = context or (self.context() if hasattr(self, "database") else admin.context(state=admin.initial_state()))
        active = {w["order"] for w in works if w["state"] != "cancelled"}
        candidates = []
        for order in self.orders:
            if order["order"] in active:
                continue
            supply = {}
            for line in order["lines"]:
                supply[line["part"]] = supply.get(line["part"], Decimal(0)) + quantity(line["quantity"])
            for line in order["lines"]:
                candidates.append(dict(CUST_ORDER_ID=order["order"], CUSTOMER_ID=order.get("customer_id", "DEMO-SUPPLY"), CUSTOMER_NAME=order["customer"],
                    SHIP_TO_ID=order["ship_to"], LINE_NO=line["order_line"], PART_ID=line["part"], ITEM_TYPE="guns" if line["serialized"] else "components",
                    OPEN_QTY=line["quantity"], AVAILABLE_QTY=supply[line["part"]], ORDER_RELEASED=1, LINE_AVAILABLE=1, CREDIT_APPROVED=1, SHIP_TO_PRESENT=1))
        gate = evaluate_release_gate(candidates, today=datetime.now(ZoneInfo(context["timezone"])).date(), mode="enforced",
            customer_policies=context["policies"], due_override_days=context["due_days"], exceptions=context["exceptions"],
            manual_holds=context["holds"], min_ship_to_cooldown_days=context["cooldown"], local_now=datetime.now(ZoneInfo(context["timezone"])))
        eligible = []
        for decision in gate["decisions"]:
            order = self.inspect_order(decision["order_id"])
            reasons = admin.order_blocks(context, order["order"], order["document_fingerprint"])
            if reasons:
                decision.update(decision="BLOCKED", label=", ".join(reasons))
            if decision["decision"] == "RELEASE":
                order["policy"] = clean(decision)
                order["policy_version"] = context["version"]
                eligible.append(order)
        return {"orders": eligible, "reservations": reservations, "decisions": clean(gate["decisions"]), "as_of": now()}

    def check_pick(self, work, line, payload):
        self.check_order(work)

    def check_order(self, work, final=False):
        if hasattr(self, "database"):
            reasons = admin.order_blocks(self.context(), work["order"], self.inspect_order(work["order"])["document_fingerprint"])
            require(not reasons, "Order is blocked: " + ", ".join(reasons), "hold")

    def packlist(self, packlist, work):
        require(packlist in self.packlists, "Packlist not found. In training, load the sample packlist first.", "not_found", 404)
        return deepcopy(self.packlists[packlist])


class VisualERP:
    def __init__(self, connection, site, database):
        require(bool(connection) and bool(site) and database is not None, "VISUAL connection, site and Dispatch database are required.")
        self.engine = create_engine(connection, pool_pre_ping=True, isolation_level="SERIALIZABLE")
        self.site, self.database = site.upper(), database

    def context(self):
        with self.database.engine.connect() as conn:
            return admin.context(conn)

    @staticmethod
    def read(conn, query, **params):
        rows = conn.execute(text(query), params).mappings().fetchmany(20001)
        require(len(rows) <= 20000, "ERP result exceeds the configured safety limit; narrow the operating site.", "unavailable", 503)
        return [dict(r) for r in rows]

    @staticmethod
    def render(filename, rules=None):
        rules = rules or admin.DEFAULT_RULES
        query = (ROOT / "sql" / filename).read_text(encoding="utf-8")
        query = re.sub(r"\s+WITH\s*\(NOLOCK\)", "", query, flags=re.I)
        def literals(values, alias):
            return " UNION ALL ".join("SELECT '" + v.replace("'", "''") + "' AS " + alias for v in values) if values else "SELECT CAST(NULL AS varchar(80)) AS " + alias + " WHERE 1=0"
        return (query.replace("__RELEASE_LOOKAHEAD_DAYS__", str(rules["lookahead_days"]))
                .replace("__COMPONENT_PRODUCT_CODES__", literals(rules["component_codes"], "PRODUCT_CODE"))
                .replace("__RELEASE_EXCLUDED_CUSTOMERS__", literals(rules["excluded_customers"], "CUSTOMER_TERM"))
                .replace("__RELEASE_SERIAL_PART_FILTER__", "AND it.SITE_ID = :site"))

    def candidate_query(self, rules=None):
        query = self.render("release_candidates.sql", rules)
        query = query.replace("WHERE col.PART_ID IS NOT NULL", "WHERE co.SITE_ID = :site AND col.PART_ID IS NOT NULL")
        # Site-aware bin supply even when part IDs are shared between sites.
        query = query.replace("WHERE pl.QTY > 0", "WHERE pl.QTY > 0 AND EXISTS (SELECT 1 FROM dbo.WAREHOUSE wh WHERE wh.ID = pl.WAREHOUSE_ID AND wh.SITE_ID = :site)")
        return query

    def detail(self, conn, order, rules=None):
        if rules is None and hasattr(self, "database"):
            rules = self.context()
        query = self.render("order_detail.sql", rules)
        query = query.replace("co.ID                                                   AS CUST_ORDER_ID,", "co.ID AS CUST_ORDER_ID, co.SITE_ID, p.STOCK_UM,")
        query = query.replace("WHERE co.ID = :so", "WHERE co.ID = :so AND co.SITE_ID = :site")
        rows = self.read(conn, query, so=order, site=self.site)
        require(rows and all(r.get("SITE_ID") == self.site for r in rows), "Order is missing or belongs to another site.")
        return rows

    def stocks(self, conn):
        return self.read(conn, """SELECT pl.PART_ID, pl.WAREHOUSE_ID, pl.LOCATION_ID,
            pl.QTY, p.STOCK_UM, p.PRODUCT_CODE, ps.USER_6 AS UPC
            FROM dbo.PART_LOCATION pl JOIN dbo.WAREHOUSE w ON w.ID=pl.WAREHOUSE_ID
            JOIN dbo.PART p ON p.ID=pl.PART_ID
            LEFT JOIN dbo.PART_SITE_VIEW ps ON ps.PART_ID=pl.PART_ID AND ps.SITE_ID=w.SITE_ID
            WHERE w.SITE_ID=:site AND pl.QTY>0 AND pl.HOLD_REASON_ID IS NULL
              AND ((pl.WAREHOUSE_ID='SHIPPING' AND
                  (LEFT(pl.LOCATION_ID,3) BETWEEN 'R01' AND 'R09' OR LEFT(pl.LOCATION_ID,3)='R11')
                  AND pl.LOCATION_ID NOT LIKE '%STAGE%' AND pl.LOCATION_ID NOT LIKE '%INTERNATIONAL%')
                OR (pl.WAREHOUSE_ID='DISTRIBUTION' AND pl.LOCATION_ID NOT LIKE '%STOCK%'))
            ORDER BY pl.PART_ID,pl.WAREHOUSE_ID,pl.LOCATION_ID""", site=self.site)

    @staticmethod
    def destination(rows):
        h = rows[0]
        return address(dict(name=h.get("SHIPTO_NAME"), address1=h.get("SHIPTO_ADDR_1"), address2=h.get("SHIPTO_ADDR_2"),
                            address3=h.get("SHIPTO_ADDR_3"), city=h.get("SHIPTO_CITY"), state=h.get("SHIPTO_STATE"),
                            postal=h.get("SHIPTO_ZIP"), country=h.get("SHIPTO_COUNTRY")))

    def blocking(self, rows, context):
        assessed = evaluate_readiness(rows, today=datetime.now(ZoneInfo(context["timezone"])).date(), manual_holds=context["holds"])
        reasons = [h["reason_code"] for h in assessed["holds"] if h["blocking"] and h["reason_code"] not in STOCK_REASONS]
        reasons += admin.order_blocks(context, rows[0]["CUST_ORDER_ID"], self.document_fingerprint(rows))
        return reasons

    def document_fingerprint(self, rows):
        fields = {k: clean(v) for k, v in rows[0].items() if "FFL" in k or "DOC" in k or "ATTACH" in k}
        return digest([self.site, rows[0]["CUST_ORDER_ID"], self.destination(rows), fields])

    def inspect_order(self, order):
        with self.engine.begin() as conn:
            rows = self.detail(conn, order)
        return dict(order=order, site=self.site, customer=rows[0]["CUSTOMER_NAME"], destination=self.destination(rows),
                    document_fingerprint=self.document_fingerprint(rows),
                    document_context={k: clean(v) for k, v in rows[0].items() if "FFL" in k or "DOC" in k or "ATTACH" in k})

    def available(self, works, reservations, context=None):
        context = context or self.context()
        require(context.get("configured"), "Review and publish Dispatch settings, or import them before starting live work.", "configuration", 503)
        local_timezone = ZoneInfo(context["timezone"])
        with self.engine.begin() as conn:
            candidates = self.read(conn, self.candidate_query(context), site=self.site)
            serials = self.read(conn, self.render("release_serial_supply.sql"), site=self.site)
            identities = Counter((s["PART_ID"], s["SERIAL_NO"]) for s in serials)
            require(all(quantity(s["NET_QTY"]) == 1 and identities[(s["PART_ID"], s["SERIAL_NO"])] == 1 for s in serials),
                    "ERP serial supply contains non-unit or duplicate trace locations. Resolve trace data first.", "unavailable", 503)
            stocks = self.stocks(conn)
            history_sql = self.render("release_shipto_history.sql").replace("WHERE s.SHIPPED_DATE >=", "WHERE s.SITE_ID=:site AND s.SHIPPED_DATE >=")
            history = self.read(conn, history_sql, site=self.site)
            occupied = [w for w in works if w["state"] not in ("cancelled", "released")]
            # A released order remains unavailable until ERP confirms its quantities.
            for work in works:
                if work["state"] == "released":
                    rows = self.detail(conn, work["order"], context)
                    totals = {str(r["LINE_NO"]): quantity(r["SHIPPED_QTY"], zero=True) for r in rows}
                    expected = {}
                    for l in work["lines"]:
                        expected[l["order_line"]] = expected.get(l["order_line"], Decimal(0)) + quantity(l["quantity"])
                    if any(totals.get(n, Decimal(0)) < quantity(work["baseline"][n]["shipped"], zero=True) + q for n, q in expected.items()):
                        occupied.append(work)
            occupied_ids = {w["order"] for w in occupied}
            reserved_serials = {(l["part"], s) for w in occupied for l in w["lines"] for s in l["serials"]}
            serials = [s for s in serials if (s["PART_ID"], s["SERIAL_NO"]) not in reserved_serials]
            stock_by_bin = {(s["PART_ID"], s["WAREHOUSE_ID"], s["LOCATION_ID"]): s for s in stocks}
            for work in occupied:
                for l in work["lines"]:
                    stock = stock_by_bin.get((l["part"], l["warehouse"], l["location"]))
                    if stock:
                        stock["QTY"] = max(Decimal(0), quantity(stock["QTY"], zero=True) - quantity(l["quantity"]))
            candidates = [r for r in candidates if r["CUST_ORDER_ID"] not in occupied_ids]
            by_part = {}
            for s in stocks:
                by_part[s["PART_ID"]] = by_part.get(s["PART_ID"], Decimal(0)) + quantity(s["QTY"], zero=True)
            for r in candidates:
                r["AVAILABLE_QTY"] = min(quantity(r["AVAILABLE_QTY"], zero=True), by_part.get(r["PART_ID"], Decimal(0)))
            # The legacy policy engine keys serials by string. Feed composite aliases
            # so identical display serials on different parts cannot collide.
            serial_map = {key(self.site, s["PART_ID"], s["SERIAL_NO"]).upper(): s for s in serials}
            gate_serials = [{**s, "SERIAL_NO": alias} for alias, s in serial_map.items()]
            gate_reservations = [{**r, "serial_no": key(self.site, r["part_id"], r["serial_no"]).upper()} for r in reservations if r["cust_order_id"] not in occupied_ids]
            local = datetime.now(local_timezone)
            for w in works:
                if w["state"] != "cancelled":
                    history.append({"CUSTOMER_ID": w.get("customer_id"), "SHIP_TO_ID": w["ship_to"], "LAST_SHIPPED_DATE": datetime.fromisoformat(w["created_at"]).astimezone(local_timezone).date()})
            gate = evaluate_release_gate(candidates, today=local.date(), mode="enforced", customer_policies=context["policies"],
                                         due_override_days=context["due_days"], exceptions=context["exceptions"], serial_inventory=gate_serials,
                                         reservations=gate_reservations, ship_to_history=history, local_now=local,
                                         min_ship_to_cooldown_days=context["cooldown"], manual_holds=context["holds"])
            # The legacy gate assigns concrete serials only for configured batches.
            # Ordinary released orders also need a deterministic prescription here.
            protected = {r["serial_no"] for r in gate["reservation_updates"]}
            unassigned = sorted([s for alias, s in serial_map.items() if alias not in protected],
                                key=lambda s: (str(s.get("LAST_TRANSACTION_AT") or ""), s["SERIAL_NO"]))
            assigned_traces = set()
            orders = []
            for decision in gate["decisions"]:
                if decision["decision"] != "RELEASE":
                    continue
                rows = self.detail(conn, decision["order_id"], context)
                blocked = self.blocking(rows, context)
                if blocked or not complete_address(self.destination(rows)):
                    decision.update(decision="BLOCKED", label=", ".join(blocked) or "Incomplete destination")
                    continue
                details = {str(r["LINE_NO"]): r for r in rows}
                lines = []
                for allocation in decision["allocations"]:
                    n, part = str(allocation["line_no"]), allocation["part_id"]
                    remain = quantity(allocation["qty"])
                    serialized = allocation["item_type"] == "guns"
                    selected = [serial_map[a["serial_no"]] for a in decision["serial_assignments"] if str(a["line_no"]) == n and a["part_id"] == part]
                    if serialized and not decision.get("batch_key"):
                        selected = [s for s in unassigned if s["PART_ID"] == part][:int(remain)]
                        for s in selected:
                            unassigned.remove(s)
                    if serialized:
                        require(remain == int(remain) and len(selected) == int(remain), "Serial prescription is incomplete.", "unavailable", 503)
                        for s in selected:
                            trace = (part, s["SERIAL_NO"])
                            require(trace not in assigned_traces, "A serial was allocated to more than one order line.", "unavailable", 503)
                            assigned_traces.add(trace)
                    bins = sorted([s for s in stocks if s["PART_ID"] == part and quantity(s["QTY"], zero=True) > 0
                                   and (serialized or s["WAREHOUSE_ID"] == "DISTRIBUTION" or s["LOCATION_ID"].startswith("R11"))],
                                  key=lambda s: pick_line_sort_key({"location": s["LOCATION_ID"]}))
                    for stock in bins:
                        if not remain:
                            break
                        sn = [s["SERIAL_NO"] for s in selected if s["WAREHOUSE_ID"] == stock["WAREHOUSE_ID"] and s["LOCATION_ID"] == stock["LOCATION_ID"]]
                        amount = min(remain, Decimal(len(sn)) if serialized else quantity(stock["QTY"], zero=True))
                        if amount <= 0:
                            continue
                        lines.append(dict(order_line=n, part=part, quantity=str(amount), uom=details[n]["STOCK_UM"],
                                          serialized=serialized, serials=sn[:int(amount)] if serialized else [], upc=stock.get("UPC"),
                                          warehouse=stock["WAREHOUSE_ID"], location=stock["LOCATION_ID"]))
                        stock["QTY"] = quantity(stock["QTY"], zero=True) - amount
                        remain -= amount
                    require(remain == 0, "Released quantities cannot be resolved to eligible bins.", "unavailable", 503)
                baseline = {n: {"ordered": str(r["ORDER_QTY"]), "shipped": str(r["SHIPPED_QTY"]), "part": r["PART_ID"], "uom": r["STOCK_UM"]} for n, r in details.items()}
                orders.append(dict(site=self.site, order=decision["order_id"], customer=rows[0]["CUSTOMER_NAME"],
                                   customer_id=rows[0]["CUSTOMER_ID"], ship_to=rows[0]["SHIP_TO_ID"], destination=self.destination(rows),
                                   lines=lines, baseline=baseline, policy=clean(decision), policy_version=context["version"]))
            saved = [{**r, "serial_no": serial_map[r["serial_no"]]["SERIAL_NO"]} for r in gate["reservation_updates"]]
            return {"orders": clean(orders), "reservations": clean(saved), "decisions": clean(gate["decisions"]), "as_of": now()}

    def check_order(self, work, final=False):
        context = self.context()
        with self.engine.begin() as conn:
            rows = self.detail(conn, work["order"])
        require(self.destination(rows) == address(work["destination"]), "Order destination changed. Resolve the order before continuing.")
        by_line = {str(r["LINE_NO"]): r for r in rows}
        for n, original in work["baseline"].items():
            row = by_line.get(n)
            require(row and row["PART_ID"] == original["part"] and row["STOCK_UM"] == original["uom"] and quantity(row["ORDER_QTY"], zero=True) == quantity(original["ordered"], zero=True), "Order lines changed after checkout.")
            assigned = sum((quantity(l["quantity"]) for l in work["lines"] if l["order_line"] == n), Decimal(0))
            delta = quantity(row["SHIPPED_QTY"], zero=True) - quantity(original["shipped"], zero=True)
            require(delta == (assigned if final else 0), "ERP shipped quantities changed or the saved shipment is not fully posted.")
        require(set(by_line) == set(work["baseline"]), "Order lines were added after checkout.")
        # Reevaluate hard holds even when this packlist consumed the last open unit.
        check_rows = deepcopy(rows)
        for row in check_rows:
            n = str(row["LINE_NO"])
            row["OPEN_QTY"] = sum((quantity(l["quantity"]) for l in work["lines"] if l["order_line"] == n), Decimal(0))
            if final and row["ORDER_STATUS"] == "C":
                row["ORDER_STATUS"] = "R"
        reasons = self.blocking(check_rows, context)
        require(not reasons, "Order is blocked: " + ", ".join(reasons), "hold")

    def check_pick(self, work, line, payload):
        self.check_order(work)
        with self.engine.begin() as conn:
            stocks = self.stocks(conn)
            stock = next((s for s in stocks if s["PART_ID"] == line["part"] and s["WAREHOUSE_ID"] == line["warehouse"] and s["LOCATION_ID"] == line["location"]), None)
            require(stock and quantity(stock["QTY"]) >= quantity(line["quantity"]), "Prescribed stock is no longer available in this bin.")
            if line["serialized"]:
                serial = identifier(payload.get("serial"))
                serials = self.read(conn, self.render("release_serial_supply.sql"), site=self.site)
                require(any(s["PART_ID"] == line["part"] and s["SERIAL_NO"] == serial and s["LOCATION_ID"] == line["location"] and s["WAREHOUSE_ID"] == line["warehouse"] for s in serials), "Serial is not available in the prescribed bin.")

    def packlist(self, packlist, work):
        with self.engine.begin() as conn:
            header = self.read(conn, """SELECT s.SITE_ID,s.CUST_ORDER_ID,s.STATUS,ca.NAME,ca.ADDR_1,ca.ADDR_2,ca.ADDR_3,
                ca.CITY,ca.STATE,ca.ZIPCODE,ca.COUNTRY FROM dbo.SHIPPER s
                JOIN dbo.CUSTOMER_ORDER co ON co.ID=s.CUST_ORDER_ID
                LEFT JOIN dbo.CUST_ADDRESS ca ON ca.CUSTOMER_ID=co.CUSTOMER_ID
                  AND ca.ADDR_NO=COALESCE(s.SHIP_TO_ADDR_NO,co.SHIP_TO_ADDR_NO)
                WHERE s.PACKLIST_ID=:packlist AND s.SITE_ID=:site""", packlist=packlist, site=self.site)
            require(len(header) == 1, "Packlist not found at this site.", "not_found", 404)
            # Always stocking quantity; USER_SHIPPED_QTY is a different unit.
            rows = self.read(conn, """SELECT sl.LINE_NO,sl.CUST_ORDER_ID,sl.CUST_ORDER_LINE_NO,col.PART_ID,
                p.STOCK_UM,sl.SHIPPED_QTY,tit.TRACE_ID,tit.QTY AS TRACE_QTY
                FROM dbo.SHIPPER_LINE sl LEFT JOIN dbo.CUST_ORDER_LINE col
                  ON col.CUST_ORDER_ID=sl.CUST_ORDER_ID AND col.LINE_NO=sl.CUST_ORDER_LINE_NO
                LEFT JOIN dbo.PART p ON p.ID=col.PART_ID
                LEFT JOIN dbo.TRACE_INV_TRANS tit ON tit.TRANSACTION_ID=sl.TRANSACTION_ID AND tit.PART_ID=col.PART_ID
                WHERE sl.PACKLIST_ID=:packlist ORDER BY sl.LINE_NO,tit.TRACE_ID""", packlist=packlist)
        h, grouped = header[0], {}
        for r in rows:
            line = grouped.setdefault(str(r["LINE_NO"]), dict(packlist_line=str(r["LINE_NO"]), order=r["CUST_ORDER_ID"],
                        order_line=str(r["CUST_ORDER_LINE_NO"]), part=r["PART_ID"], uom=r["STOCK_UM"], quantity=str(r["SHIPPED_QTY"]), serials=[]))
            if r["TRACE_ID"]:
                require(abs(Decimal(str(r["TRACE_QTY"]))) == 1, "Serialized trace quantity is not one; resolve ERP trace data.")
                line["serials"].append(r["TRACE_ID"])
        return dict(id=packlist, site=h["SITE_ID"], order=h["CUST_ORDER_ID"], status=h["STATUS"],
                    destination=address(dict(name=h["NAME"], address1=h["ADDR_1"], address2=h["ADDR_2"], address3=h["ADDR_3"], city=h["CITY"], state=h["STATE"], postal=h["ZIPCODE"], country=h["COUNTRY"])), lines=list(grouped.values()))

    def check_label(self, label):
        with self.engine.begin() as conn:
            rows = self.read(conn, """SELECT z.PACKLIST_ID,z.ORDER_ID,z.VOID FROM dbo.Z_UPS_SHIPMENTS z
                JOIN dbo.SHIPPER s ON s.PACKLIST_ID=z.PACKLIST_ID WHERE z.TRACKING_NUMBER=:tracking
                AND s.SITE_ID=:site""", tracking=label["tracking"], site=self.site)
        require(bool(rows) and all(r["PACKLIST_ID"] == label["packlist"] and r["ORDER_ID"] == label["order"] and str(r["VOID"]).strip().upper() in ("N", "0", "FALSE", "") for r in rows), "Live UPS export is missing, voided or conflicts with this label.", "label_pending")
