"""Run picklist page, run triggers, exports, health and status."""
import io
from pathlib import Path

import pandas as pd
from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for

from picklist.config import (
    DEFAULT_QUERY_TYPE,
    QUERY_FILES,
    SCHEDULE_TIME,
    UI_REFRESH_INTERVAL_SECONDS,
)
from picklist.scheduler import get_next_scheduled_run
from picklist.security import get_csrf_token, require_csrf, require_trusted_client
from picklist.services.dashboard_service import build_dashboard_data, build_status_payload, chronological_runs
from picklist.services.query_options import (
    get_default_guns_query_options,
    get_query_type,
    parse_query_run_options,
)
from picklist.services.run_history import (
    build_export_filename,
    get_latest_run,
    get_latest_run_summary,
    get_latest_successful_run,
    get_run_budget,
    get_run_by_id,
    get_run_rows,
    parse_run_timestamp,
)
from picklist.services.run_service import (
    any_run_active,
    execute_picklist_run,
    get_dummy_picklist_rows,
    get_run_state_snapshot,
    is_run_active,
    start_picklist_run_async,
)
from picklist.timeutil import (
    build_time_diagnostics,
    format_datetime_for_display,
    format_run_timestamp,
    format_schedule_time,
    get_timezone_label,
)

bp = Blueprint("runs", __name__)


@bp.route("/")
@require_trusted_client
def index():
    query_type = get_query_type(request.args.get("query_type"))
    latest_run, latest_rows = get_latest_run(query_type=query_type)
    current_picklist_run = latest_run
    rows = latest_rows
    using_dummy_data = False
    showing_last_successful_run = False

    if not latest_run:
        rows = get_dummy_picklist_rows()
        using_dummy_data = True
        current_picklist_run = None
    elif latest_run["status"] != "success":
        current_picklist_run, rows = get_latest_successful_run(query_type=query_type)
        showing_last_successful_run = current_picklist_run is not None
    elif latest_run["row_count"] == 0:
        rows = []

    query_types, latest_runs_by_type, recent_runs_by_type, latest_success_age_by_type = (
        build_dashboard_data(recent_limit=5)
    )

    formatted_latest_run = None
    if latest_run:
        formatted_latest_run = dict(latest_run)
        formatted_latest_run["formatted_run_timestamp"] = format_run_timestamp(
            latest_run["run_timestamp"]
        )

    formatted_current_picklist_run = None
    if current_picklist_run:
        formatted_current_picklist_run = dict(current_picklist_run)
        formatted_current_picklist_run["formatted_run_timestamp"] = format_run_timestamp(
            current_picklist_run["run_timestamp"]
        )

    columns = list(rows[0].keys()) if rows else []
    next_run = get_next_scheduled_run()
    formatted_next_run = format_datetime_for_display(next_run) if next_run else None
    time_diagnostics = build_time_diagnostics(next_run)
    run_state_by_type = get_run_state_snapshot()
    guns_query_defaults = get_default_guns_query_options()
    run_budget_by_type = {qt: get_run_budget(qt) for qt in query_types}
    return render_template(
        "index.html",
        latest_run=formatted_latest_run,
        current_picklist_run=formatted_current_picklist_run,
        rows=rows,
        columns=columns,
        next_run=formatted_next_run,
        latest_runs_by_type=latest_runs_by_type,
        latest_success_age_by_type=latest_success_age_by_type,
        recent_runs_by_type=recent_runs_by_type,
        recent_runs=chronological_runs(recent_runs_by_type),
        using_dummy_data=using_dummy_data,
        showing_last_successful_run=showing_last_successful_run,
        active_query_type=query_type,
        query_options=query_types,
        schedule_time_display=format_schedule_time(SCHEDULE_TIME),
        timezone_label=get_timezone_label(),
        time_diagnostics=time_diagnostics,
        run_state_by_type=run_state_by_type,
        ui_refresh_interval_seconds=UI_REFRESH_INTERVAL_SECONDS,
        guns_query_defaults=guns_query_defaults,
        run_budget_by_type=run_budget_by_type,
    )


@bp.post("/run")
@require_trusted_client
@require_csrf
def run_picklist():
    query_type = get_query_type(request.form.get("query_type"))
    try:
        query_options = parse_query_run_options(query_type, request.form)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("runs.index", query_type=query_type))

    budget = get_run_budget(query_type)
    if budget["exhausted"]:
        flash(
            f"{query_type.capitalize()} has used its {budget['limit']} run"
            f"{'' if budget['limit'] == 1 else 's'} for the day. "
            f"Export the existing list instead — the next run unlocks {budget['resets_at_display']}.",
            "error",
        )
        return redirect(url_for("runs.index", query_type=query_type))

    if start_picklist_run_async(query_type=query_type, query_options=query_options):
        flash(
            f"{query_type.capitalize()} run started. Watch the live status card and export it when the run is ready.",
            "success",
        )
    else:
        flash(
            f"{query_type.capitalize()} run is already in progress. Wait for it to finish, then try again.",
            "error",
        )
    return redirect(url_for("runs.index", query_type=query_type))


