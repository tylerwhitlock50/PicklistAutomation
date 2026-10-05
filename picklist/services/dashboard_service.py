"""View models for the run page and status API."""
from typing import Optional
from datetime import datetime, timezone

from picklist.config import QUERY_FILES, UI_REFRESH_INTERVAL_SECONDS
from picklist.scheduler import get_next_scheduled_run
from picklist.services.run_history import (
    get_latest_run_summary,
    get_latest_successful_run_summary,
    get_recent_runs,
)
from picklist.services.run_service import get_run_state_snapshot
from picklist.timeutil import format_datetime_for_display, format_relative_age, format_run_timestamp


def chronological_runs(runs_by_type: dict[str, list[dict]]) -> list[dict]:
    """Interleave the displayed run types by actual time, with a stable tie break."""
    def key(run):
        stamp = datetime.fromisoformat(run["run_timestamp"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp, run["id"]
    rows = [{**run, "query_type": kind} for kind, runs in runs_by_type.items() for run in runs]
    return sorted(rows, key=key, reverse=True)


def build_dashboard_data(recent_limit: int = 5) -> tuple[
    list[str],
    dict[str, Optional[dict]],
    dict[str, list[dict]],
    dict[str, Optional[str]],
]:
    query_types = list(QUERY_FILES.keys())
    latest_runs_by_type: dict[str, Optional[dict]] = {}
    recent_runs_by_type: dict[str, list[dict]] = {}
    latest_success_age_by_type: dict[str, Optional[str]] = {}

    for mode in query_types:
        latest_summary = get_latest_run_summary(mode)
        if latest_summary:
            formatted_latest = dict(latest_summary)
            formatted_latest["formatted_run_timestamp"] = format_run_timestamp(
                latest_summary["run_timestamp"]
            )
            latest_runs_by_type[mode] = formatted_latest
        else:
            latest_runs_by_type[mode] = None

        latest_success = get_latest_successful_run_summary(mode)
        if latest_success:
            latest_success_age_by_type[mode] = format_relative_age(latest_success["run_timestamp"])
        else:
            latest_success_age_by_type[mode] = None

        formatted_recent = []
        for run in get_recent_runs(query_type=mode, limit=recent_limit):
            formatted_run = dict(run)
            formatted_run["formatted_run_timestamp"] = format_run_timestamp(run["run_timestamp"])
            formatted_recent.append(formatted_run)
        recent_runs_by_type[mode] = formatted_recent

    return query_types, latest_runs_by_type, recent_runs_by_type, latest_success_age_by_type


def build_status_payload() -> dict:
    query_types, latest_runs_by_type, _, latest_success_age_by_type = build_dashboard_data(
        recent_limit=1
    )
    next_run = get_next_scheduled_run()

    active_runs = get_run_state_snapshot()
    for query_type in query_types:
        active_runs.setdefault(
            query_type,
            {"running": False, "started_at": None, "started_at_display": None},
        )

    return {
        "active_runs": active_runs,
        "latest_runs_by_type": latest_runs_by_type,
        "latest_success_age_by_type": latest_success_age_by_type,
        "next_run": format_datetime_for_display(next_run) if next_run else None,
        "refresh_interval_seconds": UI_REFRESH_INTERVAL_SECONDS,
    }
