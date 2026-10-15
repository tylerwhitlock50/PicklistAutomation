"""Dispatch identity, transaction and ERP boundary for the cloned audit workflow."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
import logging
from pathlib import Path
import re
import secrets
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from flask import current_app, g, make_response, request
from sqlalchemy import select, text

from fulfillment import admin, model as m
from fulfillment.domain import require
from fulfillment.service import event

logger = logging.getLogger("dispatch.audit")
UI_REFRESH_INTERVAL_SECONDS = 30
SQL = Path(__file__).resolve().parents[1] / "sql"
AUDIT_QUERY_FILE = SQL / "audit_serialized_inventory.sql"
AUDIT_LOCATIONS_SYNC_FILE = SQL / "audit_locations_sync.sql"
AUDIT_DWELL_QUERY_FILE = SQL / "audit_dwell_time.sql"
DEFAULT_SETTINGS = {"version": 1, "accuracy_target_pct": 99, "dwell_target_hours": 48,
    "sync_minutes": 15, "dwell_locations": ["MAIN/C2-SERIALIZED", "SHIPPING/STAGE", "SHIPPING/STAGE1", "SHIPPING/STAGE2", "SHIPPING/STAGE3"]}


def database():
    return current_app.extensions["fulfillment_db"]


def available():
    return database().engine.dialect.name == "postgresql"


class RequestEngine:
    @contextmanager
    def connect(self):
        if getattr(g, "audit_conn", None) is not None:
            yield g.audit_conn
        else:
            with database().engine.connect() as conn:
                yield conn

    @contextmanager
    def begin(self):
        # Every route mutation shares the outer transaction, including completion
        # rollups, resolution events and idempotency receipt.
        if getattr(g, "audit_conn", None) is not None:
            yield g.audit_conn
        else:
            with database().transaction() as conn:
                yield conn


def audit_engine():
    return RequestEngine()


def settings():
    with audit_engine().connect() as conn:
        return m.get_setting(conn, "audit_configuration", DEFAULT_SETTINGS)


def resolve_timezone():
    with audit_engine().connect() as conn:
        return ZoneInfo(admin.load(conn)["rules"]["timezone"])


def _audit_dt_display(value):
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(resolve_timezone()).strftime("%Y-%m-%d %H:%M")


def _audit_json_safe(value):
    if isinstance(value, dict):
        return {k: _audit_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def flash(message, category="message"):
    g.audit_messages = getattr(g, "audit_messages", []) + [(category, message)]
    if category == "error":
        g.audit_failed = True


def validate_barcode(value, label):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/ -]{1,120}", value),
            f"{label} must be a nonempty barcode (letters, digits, spaces, . _ : / -; maximum 120 characters).", "invalid", 400)


def validate_snapshot(rows):
    seen = set()
    for row in rows:
        serial = str(row.get("SERIAL_NO") or "").strip().upper()
        validate_barcode(serial, "ERP serial")
        validate_barcode(str(row.get("EXPECTED_LOCATION") or ""), "ERP expected location")
        require(serial not in seen, "ERP has an ambiguous serial identity or location. Resolve it before starting the audit.", "source", 409)
        seen.add(serial)


def get_erp_engine():
    return current_app.extensions["fulfillment"].erp.engine


def demo_rows():
    return [dict(SERIAL_NO=s, PART_ID="DEMO-SERIALIZED", PART_DESCRIPTION="Training serialized item", PRODUCT_CODE="DEMO",
                 EXPECTED_WAREHOUSE=wh, EXPECTED_LOCATION=loc, SCOPE=scope, TIED_WO=int(scope == "TIED-WO"),
                 CUST_ORDER_ID="SO-DEMO-100" if scope == "TIED-WO" else None, CUSTOMER_ID="DEMO-SUPPLY")
            for s, wh, loc, scope in [("DEMO-001", "SHIPPING", "R09S02", "SHIPPING"),
                ("DEMO-002", "SHIPPING", "R09S02", "SHIPPING"), ("DEMO-003", "MAIN", "C2", "C2"),
                ("DEMO-TIED", None, "SO-DEMO-100", "TIED-WO")]]


def demo_onhand(serials):
    # Synthetic recheck evidence is deliberately older than the audit snapshot.
    stamp = datetime.now(resolve_timezone()).replace(tzinfo=None) - timedelta(days=1)
    return {r["SERIAL_NO"]: [dict(warehouse=r["EXPECTED_WAREHOUSE"], location=r["EXPECTED_LOCATION"], qty=1, last_txn_at=stamp)]
            for r in demo_rows() if r["SERIAL_NO"] in serials and r["EXPECTED_WAREHOUSE"]}


def run_audit_sql_file(path, label):
    if current_app.config["MODE"] == "demo":
        if path == AUDIT_QUERY_FILE:
            return pd.DataFrame(demo_rows())
        if path == AUDIT_LOCATIONS_SYNC_FILE:
            return pd.DataFrame([dict(WAREHOUSE_ID="SHIPPING", LOCATION_ID="R09S02", DESCRIPTION="Training rack", SERIAL_COUNT=2),
                                 dict(WAREHOUSE_ID="MAIN", LOCATION_ID="C2", DESCRIPTION="Training cage", SERIAL_COUNT=1)])
        stamp = datetime.now().replace(microsecond=0)
        return pd.DataFrame([dict(SERIAL_NO="DEMO-STAGE", PART_ID="DEMO-SERIALIZED", PART_DESCRIPTION="Training staged item",
            WAREHOUSE_ID="SHIPPING", LOCATION_ID="STAGE", ARRIVED_AT=stamp-timedelta(hours=60), ERP_NOW=stamp)])
    with get_erp_engine().begin() as conn:
        rows = conn.execute(text(path.read_text(encoding="utf-8")), {"site": current_app.config["SITE"]}).mappings().fetchmany(100001)
    require(len(rows) <= 100000, "Audit source exceeds 100,000 rows. Narrow the operating site.", "source", 503)
    if rows:
        return pd.DataFrame([dict(r) for r in rows])
    columns = (["ARRIVED_AT", "ERP_NOW", "WAREHOUSE_ID", "LOCATION_ID", "SERIAL_NO", "PART_ID", "PART_DESCRIPTION"]
               if path == AUDIT_DWELL_QUERY_FILE else [])
    return pd.DataFrame(columns=columns)


def require_csrf(fn):
    return fn  # The Dispatch authentication decorator validates header/form CSRF.


def require_trusted_client(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        authenticated = current_app.extensions["audit_authenticate"]

        @authenticated
        def run():
            g.audit_nonce = secrets.token_urlsafe(24)
            require(available(), "Inventory audits require Dispatch PostgreSQL. The SQLite picking demo does not include the audit store.", "audit_unavailable", 503)
            with database().transaction() as conn:
                g.audit_conn = conn
                try:
                    actor = conn.execute(select(m.users).where(m.users.c.id == g.actor["id"])).first()
                    require(actor and actor.enabled, "Sign in to continue.", "unauthorized", 401)
                    g.actor = {"id": actor.id, "name": actor.name, "role": actor.role}
                    mutation = request.method == "POST"
                    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
                    if mutation and request.is_json:
                        require(isinstance(payload, dict), "A JSON object is required.", "invalid", 400)
                    payload = payload or {}
                    if mutation:
                        token = request.headers.get("Idempotency-Key") or payload.get("request_key")
                        require(isinstance(token, str) and 1 <= len(token) <= 100, "A request key is required.", "idempotency", 400)
                        reqkey = "audit:" + m.digest([g.actor["id"], request.path, token])
                        fingerprint = m.digest(payload)
                        previous = conn.execute(select(m.requests).where(m.requests.c.key == reqkey)).first()
                        if previous:
                            require(previous.digest == fingerprint, "Request key was reused with different contents.", "idempotency", 409)
                            saved = previous.response
                            return make_response(saved["body"], previous.status, saved["headers"])
                    savepoint = conn.begin_nested()
                    response = make_response(fn(*args, **kwargs))
                    if response.status_code >= 400 or getattr(g, "audit_failed", False):
                        savepoint.rollback()
                        if getattr(g, "audit_failed", False) and response.status_code < 400:
                            return {"error": "audit_failed", "message": " ".join(v for _, v in g.audit_messages)}, 409
                    else:
                        savepoint.commit()
                        if mutation:
                            event(conn, g.actor["id"], "audit:" + fn.__name__, {"path": request.path, "request": {k: v for k, v in payload.items() if k not in ("csrf_token", "request_key", "operator")}})
                            saved = {"body": response.get_data(as_text=True), "headers": {k: v for k, v in response.headers.items() if k in ("Content-Type", "Location")}}
                            conn.execute(m.requests.insert().values(key=reqkey, digest=fingerprint, response=saved, status=response.status_code))
                    return response
                finally:
                    g.pop("audit_conn", None)
        return run()
    return wrapped


def register(app, authenticated):
    from fulfillment.audit.routes import bp
    app.extensions["audit_authenticate"] = authenticated
    app.register_blueprint(bp)
    app.jinja_env.filters["local_dt"] = _audit_dt_display

    @app.context_processor
    def audit_context():
        return {"csrf_token": lambda: g.csrf, "audit_request_key": lambda: str(uuid4()),
                "audit_messages": lambda: getattr(g, "audit_messages", []), "audit_mode": app.config["MODE"]}
