"""Optional one-time read-only legacy import. Never imported by API workers."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from sqlalchemy import select
from fulfillment import admin, model as m
from fulfillment.domain import require


def legacy_snapshot(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        settings = {r["key"]: r["value"] for r in source.execute("SELECT key,value FROM settings")}
        unfinished = source.execute("SELECT COUNT(*) FROM pick_orders WHERE status IN ('picking','ready_for_pack','packing')").fetchone()[0]
        unfinished += source.execute("SELECT COUNT(*) FROM verify_sessions WHERE status='active'").fetchone()[0]
        reservations = [dict(r) for r in source.execute("SELECT * FROM release_gate_serial_reservations WHERE status='active'")]
        exceptions = [dict(r) for r in source.execute("SELECT * FROM release_gate_exceptions WHERE revoked_at IS NULL")]
        holds = [dict(r) for r in source.execute("SELECT * FROM manual_order_holds WHERE released_at IS NULL")]
        documents = [dict(r) for r in source.execute("SELECT * FROM readiness_holds WHERE cleared_at IS NULL AND blocking=1 AND reason_code IN ('ship_to_vs_ffl_name_mismatch','ship_to_vs_ffl_premise_mismatch')")]
    rules = deepcopy(admin.DEFAULT_RULES)
    rules["policies"] = json.loads(settings.get("release_gate_customer_policies_json") or "{}")
    for old, new in (("release_gate_due_override_days", "due_days"), ("release_gate_min_ship_to_cooldown_days", "cooldown"), ("guns_lookahead_days", "lookahead_days")):
        if settings.get(old):
            rules[new] = int(settings[old])
    if settings.get("shortage_product_codes"):
        rules["component_codes"] = [v.strip() for v in settings["shortage_product_codes"].split(",") if v.strip()]
    rules["document_checks_required"] = str(settings.get("readiness_ocr_enabled", "false")).lower() == "true"
    rules = admin.validate_rules(rules)
    controls = []
    for kind, rows in (("early_release", exceptions), ("hold", holds), ("hold", documents)):
        for row in rows:
            expires = row.get("expires_at")
            if expires:
                date = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
                if date <= datetime.now(timezone.utc):
                    continue
                expires = date.astimezone(timezone.utc).isoformat()
            require(kind != "early_release" or expires, "A legacy early-release exception has no expiry; reconcile it before import.")
            controls.append({"id": str(uuid4()), "kind": kind, "order": row["cust_order_id"], "reason": row.get("reason") or row.get("reason_code") or "Imported hold",
                             "expires_at": expires, "revoked_at": None, "actor": "legacy-import", "at": m.now(), "legacy_id": row.get("id")})
    return {"rules": rules, "controls": controls, "reservations": reservations, "unfinished": unfinished}


def import_legacy(conn, data, erp, actor):
    state = admin.load(conn)
    require(not state["configured"] and not state["imported"] and not state["controls"] and not m.get_setting(conn, "reservations", []), "Import requires a fresh, unconfigured Dispatch administration database.")
    require(not conn.execute(select(m.works.c.id)).first() and data["unfinished"] == 0, "Finish or reconcile all existing warehouse work before importing.")
    reservations = data["reservations"]
    require(all(r.get("line_no") is not None for r in reservations), "Reservations must reference exact order lines.")
    with erp.engine.begin() as source:
        supply = erp.read(source, erp.render("release_serial_supply.sql"), site=erp.site)
        identities = {(s["PART_ID"], s["SERIAL_NO"]) for s in supply if Decimal(str(s["NET_QTY"])) == 1}
        seen = set()
        for reservation in reservations:
            trace = (reservation["part_id"], reservation["serial_no"])
            require(trace in identities and trace not in seen, "A reservation is duplicated or missing from eligible ERP stock.")
            seen.add(trace)
            rows = erp.detail(source, reservation["cust_order_id"], data["rules"])
            require(any(str(r["LINE_NO"]) == str(reservation["line_no"]) and r["PART_ID"] == reservation["part_id"] for r in rows), "A reservation references a changed order line.")
    state.update(rules=data["rules"], controls=data["controls"], version=state["version"] + 1, imported={"at": m.now(), "actor": actor}, configured=False)
    m.put_setting(conn, "administration", state)
    m.put_setting(conn, "reservations", reservations)
    from fulfillment.service import event
    event(conn, actor, "admin:legacy-import", {"state": state, "reservation_count": len(reservations)})
