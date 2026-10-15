"""Dedicated JSON API and production PWA host. Start with `flask --app fulfillment.app:create_app run`."""
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import os
from pathlib import Path
import secrets

from flask import Flask, g, jsonify, request, send_from_directory
from sqlalchemy import select
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash

from fulfillment import model as m
from fulfillment.domain import Conflict, identifier, reconcile, require
from fulfillment.erp import DemoERP, VisualERP
from fulfillment.service import Service, event

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ("pick", "finish-pick", "handoff", "package", "pack", "reset-package", "remove-package",
           "verify", "label", "release", "exception", "resolve", "transfer", "substitute", "cancel", "putaway")


def create_app(config=None, erp=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(MODE=os.getenv("FULFILLMENT_MODE", "shadow"),
                      DATABASE_URL=os.getenv("FULFILLMENT_DATABASE_URL", ""),
                      SITE=os.getenv("FULFILLMENT_SITE", ""),
                      MAX_CONTENT_LENGTH=128 * 1024,
                      LABEL_TOKEN=os.getenv("FULFILLMENT_LABEL_TOKEN", ""),
                      VALIDATE_TOKEN=os.getenv("FULFILLMENT_VALIDATE_TOKEN", ""),
                      COOKIE_SECURE=True,
                      DIST=str(ROOT / "operator-web" / "dist"))
    if config:
        app.config.update(config)
    mode = app.config["MODE"]
    require(mode in ("demo", "shadow", "live"), "FULFILLMENT_MODE must be demo, shadow or live.")
    if mode == "demo":
        app.config["COOKIE_SECURE"] = False
        app.config["DATABASE_URL"] = app.config["DATABASE_URL"] or "sqlite:///data/fulfillment-demo.db"
    require(bool(app.config["DATABASE_URL"]), "Set FULFILLMENT_DATABASE_URL to a dedicated PostgreSQL database.")
    require(mode == "demo" or app.config.get("TESTING") or app.config["DATABASE_URL"].startswith("postgresql"),
            "Live and shadow services require PostgreSQL.")
    database = m.Database(app.config["DATABASE_URL"])
    if erp is None:
        erp = DemoERP() if mode == "demo" else VisualERP(os.getenv("FULFILLMENT_MSSQL_CONNECTION_STRING"),
                   app.config["SITE"], os.getenv("FULFILLMENT_LEGACY_DB"), timezone_name=os.getenv("SCHEDULE_TIMEZONE", "America/Denver"))
    service = Service(database, erp, live=mode == "live")
    app.extensions.update(fulfillment=service, fulfillment_db=database)

    def authenticated(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.cookies.get("fulfillment_session", "")
            with database.engine.connect() as conn:
                row = conn.execute(select(m.sessions.c.user_id, m.users.c.name, m.users.c.role)
                    .join(m.users, m.users.c.id == m.sessions.c.user_id)
                    .where(m.sessions.c.token_hash == m.digest(token)).where(m.sessions.c.expires > m.now())
                    .where(m.users.c.enabled.is_(True))).first()
            require(row is not None, "Sign in to continue.", "unauthorized", 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                require(hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), m.digest([token, "csrf"])), "Session validation failed. Reload and try again.", "csrf", 403)
            g.actor = {"id": row.user_id, "name": row.name, "role": row.role}
            g.csrf = m.digest([token, "csrf"])
            return fn(*args, **kwargs)
        return wrapper

    def machine(scope):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                expected = app.config[scope + "_TOKEN"]
                presented = request.headers.get("Authorization", "").removeprefix("Bearer ")
                require(len(expected) >= 32 and hmac.compare_digest(expected, presented), "Integration authentication required.", "unauthorized", 401)
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    @app.before_request
    def boundary():
        origin = request.headers.get("Origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            expected = os.getenv("FULFILLMENT_PUBLIC_ORIGIN", request.host_url.rstrip("/"))
            require(origin == expected, "Cross-origin requests are not allowed.", "origin", 403)
        if mode == "live" and request.path.startswith("/api/v1") and request.method == "POST" and "/auth/" not in request.path:
            with database.engine.connect() as conn:
                receipt = m.get_setting(conn, "cutover")
            require(receipt and receipt.get("site") == app.config["SITE"] and receipt.get("activated"), "Live checkout requires the documented cutover procedure.", "cutover", 503)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if request.path.startswith("/api/") or request.path in ("/", "/sw.js", "/index.html"):
            response.headers["Cache-Control"] = "no-store"
        if mode != "demo":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(Conflict)
    def conflict(exc):
        extra = {"result": "unavailable"} if request.path.endswith("/visual/validate") else {}
        return jsonify(error=exc.code, message=exc.message, details=exc.details, **extra), exc.status

    @app.errorhandler(Exception)
    def failure(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error="http_error", message=exc.description), exc.code
        app.logger.exception("Fulfillment request failed")
        extra = {"result": "unavailable"} if request.path.endswith("/visual/validate") else {}
        return jsonify(error="unavailable", message="The data source is unavailable. Retry the same request to recover its result.", **extra), 503

    @app.get("/health")
    def health():
        with database.engine.connect() as conn:
            require(conn.execute(select(m.schema.c.version)).scalar() == 1, "Database migration required.", "schema", 503)
        return jsonify(status="ok", mode=mode)

    @app.post("/api/v1/auth/login")
    def login():
        payload = request.get_json()
        require(isinstance(payload, dict), "A JSON object is required.", "invalid", 400)
        username = identifier(payload.get("operator"), "Operator")
        pin = str(payload.get("pin", ""))
        require(4 <= len(pin) <= 32, "Sign-in failed.", "unauthorized", 401)
        token = None
        with database.transaction() as conn:
            # Per-account and per-source budgets prevent username cycling.
            budget_keys = ["user:" + username, "ip:" + (request.remote_addr or "unknown")]
            for budget in budget_keys:
                attempt = conn.execute(select(m.attempts).where(m.attempts.c.key == budget)).first()
                active = attempt and datetime.fromisoformat(attempt.window) > datetime.now(timezone.utc) - timedelta(minutes=15)
                require(not active or attempt.count < (5 if budget.startswith("user:") else 30), "Too many sign-in attempts. Try again in 15 minutes.", "rate_limited", 429)
                conn.execute(m.attempts.delete().where(m.attempts.c.key == budget))
                conn.execute(m.attempts.insert().values(key=budget, count=attempt.count + 1 if active else 1, window=attempt.window if active else m.now()))
            user = conn.execute(select(m.users).where(m.users.c.id == username).where(m.users.c.enabled.is_(True))).first()
            if user and check_password_hash(user.pin_hash, pin):
                token = secrets.token_urlsafe(32)
                conn.execute(m.sessions.insert().values(token_hash=m.digest(token), user_id=username, expires=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()))
                conn.execute(m.attempts.delete().where(m.attempts.c.key == "user:" + username))
            event(conn, username, "login" if token else "login_failed", {})
        require(token is not None, "Sign-in failed.", "unauthorized", 401)
        response = jsonify(ok=True)
        response.set_cookie("fulfillment_session", token, httponly=True, secure=app.config["COOKIE_SECURE"], samesite="Strict", max_age=43200)
        return response

    @app.get("/api/v1/auth/me")
    @authenticated
    def me():
        return jsonify(operator=g.actor, csrf=g.csrf, mode=mode)

    @app.post("/api/v1/auth/logout")
    @authenticated
    def logout():
        with database.transaction() as conn:
            conn.execute(m.sessions.delete().where(m.sessions.c.token_hash == m.digest(request.cookies.get("fulfillment_session", ""))))
        response = jsonify(ok=True)
        response.delete_cookie("fulfillment_session")
        return response

    @app.get("/api/v1/orders")
    @authenticated
    def orders():
        with database.engine.connect() as conn:
            return jsonify(service.orders(conn))

    @app.get("/api/v1/fulfillments")
    @authenticated
    def works():
        with database.engine.connect() as conn:
            rows = service.list_work(conn)
        return jsonify(fulfillments=rows)

    @app.get("/api/v1/fulfillments/<work_id>")
    @app.get("/api/v1/fulfillments/<work_id>/manifest")
    @authenticated
    def detail(work_id):
        with database.engine.connect() as conn:
            work = service.get(conn, work_id)
            work["events"] = [dict(r) for r in conn.execute(select(m.events).where(m.events.c.work_id == work_id).order_by(m.events.c.at)).mappings()]
        return jsonify(work)

    def mutation(action, work_id=None):
        require(mode != "shadow", "Shadow mode is read-only. Use training mode to rehearse or complete cutover to go live.", "shadow", 403)
        result, status = service.command(g.actor, action, request.get_json(), request.headers.get("Idempotency-Key"), work_id)
        return jsonify(result), status

    @app.post("/api/v1/checkouts")
    @authenticated
    def checkout():
        return mutation("checkout")

    @app.post("/api/v1/fulfillments/<work_id>/<action>")
    @authenticated
    def action(work_id, action):
        require(action in ACTIONS, "Unknown action.", "not_found", 404)
        return mutation(action, work_id)

    @app.get("/api/v1/integrations/visual/fulfillments/<work_id>/manifest")
    @machine("VALIDATE")
    def integration_manifest(work_id):
        with database.engine.connect() as conn:
            return jsonify(service.get(conn, work_id))

    @app.post("/api/v1/integrations/visual/validate")
    @machine("VALIDATE")
    def validate():
        payload = request.get_json()
        require(isinstance(payload, dict), "A JSON object is required.", "invalid", 400)
        with database.engine.connect() as conn:
            work = service.get(conn, payload.get("fulfillment_id"))
        require(work["version"] == payload.get("version"), "Manifest revision changed.", "revision")
        candidate = payload.get("candidate")
        require(candidate is None or isinstance(candidate, dict), "candidate must be a document.", "invalid", 400)
        document = candidate if candidate is not None else erp.packlist(identifier(payload.get("packlist")), work)
        result = reconcile(work, document)
        return jsonify(**result, advisory=candidate is not None, version=work["version"])

    @app.post("/api/v1/integrations/worldship/labels")
    @machine("LABEL")
    def ingest():
        result, status = service.command({"id": "worldship"}, "labels", request.get_json(), request.headers.get("Idempotency-Key"))
        return jsonify(result), status

    if mode == "demo":
        @app.post("/api/v1/demo/<work_id>/documents")
        @authenticated
        def demo_documents(work_id):
            with database.transaction() as conn:
                work = service.get(conn, work_id)
                packlist = "PL-DEMO-" + work_id[:8].upper()
                erp.packlists[packlist] = dict(id=packlist, site=work["site"], order=work["order"], status="A", destination=work["destination"],
                    lines=[dict(packlist_line=str(i + 1), order=work["order"], order_line=l["order_line"], part=l["part"], uom=l["uom"], quantity=l["quantity"], serials=l["serials"]) for i, l in enumerate(work["lines"])])
                result = []
                for i, package in enumerate(work["packages"]):
                    tracking = "DEMO-TRACK-" + work_id[:8].upper() + "-" + str(i + 1)
                    service.ingest_label(conn, dict(tracking=tracking, site=work["site"], order=work["order"], packlist=packlist,
                        package=package["id"], destination=work["destination"], void=False, exported_at=m.now()))
                    result.append(dict(package=package["id"], tracking=tracking))
            return jsonify(packlist=packlist, labels=result)

    @app.get("/api/v1/openapi.json")
    def openapi():
        from fulfillment.openapi import specification
        return jsonify(specification())

    @app.get("/")
    @app.get("/<path:path>")
    def frontend(path="index.html"):
        require(not path.startswith("api/"), "Endpoint not found.", "not_found", 404)
        return send_from_directory(app.config["DIST"], path)

    return app
