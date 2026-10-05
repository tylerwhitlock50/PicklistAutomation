"""Regression coverage for schedule, source advice and display correctness."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from picklist import scheduler
from picklist.domain import shipments
from picklist.routes.pick import _session_warnings
from picklist.services.dashboard_service import chronological_runs
from picklist.timeutil import _local_dt_filter


@pytest.mark.parametrize("instant,offset", [
    ("2026-03-07T00:00:00+00:00", -7),
    ("2026-03-08T10:00:00+00:00", -6),
    ("2026-11-01T10:00:00+00:00", -7),
])
@pytest.mark.parametrize("host_zone", ["UTC", "America/Denver"])
def test_registered_schedule_uses_denver_across_dst(instant, offset, host_zone):
    fake = Mock(running=False)
    with patch.object(scheduler, "ENABLE_SCHEDULER", True), patch.object(scheduler, "acquire_scheduler_lock", return_value=True), patch.object(scheduler, "scheduler", fake), patch.object(scheduler, "SCHEDULE_TIME", "05:00"), patch.object(scheduler, "SCHEDULE_TIMEZONE", "America/Denver"), patch("apscheduler.triggers.cron.get_localzone", return_value=__import__('zoneinfo').ZoneInfo(host_zone)), patch.object(scheduler.atexit, "register"):
        scheduler.start_scheduler()
    job = next(call for call in fake.add_job.call_args_list if call.kwargs["id"] == "daily_picklist_run")
    fire = job.kwargs["trigger"].get_next_fire_time(None, datetime.fromisoformat(instant))
    assert (fire.hour, fire.minute) == (5, 0)
    assert fire.utcoffset() == timedelta(hours=offset)


def test_history_interleaves_absolute_times_without_mutating_sources():
    sources = {"guns": [{"id": 1, "run_timestamp": "2026-10-05T09:00:00-06:00"}],
               "components": [{"id": 2, "run_timestamp": "2026-10-05T14:30:00+00:00"}, {"id": 3, "run_timestamp": "2026-10-05T15:30:00"}]}
    assert [row["id"] for row in chronological_runs(sources)] == [3, 1, 2]
    assert "query_type" not in sources["guns"][0]


@pytest.mark.parametrize("value", [None, pd.NaT, "NaT", "None", "nan", ""])
def test_missing_dates_never_leak_sentinels(value):
    assert shipments._date_text(value) is None
    assert shipments._datetime_text(value) is None
    assert _local_dt_filter(value) == ""


def test_session_warnings_are_nonblocking_with_missing_sources():
    with patch("picklist.routes.pick.get_run_by_id", side_effect=RuntimeError("unavailable")), patch("picklist.routes.pick.readiness_store.latest_snapshot", side_effect=RuntimeError("unavailable")):
        warnings = _session_warnings({"source_runs_json": '{"guns": 1}'}, [])
    assert len(warnings) == 2
    assert all("unavailable" in item for item in warnings)


def test_session_warns_about_stale_source_and_relevant_readiness():
    stamp = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    with patch("picklist.routes.pick.get_run_by_id", return_value={"run_timestamp": stamp}), patch("picklist.routes.pick.readiness_store.latest_snapshot", return_value={"evaluated_at": stamp, "orders": [{"order_id": "SO-1", "state": "BLOCKED"}, {"order_id": "SO-OTHER", "state": "ATTENTION"}]}):
        warnings = _session_warnings({"source_runs_json": '{"guns": 1}'}, [{"cust_order_id": "SO-1"}])
    assert "Stale saved plan" in warnings[0]
    assert "SO-1 (blocked)" in warnings[1]
    assert "SO-OTHER" not in warnings[1]


def test_all_templates_compile():
    from picklist.app import app
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)


def test_today_survives_due_audit_outage():
    from picklist.app import app
    from picklist.routes import shipping
    with patch.object(shipping.audit_store, "list_location_status", side_effect=RuntimeError("offline")), patch.object(shipping, "build_unfinished_work", return_value=[]), patch.object(shipping, "_recent_sessions_for_display", return_value=[]), patch.object(shipping.request_store, "list_requests", return_value=[]):
        response = app.test_client().get("/work")
    assert response.status_code == 200
    assert "Due audit locations are temporarily unavailable" in response.get_data(as_text=True)
