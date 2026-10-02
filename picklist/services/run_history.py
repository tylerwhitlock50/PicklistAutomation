"""Persisted picklist runs, plan snapshots, reporting events and exports."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from picklist.config import (
    ESTIMATED_MINUTES_SAVED_PER_RUN,
    EXPORT_DIR,
    MAX_RUNS,
    PLAN_SNAPSHOT_RETENTION_DAYS,
    REPORTING_MIN_DISTINCT_RUN_MINUTES,
)
from picklist.db import get_sqlite_conn
from picklist.services.settings_service import get_max_runs_per_day
from picklist.timeutil import format_datetime_for_display, format_run_timestamp, resolve_timezone


def prune_old_runs(conn: sqlite3.Connection) -> None:
    old_run_ids = conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?", (MAX_RUNS,)
    ).fetchall()
    if not old_run_ids:
        return

    ids = [row[0] for row in old_run_ids]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM run_rows WHERE run_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", ids)


def get_run_budget(query_type: str) -> dict[str, Any]:
    """Successful runs this list has used in the rolling 24-hour window.

    The window rolls rather than resetting at midnight: with a limit of 1,
    the next run unlocks 24 hours after the first one. Failed runs are free —
    an ERP hiccup should not burn the day's generation. Scheduled runs count
    the same as button presses.
    """
    limit = get_max_runs_per_day()
    budget: dict[str, Any] = {
        "limit": limit,
        "used": 0,
        "remaining": None,
        "exhausted": False,
        "resets_at": None,
        "resets_at_display": None,
    }
    if limit <= 0:
        return budget
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT run_timestamp FROM runs
            WHERE query_type = ? AND status = 'success' AND run_timestamp > ?
            ORDER BY run_timestamp
            """,
            (query_type, cutoff),
        ).fetchall()
    budget["used"] = len(rows)
    budget["remaining"] = max(0, limit - len(rows))
    if len(rows) >= limit:
        budget["exhausted"] = True
        # The oldest counted run ages out of the window first.
        overflow = rows[len(rows) - limit]
        resets_at = datetime.fromisoformat(overflow["run_timestamp"]).replace(
            tzinfo=timezone.utc
        ) + timedelta(hours=24)
        budget["resets_at"] = resets_at
        budget["resets_at_display"] = format_datetime_for_display(resets_at)
    return budget


def save_run(
    df: pd.DataFrame,
    status: str,
    query_type: str,
    run_timestamp: datetime,
    error_message: Optional[str] = None,
) -> int:
    run_timestamp_iso = run_timestamp.isoformat()
    with get_sqlite_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (run_timestamp, status, row_count, query_type, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_timestamp_iso, status, len(df.index), query_type, error_message),
        )
        run_id = cursor.lastrowid

        if not df.empty:
            rows = [(run_id, json.dumps(row, default=str)) for row in df.to_dict(orient="records")]
            conn.executemany("INSERT INTO run_rows (run_id, row_json) VALUES (?, ?)", rows)

        prune_old_runs(conn)
        return run_id


def plan_date_for_run_timestamp(run_timestamp: datetime) -> str:
    """Calendar day (display timezone) a picklist run belongs to."""
    if run_timestamp.tzinfo is None:
        run_timestamp = run_timestamp.replace(tzinfo=timezone.utc)
    return run_timestamp.astimezone(resolve_timezone()).date().isoformat()


def prune_plan_snapshots(conn: sqlite3.Connection) -> None:
    cutoff = (
        datetime.now(timezone.utc).astimezone(resolve_timezone()).date()
        - timedelta(days=PLAN_SNAPSHOT_RETENTION_DAYS)
    ).isoformat()
    conn.execute("DELETE FROM plan_snapshots WHERE plan_date < ?", (cutoff,))