@bp.post("/run-both")
@require_trusted_client
@require_csrf
def run_both_picklists():
    if any_run_active():
        flash("Another run is already in progress. Wait before using Run Both.", "error")
        return redirect(url_for("runs.index", query_type=DEFAULT_QUERY_TYPE))

    results: list[str] = []
    all_succeeded = True

    for query_type in QUERY_FILES:
        if get_run_budget(query_type)["exhausted"]:
            all_succeeded = False
            results.append(f"{query_type}: skipped (daily run limit)")
            continue
        export_path = execute_picklist_run(query_type=query_type)
        status = "success" if export_path else "failed"
        if status == "failed":
            all_succeeded = False
        results.append(f"{query_type}: {status}")

    flash_message = "Run both complete. " + " | ".join(results)
    flash(flash_message, "success" if all_succeeded else "error")
    return redirect(url_for("runs.index", query_type=DEFAULT_QUERY_TYPE))


@bp.get("/export")
@require_trusted_client
def export_latest():
    query_type = get_query_type(request.args.get("query_type"))
    if is_run_active(query_type):
        flash(
            f"{query_type.capitalize()} is currently running. Wait for it to finish before exporting.",
            "error",
        )
        return redirect(url_for("runs.index", query_type=query_type))

    latest_run = get_latest_run_summary(query_type=query_type)
    if not latest_run:
        flash(f"No {query_type} runs have completed yet. Run it first.", "error")
        return redirect(url_for("runs.index", query_type=query_type))

    if latest_run["status"] != "success":
        flash(
            f"The latest {query_type} run did not succeed, so export is not ready. Run it again first.",
            "error",
        )
        return redirect(url_for("runs.index", query_type=query_type))

    stored_export_path = latest_run["export_path"]
    if stored_export_path:
        stored_path = Path(stored_export_path)
        if stored_path.exists():
            return send_file(
                stored_path,
                as_attachment=True,
                download_name=stored_path.name,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    rows = get_run_rows(run_id=latest_run["id"])
    if not rows:
        flash(
            f"Latest {query_type} run completed, but its export file is unavailable. Please run it again.",
            "error",
        )
        return redirect(url_for("runs.index", query_type=query_type))

    df = pd.DataFrame(rows)
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    run_timestamp = parse_run_timestamp(latest_run["run_timestamp"])
    filename = build_export_filename(
        query_type=query_type,
        run_timestamp=run_timestamp,
        run_id=latest_run["id"],
    )
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/export/run/<int:run_id>")
@require_trusted_client
def export_run(run_id: int):
    query_type = get_query_type(request.args.get("query_type"))
    run = get_run_by_id(run_id=run_id, query_type=query_type)
    if not run:
        flash(f"Run #{run_id} was not found for {query_type}.", "error")
        return redirect(url_for("runs.index", query_type=query_type))

    if run["status"] != "success":
        flash(f"Run #{run_id} is not successful and cannot be exported.", "error")
        return redirect(url_for("runs.index", query_type=query_type))

    stored_export_path = run["export_path"]
    if stored_export_path:
        stored_path = Path(stored_export_path)
        if stored_path.exists():
            return send_file(
                stored_path,
                as_attachment=True,
                download_name=stored_path.name,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    rows = get_run_rows(run_id=run_id)
    if not rows:
        flash(
            f"Run #{run_id} completed, but no stored export file or row data was found.",
            "error",
        )
        return redirect(url_for("runs.index", query_type=query_type))

    df = pd.DataFrame(rows)
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    run_timestamp = parse_run_timestamp(run["run_timestamp"])
    filename = build_export_filename(
        query_type=query_type,
        run_timestamp=run_timestamp,
        run_id=run["id"],
    )
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@bp.get("/api/status")
@require_trusted_client
def api_status():
    return jsonify(build_status_payload()), 200


@bp.get("/api/csrf")
@require_trusted_client
def api_csrf():
    return jsonify({"csrf_token": get_csrf_token()}), 200


@bp.get("/runs")
@require_trusted_client
def run_history_page():
    query_types, latest_runs_by_type, recent_runs_by_type, latest_success_age_by_type = (
        build_dashboard_data(recent_limit=25)
    )
    return render_template(
        "run_history.html",
        query_options=query_types,
        latest_runs_by_type=latest_runs_by_type,
        recent_runs_by_type=recent_runs_by_type,
        recent_runs=chronological_runs(recent_runs_by_type),
        latest_success_age_by_type=latest_success_age_by_type,
        timezone_label=get_timezone_label(),
    )


@bp.post("/api/run")
@require_trusted_client
@require_csrf
def api_run_picklist():
    payload = request.get_json(silent=True) or {}
    query_type = get_query_type(payload.get("query_type"))
    try:
        query_options = parse_query_run_options(query_type, payload)
    except ValueError as exc:
        return (
            jsonify(
                {
                    "status": "invalid",
                    "query_type": query_type,
                    "run_id": None,
                    "export_file": None,
                    "message": str(exc),
                }
            ),
            400,
        )
    if not start_picklist_run_async(query_type=query_type, query_options=query_options):
        latest_run, _ = get_latest_run(query_type=query_type)
        return (
            jsonify(
                {
                    "status": "running",
                    "query_type": query_type,
                    "run_id": latest_run["id"] if latest_run else None,
                    "export_file": None,
                    "message": "A run is already active for this query type.",
                }
            ),
            409,
        )

    return (
        jsonify(
            {
                "status": "started",
                "query_type": query_type,
                "run_id": None,
                "export_file": None,
                "message": "Picklist run started.",
            }
        ),
        202,
    )
