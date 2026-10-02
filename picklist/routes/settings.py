"""Settings page and connection test endpoints."""
import json
import re
import secrets

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from picklist.config import logger, SCHEDULE_TIME, SETTINGS_PASSWORD, SETTINGS_SESSION_KEY
from picklist.db import delete_setting, get_config_source, get_config_value, set_setting
from picklist.domain import identity, notifier
from picklist.features import FEATURE_FLAGS
from picklist.scheduler import get_next_scheduled_run
from picklist.security import (
    require_csrf,
    require_trusted_client,
    settings_access_granted,
    validate_csrf,
)
from picklist.services.notification_service import (
    parse_recipient_addresses,
    recipients_are_valid,
    send_email_notification_with_config,
    send_telegram_notification_with_credentials,
)
from picklist.services.run_history import get_reporting_metrics
from picklist.services.settings_service import (
    ensure_release_gate_policy_version,
    get_excess_packlist_cost,
    get_max_runs_per_day,
    get_operator_roster,
    get_release_gate_customer_policies,
    get_release_gate_due_override_days,
    get_release_gate_mode,
    get_teams_digest_time,
    parse_release_gate_customer_policies,
    validate_optional_chat_id,
    validate_optional_email,
    validate_optional_port,
    validate_optional_recipient_list,
)
from picklist.services.shipping_service import _release_gate_cache
from picklist.timeutil import build_time_diagnostics, format_schedule_time, parse_schedule_time
from picklist.util import mask_connection_string, parse_bool

bp = Blueprint("settings", __name__)


