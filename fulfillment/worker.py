"""Independent queued-reservation maintenance. No ERP writes or legacy process."""
import argparse
import logging
import os
import time
from fulfillment import admin, model as m
from fulfillment.domain import require
from fulfillment.erp import VisualERP, DemoERP
from fulfillment.service import Service


def tick(service, site, mode):
    with service.db.transaction() as conn:
        if mode == "shadow" or not admin.load(conn)["configured"]:
            return False
        if mode == "live":
            receipt = m.get_setting(conn, "cutover", {})
            require(receipt.get("activated") and receipt.get("site") == site, "Worker requires exclusive Dispatch activation.")
        service.reconcile_allocations(conn, "dispatch-worker")
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    require(15 <= args.interval <= 3600, "Worker interval must be 15–3600 seconds.")
    mode = os.getenv("FULFILLMENT_MODE", "shadow")
    require(mode in ("demo", "shadow", "live"), "Invalid Dispatch mode.")
    url = os.getenv("FULFILLMENT_DATABASE_URL", "")
    require(url and (mode == "demo" or url.startswith("postgresql")), "Live workers require the Dispatch PostgreSQL database.")
    db = m.Database(url)
    site = os.getenv("FULFILLMENT_SITE", "").upper()
    erp = DemoERP() if mode == "demo" else VisualERP(os.getenv("FULFILLMENT_MSSQL_CONNECTION_STRING"), site, db)
    service = Service(db, erp, live=mode == "live")
    while True:
        try:
            tick(service, site, mode)
        except Exception:
            logging.exception("Dispatch allocation refresh failed; existing reservations retained")
            if args.once:
                raise
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