def save_plan_snapshot(
    run_id: int,
    query_type: str,
    run_timestamp: datetime,
    rows: list[dict],
) -> None:
    """Keep the day's plan for reconciliation.

    INSERT OR IGNORE: the first successful run of the day is the plan — later
    re-runs shrink as orders ship, so overwriting would hide the real target.
    """
    plan_date = plan_date_for_run_timestamp(run_timestamp)
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO plan_snapshots
                (plan_date, query_type, run_id, run_timestamp, rows_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                plan_date,
                query_type,
                run_id,
                run_timestamp.isoformat(),
                json.dumps(rows, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        prune_plan_snapshots(conn)


def backfill_plan_snapshots() -> None:
    """Seed plan_snapshots from whatever runs survive in the runs table.

    Makes reconciliation work immediately after this feature ships instead of
    only for runs that happen after the upgrade.
    """
    with get_sqlite_conn() as conn:
        runs = conn.execute(
            "SELECT id, run_timestamp, query_type FROM runs WHERE status = 'success' ORDER BY id"
        ).fetchall()
    for run in runs:
        try:
            run_timestamp = parse_run_timestamp(run["run_timestamp"])
        except ValueError:
            continue
        save_plan_snapshot(
            run_id=run["id"],
            query_type=run["query_type"],
            run_timestamp=run_timestamp,
            rows=get_run_rows(run["id"]),
        )


def get_plan_snapshots(plan_date: str) -> dict[str, dict]:
    """{query_type: {run_id, run_timestamp, rows}} for one plan date."""
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM plan_snapshots WHERE plan_date = ?", (plan_date,)
        ).fetchall()
    plans: dict[str, dict] = {}
    for row in rows:
        try:
            parsed_rows = json.loads(row["rows_json"])
        except (TypeError, json.JSONDecodeError):
            parsed_rows = []
        plans[row["query_type"]] = {
            "run_id": row["run_id"],
            "run_timestamp": row["run_timestamp"],
            "rows": parsed_rows,
        }
    return plans


def list_plan_dates(limit: int = 45) -> list[str]:
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT plan_date FROM plan_snapshots ORDER BY plan_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row["plan_date"] for row in rows]


