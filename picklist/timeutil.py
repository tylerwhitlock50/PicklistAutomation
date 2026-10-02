"""Time zone resolution and timestamp formatting."""
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from picklist.config import DISPLAY_TIMEZONE, logger, SCHEDULE_TIME, SCHEDULE_TIMEZONE


def resolve_timezone() -> ZoneInfo:
    configured_timezone = DISPLAY_TIMEZONE or SCHEDULE_TIMEZONE
    try:
        return ZoneInfo(configured_timezone)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid timezone '%s'. Falling back to UTC for display formatting.",
            configured_timezone,
        )
        return ZoneInfo("UTC")


def resolve_schedule_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(SCHEDULE_TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid schedule timezone '%s'. Falling back to UTC for scheduling display.",
            SCHEDULE_TIMEZONE,
        )
        return ZoneInfo("UTC")


def get_timezone_label() -> str:
    return resolve_timezone().key


def format_datetime_for_display(value: datetime) -> str:
    return value.astimezone(resolve_timezone()).strftime("%Y-%m-%d %H:%M %Z")


def format_run_timestamp(run_timestamp: str) -> str:
    parsed = datetime.fromisoformat(run_timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return format_datetime_for_display(parsed)


def format_schedule_time(time_value: str) -> str:
    try:
        hour, minute = parse_schedule_time(time_value)
    except ValueError:
        return f"Invalid time ({time_value})"

    timezone = resolve_schedule_timezone()
    sample = datetime(2000, 1, 1, hour, minute, tzinfo=timezone)
    return sample.strftime("%H:%M %Z")


def _local_dt_filter(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render an ISO timestamp (any zone) in plant time for templates."""
    if not value:
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16].replace("T", " ")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(resolve_timezone()).strftime(fmt)


def format_relative_age(run_timestamp: str) -> str:
    parsed = datetime.fromisoformat(run_timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - parsed
    if delta.total_seconds() < 60:
        return "just now"

    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"

    days = hours // 24
    return f"{days}d ago"


def format_time_snapshot(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def build_time_diagnostics(next_run: Optional[datetime]) -> dict[str, Optional[str]]:
    now_utc = datetime.now(timezone.utc)
    now_server = now_utc.astimezone()
    display_timezone = resolve_timezone()
    schedule_timezone = resolve_schedule_timezone()

    diagnostics: dict[str, Optional[str]] = {
        "server_now": format_time_snapshot(now_server),
        "server_timezone": now_server.tzname() or str(now_server.tzinfo),
        "display_now": format_time_snapshot(now_utc.astimezone(display_timezone)),
        "display_timezone": display_timezone.key,
        "utc_now": format_time_snapshot(now_utc),
        "schedule_timezone": schedule_timezone.key,
        "schedule_time": SCHEDULE_TIME,
        "next_run_schedule": None,
        "next_run_server": None,
        "next_run_display": None,
        "next_run_utc": None,
    }

    if next_run:
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=schedule_timezone)
        diagnostics["next_run_schedule"] = format_time_snapshot(
            next_run.astimezone(schedule_timezone)
        )
        diagnostics["next_run_server"] = format_time_snapshot(next_run.astimezone())
        diagnostics["next_run_display"] = format_time_snapshot(
            next_run.astimezone(display_timezone)
        )
        diagnostics["next_run_utc"] = format_time_snapshot(next_run.astimezone(timezone.utc))

    return diagnostics


def parse_schedule_time(time_value: str) -> tuple[int, int]:
    try:
        hour_str, minute_str = time_value.split(":", maxsplit=1)
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError as exc:
        raise ValueError("SCHEDULE_TIME must use HH:MM format (e.g., 05:00).") from exc

    if hour not in range(24) or minute not in range(60):
        raise ValueError("SCHEDULE_TIME must be a valid 24-hour time (00:00 to 23:59).")
    return hour, minute


def _audit_dt_display(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    try:
        return format_datetime_for_display(value)
    except (ValueError, TypeError):
        return str(value)


def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone(resolve_timezone()).date()


def _erp_local_time_display(value) -> Optional[str]:
    """ERP datetimes are already server-local — format without tz conversion."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    try:
        return value.strftime("%H:%M")
    except (AttributeError, ValueError):
        return str(value)
