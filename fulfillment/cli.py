"""Independent Dispatch setup: python -m fulfillment.cli --help."""
import argparse
import getpass
import json
import os
from sqlalchemy import select
from werkzeug.security import generate_password_hash
from fulfillment import admin, model as m
from fulfillment.domain import identifier, require
from fulfillment.service import event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "user", "disable-user", "import-legacy", "activate"])
    parser.add_argument("--operator")
    parser.add_argument("--name")
    parser.add_argument("--role", choices=["operator", "supervisor", "admin"], default="operator")
    parser.add_argument("--legacy-db", help="Used only for the optional one-time import")
    parser.add_argument("--legacy-stopped", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed import; default is preview only")
    parser.add_argument("--exclusive-operation", action="store_true", help="Attest no other picking/allocation system operates on this site's stock")
    args = parser.parse_args()
    url = os.environ.get("FULFILLMENT_DATABASE_URL")
    require(bool(url), "Set FULFILLMENT_DATABASE_URL first.")
    db = m.Database(url)
    if args.command == "init":
        db.initialize()
        print(f"Dispatch schema ready (version {m.SCHEMA_VERSION}). Existing work is preserved.")
        return
    if args.command == "import-legacy":
        from fulfillment.migration import legacy_snapshot, import_legacy
        require(bool(args.legacy_db), "Provide --legacy-db for this optional import.")
        data = legacy_snapshot(args.legacy_db)
        print(json.dumps({"rules": data["rules"], "controls": len(data["controls"]), "reservations": len(data["reservations"]), "unfinished": data["unfinished"]}, indent=2))
        if not args.apply:
            print("Preview only. Review configuration formerly supplied by environment variables separately.")
            return
        require(args.legacy_stopped, "Stop legacy picking and scheduled allocation before applying the import; use --legacy-stopped.")
        from fulfillment.erp import VisualERP
        erp = VisualERP(os.getenv("FULFILLMENT_MSSQL_CONNECTION_STRING"), os.getenv("FULFILLMENT_SITE"), db)
        with db.transaction() as conn:
            import_legacy(conn, data, erp, getpass.getuser())
        print("Imported into Dispatch. Review and publish settings in Admin; the source was not changed.")
        return
    with db.transaction() as conn:
        if args.command in ("user", "disable-user"):
            user_id = identifier(args.operator, "Operator")
            old = conn.execute(select(m.users).where(m.users.c.id == user_id)).first()
            disabling = args.command == "disable-user"
            if old and old.role == "admin" and old.enabled and (disabling or args.role != "admin"):
                require(conn.execute(select(m.users.c.id).where(m.users.c.role == "admin").where(m.users.c.enabled.is_(True)).where(m.users.c.id != user_id)).first(), "Keep at least one enabled admin.")
            if disabling:
                require(old is not None, "Operator not found.")
                require(not any(r.document["operator"] == user_id and r.state not in ("cancelled", "released") for r in conn.execute(select(m.works))), "Transfer active work before disabling the account.")
                conn.execute(m.users.update().where(m.users.c.id == user_id).values(enabled=False))
            else:
                pin = getpass.getpass("New PIN (6–32 digits): ")
                require(pin.isascii() and pin.isdigit() and 6 <= len(pin) <= 32 and pin == getpass.getpass("Repeat PIN: "), "PINs must match and contain 6–32 digits.")
                values = dict(name=args.name or user_id, pin_hash=generate_password_hash(pin), role=args.role, enabled=True)
                if old:
                    conn.execute(m.users.update().where(m.users.c.id == user_id).values(**values))
                else:
                    conn.execute(m.users.insert().values(id=user_id, **values))
            conn.execute(m.sessions.delete().where(m.sessions.c.user_id == user_id))
            event(conn, getpass.getuser(), "admin:cli-account", {"operator": user_id, "disabled": disabling, "role": args.role if not disabling else old.role})
            state = admin.load(conn)
            state["version"] += 1
            m.put_setting(conn, "administration", state)
            print("Operator updated; existing sessions revoked.")
        else:
            require(args.exclusive_operation, "Confirm exclusive warehouse operation with --exclusive-operation.")
            state = admin.load(conn)
            require(state["configured"], "Review and publish Dispatch rules before activation.")
            require(conn.execute(select(m.users.c.id).where(m.users.c.role == "admin").where(m.users.c.enabled.is_(True))).first(), "Create an enabled administrator first.")
            site = identifier(os.getenv("FULFILLMENT_SITE"), "Site")
            require(not conn.execute(select(m.works.c.id).where(m.works.c.state.notin_(["cancelled", "released"]))).first(), "Resolve active work before activation.")
            m.put_setting(conn, "cutover", dict(site=site, activated=True, at=m.now(), operator=getpass.getuser()))
            event(conn, getpass.getuser(), "admin:activate", {"site": site, "policy_version": state["version"]})
            print("Dispatch activated for exclusive operation at this site.")


if __name__ == "__main__":
    main()
