"""APScheduler jobs and the single-instance lock."""
import atexit
from datetime import datetime
from typing import Optional

try:
    import fcntl
except ImportError:  # Windows: no flock, the lock file is still created
    fcntl = None

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from picklist.config import (
    DEFAULT_QUERY_TYPE,
    ENABLE_SCHEDULER,
    logger,
    READINESS_REFRESH_MINUTES,
    SCHEDULE_TIME,
    SCHEDULE_TIMEZONE,
    SCHEDULER_LOCK_PATH,
    SHIPPED_DIGEST_CHECK_MINUTES,
)
from picklist.domain import notifier
from picklist.features import feature_enabled
from picklist.services import readiness_service, request_service
from picklist.services.digest_service import send_shipped_digest
from picklist.services.run_service import execute_picklist_run
from picklist.services.settings_service import get_teams_digest_time
from picklist.timeutil import _today_local, parse_schedule_time, resolve_timezone


SCHEDULER_LOCK_FILE = None


scheduler = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)


def acquire_scheduler_lock() -> bool:
    global SCHEDULER_LOCK_FILE  # noqa: PLW0603
    if SCHEDULER_LOCK_FILE is not None:
        return True

    lock_file = SCHEDULER_LOCK_PATH.open("w")
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False

    SCHEDULER_LOCK_FILE = lock_file
    return True


def start_scheduler() -> None:
    if not ENABLE_SCHEDULER:
        logger.info("Daily scheduler disabled via ENABLE_SCHEDULER=false.")
        return

    if not acquire_scheduler_lock():
        logger.info("Scheduler lock already held by another process; skipping scheduler startup.")
        return

    try:
        hour, minute = parse_schedule_time(SCHEDULE_TIME)
    except ValueError as exc:
        logger.error("Scheduler configuration error: %s", exc)
        logger.warning("Scheduler startup skipped due to invalid SCHEDULE_TIME value.")
        shutdown_scheduler()
        return

    if scheduler.running:
        return

    scheduler.add_job(
        execute_picklist_run,
        kwargs={"query_type": DEFAULT_QUERY_TYPE},
        trigger=CronTrigger(hour=hour, minute=minute),
        id="daily_picklist_run",
        replace_existing=True,
    )
    if READINESS_REFRESH_MINUTES > 0:
        scheduler.add_job(
            scheduled_readiness_refresh,
            trigger=IntervalTrigger(minutes=READINESS_REFRESH_MINUTES),
            id="readiness_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduled order readiness refresh every %d minutes.", READINESS_REFRESH_MINUTES)
    if SHIPPED_DIGEST_CHECK_MINUTES > 0:
        scheduler.add_job(
            scheduled_shipped_digest_check,
            trigger=IntervalTrigger(minutes=SHIPPED_DIGEST_CHECK_MINUTES),
            id="shipped_digest_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    logger.info(
        "Scheduled daily picklist run at %02d:%02d (%s).",
        hour,
        minute,
        scheduler.timezone,
    )
    atexit.register(shutdown_scheduler)


def shutdown_scheduler() -> None:
    global SCHEDULER_LOCK_FILE  # noqa: PLW0603
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if SCHEDULER_LOCK_FILE is not None:
        SCHEDULER_LOCK_FILE.close()
        SCHEDULER_LOCK_FILE = None


def get_next_scheduled_run() -> Optional[datetime]:
    job = scheduler.get_job("daily_picklist_run")
    if not job or not job.next_run_time:
        return None
    return job.next_run_time


def scheduled_shipped_digest_check() -> None:
    """Runs every few minutes; posts the digest once per day after the configured time."""
    if not feature_enabled("orders"):
        return
    if not notifier.webhook_url() or not notifier.event_enabled("shipped_digest"):
        return
    try:
        hour, minute = parse_schedule_time(get_teams_digest_time())
    except ValueError:
        logger.warning("Invalid Teams digest time '%s'; digest skipped.", get_teams_digest_time())
        return
    now_local = datetime.now(resolve_timezone())
    if (now_local.hour, now_local.minute) < (hour, minute):
        return
    day = _today_local()
    if notifier.already_sent("teams", f"shipped_digest:{day.isoformat()}"):
        return
    try:
        result = send_shipped_digest(day)
        if result["sent"]:
            logger.info("Shipped digest posted for %s (%d packlists).", day, result["packlists"])
    except Exception:  # noqa: BLE001 - scheduler job must never die
        logger.exception("Shipped digest failed")


def scheduled_readiness_refresh() -> None:
    if not feature_enabled("orders"):
        return
    try:
        request_service.expire_holds()
    except Exception:  # noqa: BLE001
        logger.exception("Manual hold expiry sweep failed")
    try:
        readiness_service.refresh("schedule", force=True)
    except Exception:  # noqa: BLE001 - scheduler job must never die
        logger.exception("Scheduled readiness refresh failed")
