"""Explicit database setup and account management: python -m fulfillment.cli --help."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment.model import Database, users, works, put_setting, now
from fulfillment.domain import identifier, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "user", "disable-user", "import-reservations", "activate"])
    parser.add_argument("--operator")
    parser.add_argument("--name")
    parser.add_argument("--role", choices=["operator", "supervisor"], default="operator")
    parser.add_argument("--legacy-db")
    parser.add_argument("--legacy-stopped", action="store_true", help="Attest legacy pick endpoints and scheduler have been disabled")
    args = parser.parse_args()
    url = os.environ.get("FULFILLMENT_DATABASE_URL")
    require(bool(url), "Set FULFILLMENT_DATABASE_URL first.")
    db = Database(url)
    if args.command == "init":
        db.initialize()
        print("Database schema is ready (version 1).")
        return
    with db.transaction() as conn:
        if args.command in ("user", "disable-user"):
            user_id = identifier(args.operator, "Operator")
            existing = conn.execute(select(users).where(users.c.id == user_id)).first()
            if args.command == "disable-user":
                require(existing is not None, "Operator not found.")
                conn.execute(users.update().where(users.c.id == user_id).values(enabled=False))
            else:
                pin = getpass.getpass("New PIN (at least 6 digits): ")
                require(pin.isdigit() and 6 <= len(pin) <= 32 and pin == getpass.getpass("Repeat PIN: "), "PINs must match and contain at least 6 digits.")
                values = dict(name=args.name or user_id, pin_hash=generate_password_hash(pin), role=args.role, enabled=True)
                if existing:
                    conn.execute(users.update().where(users.c.id == user_id).values(**values))
                else:
                    conn.execute(users.insert().values(id=user_id, **values))
            print("Operator updated.")
        else:
            require(args.legacy_db and args.legacy_stopped, "Provide --legacy-db and --legacy-stopped after disabling legacy picking and scheduled runs.")
            require(not conn.execute(select(works.c.id)).first(), "Migration/activation requires an empty fulfillment database.")
            with sqlite3.connect(Path(args.legacy_db).resolve().as_uri() + "?mode=ro", uri=True) as legacy:
                legacy.row_factory = sqlite3.Row
                unfinished = legacy.execute("SELECT COUNT(*) FROM pick_orders WHERE status IN ('picking','ready_for_pack','packing')").fetchone()[0]
                unfinished += legacy.execute("SELECT COUNT(*) FROM verify_sessions WHERE status='active'").fetchone()[0]
                require(unfinished == 0, "Finish or reconcile all legacy picks and verification sessions first.")
                reservations = [dict(r) for r in legacy.execute("SELECT * FROM release_gate_serial_reservations WHERE status='active'")]
            require(all(r.get("line_no") for r in reservations), "Some reservations lack order-line identity; reconcile them before import.")
            # Validate against the live adapter before preserving sticky assignments.
            from fulfillment.erp import VisualERP
            adapter = VisualERP(os.getenv("FULFILLMENT_MSSQL_CONNECTION_STRING"), os.getenv("FULFILLMENT_SITE"), args.legacy_db)
            with adapter.engine.begin() as source:
                supply = adapter.read(source, adapter.render("release_serial_supply.sql"), site=adapter.site)
                identities = {(s["PART_ID"], s["SERIAL_NO"]) for s in supply}
                for r in reservations:
                    require((r["part_id"], r["serial_no"]) in identities, "A reservation no longer exists in eligible ERP stock.")
                    rows = adapter.detail(source, r["cust_order_id"])
                    require(any(str(row["LINE_NO"]) == str(r["line_no"]) and row["PART_ID"] == r["part_id"] for row in rows), "A reservation references a changed order line.")
            put_setting(conn, "reservations", reservations)
            if args.command == "activate":
                put_setting(conn, "cutover", dict(site=adapter.site, activated=True, at=now(), operator=getpass.getuser()))
            print("Validated reservations imported." + (" Live cutover activated." if args.command == "activate" else ""))


if __name__ == "__main__":
    main()