@bp.route("/settings", methods=["GET", "POST"])
@require_trusted_client
def settings():
    reporting_metrics = get_reporting_metrics()

    if request.method == "POST":
        validate_csrf()
        action = (request.form.get("action") or "save").strip().lower()

        if action == "unlock":
            if not SETTINGS_PASSWORD:
                logger.error("SETTINGS_PASSWORD is not configured.")
                flash("SETTINGS_PASSWORD is not configured on this server.", "error")
                return redirect(url_for("settings.settings"))
            submitted_password = request.form.get("settings_password") or ""
            if secrets.compare_digest(submitted_password, SETTINGS_PASSWORD):
                session[SETTINGS_SESSION_KEY] = True
                logger.info("Settings unlocked for client %s.", request.remote_addr)
                flash("Settings unlocked.", "success")
            else:
                logger.warning("Failed settings unlock attempt from %s.", request.remote_addr)
                flash("Invalid settings password.", "error")
            return redirect(url_for("settings.settings"))

        if action == "logout":
            session.pop(SETTINGS_SESSION_KEY, None)
            logger.info("Settings locked for client %s.", request.remote_addr)
            flash("Settings locked.", "success")
            return redirect(url_for("settings.settings"))

        if not settings_access_granted():
            logger.warning(
                "Blocked settings save attempt without unlock from %s.",
                request.remote_addr,
            )
            flash("Unlock settings before saving changes.", "error")
            return redirect(url_for("settings.settings"))

        telegram_chat_id = (request.form.get("telegram_chat_id") or "").strip()
        smtp_port = (request.form.get("smtp_port") or "").strip()
        smtp_sender = (request.form.get("smtp_sender") or "").strip()
        smtp_recipient = (request.form.get("smtp_recipient") or "").strip()

        validation_error = (
            validate_optional_chat_id(telegram_chat_id)
            or validate_optional_port(smtp_port)
            or validate_optional_email(smtp_sender, "SMTP sender")
            or validate_optional_recipient_list(smtp_recipient)
        )
        if validation_error:
            flash(validation_error, "error")
            return redirect(url_for("settings.settings"))

        mssql_connection_string = (request.form.get("mssql_connection_string") or "").strip()
        if request.form.get("clear_mssql_connection_string"):
            delete_setting("mssql_connection_string")
        elif mssql_connection_string:
            set_setting("mssql_connection_string", mssql_connection_string)

        mssql_write_connection_string = (
            request.form.get("mssql_write_connection_string") or ""
        ).strip()
        if request.form.get("clear_mssql_write_connection_string"):
            delete_setting("mssql_write_connection_string")
        elif mssql_write_connection_string:
            set_setting("mssql_write_connection_string", mssql_write_connection_string)

        telegram_bot_token = (request.form.get("telegram_bot_token") or "").strip()
        if request.form.get("clear_telegram_bot_token"):
            delete_setting("telegram_bot_token")
        elif telegram_bot_token:
            set_setting("telegram_bot_token", telegram_bot_token)

        if telegram_chat_id:
            set_setting("telegram_chat_id", telegram_chat_id)
        else:
            delete_setting("telegram_chat_id")

        smtp_host = (request.form.get("smtp_host") or "").strip()
        if smtp_host:
            set_setting("smtp_host", smtp_host)
        else:
            delete_setting("smtp_host")

        if smtp_port:
            set_setting("smtp_port", smtp_port)
        else:
            delete_setting("smtp_port")

        smtp_user = (request.form.get("smtp_user") or "").strip()
        if smtp_user:
            set_setting("smtp_user", smtp_user)
        else:
            delete_setting("smtp_user")

        smtp_password = (request.form.get("smtp_password") or "").strip()
        if request.form.get("clear_smtp_password"):
            delete_setting("smtp_password")
        elif smtp_password:
            set_setting("smtp_password", smtp_password)

        if smtp_sender:
            set_setting("smtp_sender", smtp_sender)
        else:
            delete_setting("smtp_sender")

        if smtp_recipient:
            set_setting("smtp_recipient", smtp_recipient)
        else:
            delete_setting("smtp_recipient")

        teams_webhook_url = (request.form.get("teams_webhook_url") or "").strip()
        if request.form.get("clear_teams_webhook_url"):
            delete_setting("teams_webhook_url")
        elif teams_webhook_url:
            if not teams_webhook_url.lower().startswith("https://"):
                flash("Teams webhook URL must start with https://.", "error")
                return redirect(url_for("settings.settings"))
            set_setting("teams_webhook_url", teams_webhook_url)

        selected_events = [
            name for name in notifier.TEAMS_EVENTS if request.form.get(f"teams_event_{name}")
        ]
        if len(selected_events) == len(notifier.TEAMS_EVENTS):
            set_setting("teams_enabled_events", "all")
        elif selected_events:
            set_setting("teams_enabled_events", ",".join(selected_events))
        else:
            set_setting("teams_enabled_events", "none")

        teams_digest_time = (request.form.get("teams_digest_time") or "").strip()
        if teams_digest_time:
            try:
                parse_schedule_time(teams_digest_time)
            except ValueError:
                flash("Teams digest time must be HH:MM (24-hour).", "error")
                return redirect(url_for("settings.settings"))
            set_setting("teams_digest_time", teams_digest_time)
        else:
            delete_setting("teams_digest_time")

        app_public_url = (request.form.get("app_public_url") or "").strip().rstrip("/")
        if app_public_url:
            if not re.match(r"^https?://", app_public_url, re.IGNORECASE):
                flash("App public URL must start with http:// or https://.", "error")
                return redirect(url_for("settings.settings"))
            set_setting("app_public_url", app_public_url)
        else:
            delete_setting("app_public_url")

        roster_raw = (request.form.get("operator_roster_json") or "").strip()
        try:
            roster = identity.parse_roster(roster_raw)
        except ValueError as exc:
            flash(f"Operator roster: {exc}", "error")
            return redirect(url_for("settings.settings"))
        if roster:
            set_setting("operator_roster_json", json.dumps(roster, separators=(",", ":")))
        else:
            delete_setting("operator_roster_json")

        max_runs_per_day = (request.form.get("max_runs_per_day") or "").strip()
        if max_runs_per_day:
            try:
                max_runs_value = int(max_runs_per_day)
                if max_runs_value < 0:
                    raise ValueError
            except ValueError:
                flash("Max runs per day must be a whole number (0 = unlimited).", "error")
                return redirect(url_for("settings.settings"))
            set_setting("max_runs_per_day", str(max_runs_value))
        else:
            delete_setting("max_runs_per_day")

        excess_cost = (request.form.get("excess_packlist_cost_usd") or "").strip()
        if excess_cost:
            try:
                excess_cost_value = float(excess_cost)
                if excess_cost_value < 0:
                    raise ValueError
            except ValueError:
                flash("Excess packlist cost must be a dollar amount of 0 or more.", "error")
                return redirect(url_for("settings.settings"))
            set_setting("excess_packlist_cost_usd", f"{excess_cost_value:g}")
        else:
            delete_setting("excess_packlist_cost_usd")

        release_gate_mode = (request.form.get("release_gate_mode") or "advisory").strip().lower()
        if release_gate_mode not in {"off", "advisory", "enforced"}:
            flash("Release gate mode must be off, advisory, or enforced.", "error")
            return redirect(url_for("settings.settings"))
        due_days_raw = (
            request.form.get("release_gate_due_override_days") or "1"
        ).strip()
        try:
            due_days = int(due_days_raw)
            if due_days < 0 or due_days > 30:
                raise ValueError
        except ValueError:
            flash("Commitment-protection days must be a whole number from 0 to 30.", "error")
            return redirect(url_for("settings.settings"))
        policies_raw = (
            request.form.get("release_gate_customer_policies_json") or "{}"
        ).strip()
        try:
            policies = parse_release_gate_customer_policies(policies_raw)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings.settings"))
        set_setting("release_gate_mode", release_gate_mode)
        set_setting("release_gate_due_override_days", str(due_days))
        set_setting(
            "release_gate_customer_policies_json",
            json.dumps(policies, sort_keys=True),
        )
        ensure_release_gate_policy_version(
            changed_by=f"settings:{request.remote_addr or 'unknown'}"
        )
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )

        set_setting("smtp_use_tls", "true" if request.form.get("smtp_use_tls") else "false")

        for feature_name, feature_def in FEATURE_FLAGS.items():
            set_setting(
                feature_def["setting_key"],
                "true" if request.form.get(f"feature_{feature_name}") else "false",
            )

        logger.info("Settings updated by client %s.", request.remote_addr)
        flash("Settings saved. Values entered here override .env values.", "success")
        return redirect(url_for("settings.settings"))

    next_run = get_next_scheduled_run()
    time_diagnostics = build_time_diagnostics(next_run)
    schedule_time_display = format_schedule_time(SCHEDULE_TIME)

    if not settings_access_granted():
        return render_template(
            "settings.html",
            settings_unlocked=False,
            time_diagnostics=time_diagnostics,
            schedule_time_display=schedule_time_display,
            reporting_metrics=reporting_metrics,
        )

    mssql_value = get_config_value("mssql_connection_string", "MSSQL_CONNECTION_STRING")
    mssql_write_value = get_config_value(
        "mssql_write_connection_string", "MSSQL_WRITE_CONNECTION_STRING"
    )
    telegram_chat_id = get_config_value("telegram_chat_id", "TELEGRAM_CHAT_ID", "")
    smtp_host = get_config_value("smtp_host", "SMTP_HOST", "")
    smtp_port = get_config_value("smtp_port", "SMTP_PORT", "587")
    smtp_user = get_config_value("smtp_user", "SMTP_USER", "")
    smtp_sender = get_config_value("smtp_sender", "SMTP_SENDER", "")
    smtp_recipient = get_config_value("smtp_recipient", "SMTP_RECIPIENT", "")
    smtp_use_tls = parse_bool(
        get_config_value("smtp_use_tls", "SMTP_USE_TLS", default="true"),
        default=True,
    )
    release_policies = get_release_gate_customer_policies()

    return render_template(
        "settings.html",
        settings_unlocked=True,
        time_diagnostics=time_diagnostics,
        schedule_time_display=schedule_time_display,
        reporting_metrics=reporting_metrics,
        mssql_connection_string_masked=mask_connection_string(mssql_value)
        if mssql_value
        else "Not configured",
        mssql_source=get_config_source("mssql_connection_string", "MSSQL_CONNECTION_STRING"),
        mssql_write_connection_string_masked=mask_connection_string(mssql_write_value)
        if mssql_write_value
        else "Not configured (saves use the main connection)",
        mssql_write_source=get_config_source(
            "mssql_write_connection_string", "MSSQL_WRITE_CONNECTION_STRING"
        ),
        telegram_bot_token_configured=bool(
            get_config_value("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
        ),
        telegram_bot_source=get_config_source("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=telegram_chat_id,
        telegram_chat_source=get_config_source("telegram_chat_id", "TELEGRAM_CHAT_ID"),
        smtp_password_configured=bool(get_config_value("smtp_password", "SMTP_PASSWORD")),
        smtp_password_source=get_config_source("smtp_password", "SMTP_PASSWORD"),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_sender=smtp_sender,
        smtp_recipient=smtp_recipient,
        smtp_use_tls=smtp_use_tls,
        max_runs_per_day=get_max_runs_per_day(),
        max_runs_source=get_config_source("max_runs_per_day", "MAX_RUNS_PER_DAY"),
        excess_packlist_cost_usd=get_excess_packlist_cost(),
        excess_cost_source=get_config_source(
            "excess_packlist_cost_usd", "EXCESS_PACKLIST_COST_USD"
        ),
        release_gate_mode=get_release_gate_mode(),
        release_gate_due_override_days=get_release_gate_due_override_days(),
        release_gate_customer_policies_json=json.dumps(
            release_policies, indent=2, sort_keys=True
        ),
        release_gate_customer_policies=release_policies,
        teams_webhook_configured=bool(get_config_value("teams_webhook_url", "TEAMS_WEBHOOK_URL")),
        teams_webhook_source=get_config_source("teams_webhook_url", "TEAMS_WEBHOOK_URL"),
        teams_events=list(notifier.TEAMS_EVENTS.items()),
        teams_enabled_events=notifier.enabled_events(),
        teams_digest_time=get_teams_digest_time(),
        app_public_url=get_config_value("app_public_url", "APP_PUBLIC_URL", "") or "",
        operator_roster_json=json.dumps(get_operator_roster(), separators=(",", ":")),
    )


@bp.get("/api/me")
@require_trusted_client
def api_me():
    operator = identity.current_operator()
    return jsonify(
        {
            "operator": operator.as_dict() if operator else None,
            "roster_size": len(get_operator_roster()),
            "teams": [{"key": team, "label": identity.TEAM_LABELS[team]} for team in identity.TEAMS],
        }
    )


@bp.post("/api/settings/test-teams")
@require_trusted_client
@require_csrf
def api_test_teams_settings():
    if not settings_access_granted():
        return jsonify({"ok": False, "message": "Unlock settings before testing."}), 403
    payload = request.get_json(silent=True) or {}
    webhook = (payload.get("webhook_url") or "").strip()
    ok, message = notifier.send_test(webhook)
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400


@bp.post("/api/settings/test-telegram")
@require_trusted_client
@require_csrf
def api_test_telegram_settings():
    if not settings_access_granted():
        return jsonify({"ok": False, "message": "Unlock settings before testing."}), 403

    payload = request.get_json(silent=True) or {}
    bot_token = (payload.get("bot_token") or "").strip()
    chat_id = (payload.get("chat_id") or "").strip()
    if not bot_token:
        bot_token = get_config_value("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "") or ""
    if not chat_id:
        chat_id = get_config_value("telegram_chat_id", "TELEGRAM_CHAT_ID", "") or ""

    if not re.fullmatch(r"-?\d+", chat_id):
        return jsonify({"ok": False, "message": "Chat ID must be numeric (optional leading -)."}), 400

    ok, message = send_telegram_notification_with_credentials(
        bot_token=bot_token,
        chat_id=chat_id,
        message="Picklist Automation settings test message.",
    )
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400


@bp.post("/api/settings/test-smtp")
@require_trusted_client
@require_csrf
def api_test_smtp_settings():
    if not settings_access_granted():
        return jsonify({"ok": False, "message": "Unlock settings before testing."}), 403

    payload = request.get_json(silent=True) or {}

    smtp_host = (payload.get("smtp_host") or "").strip() or (
        get_config_value("smtp_host", "SMTP_HOST", "") or ""
    )
    smtp_user = (payload.get("smtp_user") or "").strip() or (
        get_config_value("smtp_user", "SMTP_USER", "") or ""
    )
    smtp_password = (payload.get("smtp_password") or "").strip() or (
        get_config_value("smtp_password", "SMTP_PASSWORD", "") or ""
    )
    smtp_sender = (payload.get("smtp_sender") or "").strip() or (
        get_config_value("smtp_sender", "SMTP_SENDER", "") or ""
    )

    recipients_raw = (payload.get("smtp_recipient") or "").strip()
    if not recipients_raw:
        recipients_raw = get_config_value("smtp_recipient", "SMTP_RECIPIENT", "") or ""
    smtp_recipients = parse_recipient_addresses(recipients_raw)
    if smtp_recipients and not recipients_are_valid(smtp_recipients):
        return jsonify({"ok": False, "message": "Recipient list contains an invalid email address."}), 400

    smtp_port_raw = (
        str(payload.get("smtp_port", "")).strip()
        or (get_config_value("smtp_port", "SMTP_PORT", "587") or "587")
    )
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        return jsonify({"ok": False, "message": "SMTP port must be numeric."}), 400
    if smtp_port < 1 or smtp_port > 65535:
        return jsonify({"ok": False, "message": "SMTP port must be between 1 and 65535."}), 400

    smtp_use_tls = payload.get("smtp_use_tls")
    if isinstance(smtp_use_tls, bool):
        use_tls = smtp_use_tls
    else:
        use_tls = parse_bool(
            get_config_value("smtp_use_tls", "SMTP_USE_TLS", default="true"),
            default=True,
        )

    ok, message = send_email_notification_with_config(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        smtp_sender=smtp_sender,
        smtp_recipients=smtp_recipients,
        smtp_use_tls=use_tls,
        subject="Picklist Automation SMTP settings test",
        body="This is a test email from Picklist Automation settings validation.",
    )
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400
