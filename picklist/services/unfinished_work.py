"""Local pick/verify cleanup queue; opening a screen does not count as activity."""
from datetime import datetime, timedelta, timezone
import logging

from picklist.stores import pick_store, verify_store, audit_store
from picklist.timeutil import _audit_dt_display

IDLE_HOURS = 10
logger = logging.getLogger(__name__)


class OpenWork(list):
    """Rows plus an explicit partial-data indicator for optional audit storage."""
    audit_unavailable = False



def build_unfinished_work(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    work = OpenWork()
    for kind, store in (("pick", pick_store), ("verify", verify_store), ("audit", audit_store)):
        try:
            sessions = store.unfinished_sessions()
        except Exception:
            if kind != "audit":
                raise
            logger.warning("Active audits unavailable; showing local pick and verification work", exc_info=True)
            work.audit_unavailable = True
            continue
        for session in sessions:
            timestamp = session["last_activity"]
            try:
                last = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                last = now
            elapsed = max(timedelta(), now - last)
            work.append({
                **session, "kind": kind,
                "label": (session.get("order_ids") or f"Pick session #{session['id']}") if kind == "pick" else ((session.get("label") or session.get("scope") or "Full audit") if kind == "audit" else session["packlist_id"]),
                "operator_display": session.get("assigned_operators") or session.get("operator") or "Not recorded",
                "last_activity_display": _audit_dt_display(timestamp),
                "idle_hours": round(elapsed.total_seconds() / 3600, 1),
                "stale": elapsed >= timedelta(hours=IDLE_HOURS),
                "type_label": session.get("query_type", "Audit" if kind == "audit" else "Verify"),
                "_activity_sort": last,
            })
    work.sort(key=lambda row: (not row["stale"], row["_activity_sort"], row["kind"], row["id"]))
    return work
