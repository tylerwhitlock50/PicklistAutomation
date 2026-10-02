"""Flask application. gunicorn target: ``picklist.app:app``."""
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import flash, Flask, jsonify, redirect, request, url_for

from picklist.config import BASE_DIR, FLASK_DEBUG, logger, READINESS_CACHE_SECONDS
from picklist.db import (
    _int_setting,
    get_config_value,
    get_sqlite_conn,
    initialize_db,
    migrate_sensitive_settings_encryption,
)
from picklist.domain import identity, notifier
from picklist.features import feature_enabled, FEATURE_FLAGS, get_feature_flags
from picklist.routes import register_blueprints
from picklist.scheduler import start_scheduler
from picklist.security import get_csrf_token
from picklist.services import readiness_service, request_service
from picklist.services.audit_service import check_audit_universe_sql
from picklist.services.notification_service import send_email_notification
from picklist.services.orders_service import (
    _picklist_orders_today,
    _readiness_blocking_holds,
    _readiness_doc_findings,
    _readiness_gate_decisions,
    _readiness_pick_status,
    fetch_order_detail_rows,
    fetch_order_part_locations,
    fetch_order_shipment_rows,
    fetch_readiness_candidate_rows,
)
from picklist.services.query_options import get_default_guns_query_options
from picklist.services.run_history import backfill_plan_snapshots
from picklist.services.settings_service import (
    get_operator_roster,
    get_readiness_config,
    get_request_sla_overrides,
)
from picklist.services.shipping_service import _active_manual_holds, invalidate_release_gate_cache
from picklist.stores import (
    allocation_store,
    audit_store,
    pick_store,
    readiness_store,
    request_store,
    shipping_store,
    verify_store,
)
from picklist.timeutil import _local_dt_filter, _today_local, resolve_timezone

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
flask_secret_key = os.getenv("FLASK_SECRET_KEY")
if not flask_secret_key:
    flask_secret_key = secrets.token_urlsafe(48)
    logger.warning(
        "FLASK_SECRET_KEY is not set; generated an ephemeral key for this process. "
        "Set FLASK_SECRET_KEY for stable sessions."
    )
app.secret_key = flask_secret_key
app.jinja_env.globals["csrf_token"] = get_csrf_token
app.add_template_filter(_local_dt_filter, "local_dt")


@app.before_request
def log_request_start() -> None:
    request.environ["request_start_time"] = time.perf_counter()


@app.after_request
def log_request_complete(response):
    if request.path.startswith("/static/"):
        return response

    started_at = request.environ.get("request_start_time")
    elapsed_ms = 0.0
    if isinstance(started_at, float):
        elapsed_ms = (time.perf_counter() - started_at) * 1000

    logger.info(
        "HTTP %s %s -> %s (%.1fms) from %s",
        request.method,
        request.path,
        response.status_code,
        elapsed_ms,
        request.remote_addr or "unknown",
    )
    return response


@app.context_processor
def inject_feature_flags():
    return {"feature_flags": get_feature_flags()}


@app.context_processor
def inject_request_badge():
    """Open-request count for the Requests tab. Best effort: a missing or
    unconfigured request store must never break a page render."""
    badge = {"open": 0, "overdue": 0}
    if feature_enabled("requests"):
        try:
            badge = request_store.open_counts()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Request badge unavailable: %s", exc)
    return {"request_badge": badge}


@app.context_processor
def inject_operator_roster():
    return {
        "operator_roster": get_operator_roster(),
        "operator_teams": [(team, identity.TEAM_LABELS[team]) for team in identity.TEAMS],
    }


@app.before_request
def enforce_feature_flags():
    for name, definition in FEATURE_FLAGS.items():
        if not request.path.startswith(definition["path_prefixes"]):
            continue
        if feature_enabled(name):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": f"{definition['label']} is currently disabled."}), 404
        flash(f"{definition['label']} is currently turned off in Settings.", "error")
        return redirect(url_for("runs.index"))
    return None


register_blueprints(app)

# Service wiring. Order matters: stores are initialised before the services that use them.
initialize_db()
migrate_sensitive_settings_encryption()
audit_store.initialize()
pick_store.initialize(get_sqlite_conn)
verify_store.initialize(get_sqlite_conn)
allocation_store.initialize(get_sqlite_conn)
shipping_store.initialize(get_sqlite_conn)
notifier.initialize(get_sqlite_conn)
notifier.configure(
    get_config_value=get_config_value,
    send_email_notification=send_email_notification,
    logger=logger,
)
identity.configure(get_roster=get_operator_roster)
readiness_store.initialize(get_sqlite_conn)
readiness_service.configure(
    fetch_candidates=fetch_readiness_candidate_rows,
    fetch_order_rows=fetch_order_detail_rows,
    fetch_order_locations=fetch_order_part_locations,
    picklist_orders_today=_picklist_orders_today,
    picklist_horizon=lambda: _today_local()
    + timedelta(days=int(get_default_guns_query_options()["lookahead_days"])),
    gate_decisions=_readiness_gate_decisions,
    manual_holds=_active_manual_holds,
    pick_status=_readiness_pick_status,
    fetch_order_shipments=fetch_order_shipment_rows,
    today=lambda: _today_local(),
    config=get_readiness_config,
    notify=notifier.send_teams_notification,
    public_url=notifier.public_url,
    cache_seconds=READINESS_CACHE_SECONDS,
    doc_findings=_readiness_doc_findings,
    logger=logger,
)
request_store.initialize(get_sqlite_conn)
request_store.configure(sla_overrides=get_request_sla_overrides())
request_service.configure(
    add_exception=lambda order_id, reason, actor, expires_at: shipping_store.add_exception(
        cust_order_id=order_id, reason=reason, created_by=actor, expires_at=expires_at
    ),
    revoke_exception=lambda exception_id, actor: shipping_store.revoke_exception(exception_id, actor),
    invalidate_gate_cache=invalidate_release_gate_cache,
    refresh_readiness=lambda: threading.Thread(
        target=readiness_service.refresh, kwargs={"trigger": "request", "force": True},
        name="readiness-after-request", daemon=True,
    ).start(),
    blocking_holds=_readiness_blocking_holds,
    notify=notifier.send_teams_notification,
    public_url=notifier.public_url,
    now=lambda: datetime.now(resolve_timezone()),
    expedite_max_hours=_int_setting("request_expedite_max_hours", "REQUEST_EXPEDITE_MAX_HOURS", 72),
    logger=logger,
)
backfill_plan_snapshots()
check_audit_universe_sql()
start_scheduler()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=FLASK_DEBUG,
        use_reloader=False,
    )