def record_reporting_event(
    source_run_id: int,
    query_type: str,
    event_timestamp: datetime,
) -> bool:
    threshold_seconds = REPORTING_MIN_DISTINCT_RUN_MINUTES * 60
    event_timestamp_iso = event_timestamp.isoformat()

    with get_sqlite_conn() as conn:
        latest_event = conn.execute(
            """
            SELECT event_timestamp
            FROM reporting_events
            ORDER BY event_timestamp DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

        if latest_event:
            latest_timestamp = parse_run_timestamp(latest_event["event_timestamp"])
            elapsed_seconds = (event_timestamp - latest_timestamp).total_seconds()
            if elapsed_seconds < threshold_seconds:
                return False

        conn.execute(
            """
            INSERT INTO reporting_events (event_timestamp, source_run_id, query_type)
            VALUES (?, ?, ?)
            """,
            (event_timestamp_iso, source_run_id, query_type),
        )
        return True


def get_reporting_metrics() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    display_timezone = resolve_timezone()
    start_of_today_display = now_utc.astimezone(display_timezone).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    start_of_today_utc = start_of_today_display.astimezone(timezone.utc)
    start_of_today_utc_iso = start_of_today_utc.replace(tzinfo=None).isoformat()

    with get_sqlite_conn() as conn:
        total_runs_count = conn.execute(
            "SELECT COUNT(*) AS count FROM reporting_events"
        ).fetchone()["count"]
        today_runs_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reporting_events
            WHERE event_timestamp >= ?
            """,
            (start_of_today_utc_iso,),
        ).fetchone()["count"]
        latest_event = conn.execute(
            """
            SELECT event_timestamp
            FROM reporting_events
            ORDER BY event_timestamp DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    total_minutes_saved = total_runs_count * ESTIMATED_MINUTES_SAVED_PER_RUN
    today_minutes_saved = today_runs_count * ESTIMATED_MINUTES_SAVED_PER_RUN
    latest_event_display = None
    if latest_event:
        latest_event_display = format_run_timestamp(latest_event["event_timestamp"])

    return {
        "total_runs_count": total_runs_count,
        "today_runs_count": today_runs_count,
        "total_minutes_saved": total_minutes_saved,
        "today_minutes_saved": today_minutes_saved,
        "estimated_minutes_per_run": ESTIMATED_MINUTES_SAVED_PER_RUN,
        "distinct_window_minutes": REPORTING_MIN_DISTINCT_RUN_MINUTES,
        "latest_event_display": latest_event_display,
    }


def format_run_timestamp_for_filename(run_timestamp: datetime) -> str:
    return run_timestamp.strftime("%Y-%m-%d_%H%M")


def parse_run_timestamp(run_timestamp: str) -> datetime:
    return datetime.fromisoformat(run_timestamp)


def build_export_filename(query_type: str, run_timestamp: datetime, run_id: int) -> str:
    formatted_timestamp = format_run_timestamp_for_filename(run_timestamp)
    return f"picklist_{query_type}_{formatted_timestamp}_run{run_id}.xlsx"


def generate_export(df: pd.DataFrame, run_id: int, query_type: str, run_timestamp: datetime) -> Path:
    export_path = EXPORT_DIR / build_export_filename(
        query_type=query_type,
        run_timestamp=run_timestamp,
        run_id=run_id,
    )
    df.to_excel(export_path, index=False)

    with get_sqlite_conn() as conn:
        conn.execute("UPDATE runs SET export_path = ? WHERE id = ?", (str(export_path), run_id))

    return export_path


def get_latest_run(query_type: str):
    with get_sqlite_conn() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE query_type = ? ORDER BY id DESC LIMIT 1",
            (query_type,),
        ).fetchone()
        if not run:
            return None, []

        rows = conn.execute(
            "SELECT row_json FROM run_rows WHERE run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()
        parsed_rows = [json.loads(row["row_json"]) for row in rows]

        return run, parsed_rows


def get_latest_successful_run(query_type: str):
    with get_sqlite_conn() as conn:
        run = conn.execute(
            """
            SELECT *
            FROM runs
            WHERE query_type = ? AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """,
            (query_type,),
        ).fetchone()
        if not run:
            return None, []

        rows = conn.execute(
            "SELECT row_json FROM run_rows WHERE run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()
        parsed_rows = [json.loads(row["row_json"]) for row in rows]
        return run, parsed_rows


def get_latest_run_summary(query_type: str) -> Optional[sqlite3.Row]:
    with get_sqlite_conn() as conn:
        return conn.execute(
            """
            SELECT id, run_timestamp, status, row_count, query_type, export_path, error_message
            FROM runs
            WHERE query_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (query_type,),
        ).fetchone()


def get_latest_successful_run_summary(query_type: str) -> Optional[sqlite3.Row]:
    with get_sqlite_conn() as conn:
        return conn.execute(
            """
            SELECT id, run_timestamp, status, row_count, query_type, export_path, error_message
            FROM runs
            WHERE query_type = ? AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """,
            (query_type,),
        ).fetchone()


def get_recent_runs(query_type: str, limit: int = 10) -> list[sqlite3.Row]:
    with get_sqlite_conn() as conn:
        return conn.execute(
            """
            SELECT id, run_timestamp, status, row_count, query_type, export_path, error_message
            FROM runs
            WHERE query_type = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (query_type, limit),
        ).fetchall()


def get_run_by_id(run_id: int, query_type: str) -> Optional[sqlite3.Row]:
    with get_sqlite_conn() as conn:
        return conn.execute(
            """
            SELECT id, run_timestamp, status, row_count, query_type, export_path, error_message
            FROM runs
            WHERE id = ? AND query_type = ?
            LIMIT 1
            """,
            (run_id, query_type),
        ).fetchone()


def get_run_rows(run_id: int) -> list[dict]:
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT row_json FROM run_rows WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [json.loads(row["row_json"]) for row in rows]
