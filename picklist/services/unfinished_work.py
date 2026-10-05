"""Local pick/verify cleanup queue; opening a screen does not count as activity."""
from datetime import datetime, timedelta, timezone

from picklist.stores import pick_store, verify_store
from picklist.timeutil import _audit_dt_display

IDLE_HOURS = 10


def build_unfinished_work(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    work = []
    for kind, store in (("pick", pick_store), ("verify", verify_store)):
        for session in store.unfinished_sessions():
            timestamp = session["last_activity"]
            try:
                last = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                last = now
            elapsed = max(timedelta(), now - last)
            work.append({
                **session, "kind": kind,
                "label": (session.get("order_ids") or f"Pick session #{session['id']}") if kind == "pick" else session["packlist_id"],
                "operator_display": session.get("assigned_operators") or session.get("operator") or "Not recorded",
                "last_activity_display": _audit_dt_display(timestamp),
                "idle_hours": round(elapsed.total_seconds() / 3600, 1),
                "stale": elapsed >= timedelta(hours=IDLE_HOURS),
                "type_label": session.get("query_type", "Verify"),
            })
    work.sort(key=lambda row: (not row["stale"], row["last_activity"], row["kind"], row["id"]))
    return work
