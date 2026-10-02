"""Microsoft Teams notifications (incoming webhook / Workflows).

Mirrors the Telegram/SMTP contract in app.py: skip quietly when unconfigured,
log on failure, optionally fall back to email, never raise into a request.

The webhook receives an Adaptive Card wrapped in the envelope the Teams
"When a Teams webhook request is received" Workflow template expects. The
legacy Office 365 connector (MessageCard) format is retired, so we do not
emit it. A ``transport`` seam lets a Graph chat-message transport be added
later without touching callers.

Idempotency: callers pass ``event_key`` (e.g. ``shipped_digest:2026-10-01``);
a key that was already sent is skipped. Every attempt is recorded in the
``notification_log`` SQLite table for troubleshooting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

import requests

TEAMS_EVENTS: dict[str, str] = {
    "hold_created": "New order holds",
    "hold_resolved": "Holds cleared",
    "request_created": "New requests",
    "request_assigned": "Request assigned",
    "request_done": "Request completed / declined",
    "shipped_digest": "Daily shipped + tracking digest",
    "picklist_run": "Picklist run results",
}
DEFAULT_ENABLED_EVENTS = (
    "hold_created",
    "hold_resolved",
    "request_created",
    "request_assigned",
    "request_done",
    "shipped_digest",
)
ROWS_PER_CARD = 25
WEBHOOK_TIMEOUT_SECONDS = 10
MAX_TEXT_CHARS = 4000

_logger = logging.getLogger("picklist-app.notifier")
_get_conn: Optional[Callable[[], sqlite3.Connection]] = None
_get_config_value: Optional[Callable[..., Optional[str]]] = None
_send_email: Optional[Callable[..., None]] = None
_transport: Optional[Callable[[str, dict], None]] = None


# --------------------------------------------------------------------------- setup


def initialize(get_conn: Callable[[], sqlite3.Connection]) -> None:
    global _get_conn  # noqa: PLW0603
    _get_conn = get_conn
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT,
                status TEXT NOT NULL,
                error TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_log_key
                ON notification_log(channel, event_key, status);
            CREATE INDEX IF NOT EXISTS idx_notification_log_created
                ON notification_log(created_at);
            """
        )
        conn.commit()
    finally:
        _close_if_owned(conn)


def configure(
    *,
    get_config_value: Callable[..., Optional[str]],
    send_email_notification: Optional[Callable[..., None]] = None,
    logger: Optional[logging.Logger] = None,
    transport: Optional[Callable[[str, dict], None]] = None,
) -> None:
    global _get_config_value, _send_email, _logger, _transport  # noqa: PLW0603
    _get_config_value = get_config_value
    _send_email = send_email_notification
    if logger is not None:
        _logger = logger
    _transport = transport


def _conn() -> sqlite3.Connection:
    if _get_conn is None:
        raise RuntimeError("notifier.initialize() must be called first")
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    return conn


def _close_if_owned(conn: sqlite3.Connection) -> None:
    if getattr(conn, "_notifier_shared", False):
        return
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config(setting_key: str, env_key: str, default: Optional[str] = None) -> Optional[str]:
    if _get_config_value is None:
        return default
    return _get_config_value(setting_key, env_key, default)


# --------------------------------------------------------------------------- config


def webhook_url() -> Optional[str]:
    value = (_config("teams_webhook_url", "TEAMS_WEBHOOK_URL") or "").strip()
    return value or None


def parse_enabled_events(raw: Any) -> set[str]:
    """CSV of event names; ``all`` means every known event; blank means defaults."""
    text = str(raw or "").strip().lower()
    if not text:
        return set(DEFAULT_ENABLED_EVENTS)
    if text == "all":
        return set(TEAMS_EVENTS)
    if text == "none":
        return set()
    return {item.strip() for item in text.split(",") if item.strip() in TEAMS_EVENTS}


def enabled_events() -> set[str]:
    return parse_enabled_events(_config("teams_enabled_events", "TEAMS_ENABLED_EVENTS", ""))


def event_enabled(event: str) -> bool:
    return event in enabled_events()


def public_url(path: str = "") -> Optional[str]:
    base = (_config("app_public_url", "APP_PUBLIC_URL") or "").strip().rstrip("/")
    if not base:
        return None
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


# --------------------------------------------------------------------------- cards


def _text_block(text: Any, **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "TextBlock", "text": str(text)[:MAX_TEXT_CHARS], "wrap": True}
    block.update(extra)
    return block


def _row_set(cells: Sequence[Any], *, header: bool = False) -> dict[str, Any]:
    columns = []
    for index, cell in enumerate(cells):
        columns.append(
            {
                "type": "Column",
                "width": "auto" if index == 0 else "stretch",
                "items": [
                    _text_block(
                        "" if cell is None else cell,
                        size="Small",
                        weight="Bolder" if header else "Default",
                        spacing="None",
                    )
                ],
            }
        )
    return {"type": "ColumnSet", "columns": columns, "spacing": "Small"}


def build_card(
    *,
    title: str,
    text: Optional[str] = None,
    facts: Optional[Iterable[tuple[str, Any]]] = None,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    columns: Optional[Sequence[str]] = None,
    link: Optional[str] = None,
    link_label: str = "Open in Warehouse Ops",
    footer: Optional[str] = None,
) -> dict[str, Any]:
    """Pure Adaptive Card (v1.4) content. Rows beyond ROWS_PER_CARD are truncated."""
    body: list[dict[str, Any]] = [_text_block(title, weight="Bolder", size="Medium")]
    if text:
        body.append(_text_block(text))
    fact_items = [
        {"title": str(key), "value": "" if value is None else str(value)}
        for key, value in (facts or [])
    ]
    if fact_items:
        body.append({"type": "FactSet", "facts": fact_items})
    if rows:
        if columns:
            body.append(_row_set(columns, header=True))
        shown = list(rows)[:ROWS_PER_CARD]
        for row in shown:
            body.append(_row_set(row))
        if len(rows) > len(shown):
            body.append(
                _text_block(f"... {len(rows) - len(shown)} more not shown", isSubtle=True, size="Small")
            )
    if footer:
        body.append(_text_block(footer, isSubtle=True, size="Small"))
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": link_label, "url": link}]
    return card


def build_payload(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def chunk_rows(rows: Sequence[Any], size: int = ROWS_PER_CARD) -> list[list[Any]]:
    items = list(rows)
    if not items:
        return [[]]
    return [items[index : index + size] for index in range(0, len(items), size)]


def card_to_text(card: dict[str, Any]) -> str:
    """Plain-text rendering for the email fallback."""
    lines: list[str] = []
    for block in card.get("body", []):
        kind = block.get("type")
        if kind == "TextBlock":
            lines.append(str(block.get("text", "")))
        elif kind == "FactSet":
            for fact in block.get("facts", []):
                lines.append(f"{fact.get('title')}: {fact.get('value')}")
        elif kind == "ColumnSet":
            cells = []
            for column in block.get("columns", []):
                for item in column.get("items", []):
                    cells.append(str(item.get("text", "")))
            lines.append(" | ".join(cells))
    for action in card.get("actions", []):
        if action.get("url"):
            lines.append(str(action["url"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- log


def already_sent(channel: str, event_key: Optional[str]) -> bool:
    if not event_key or _get_conn is None:
        return False
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM notification_log WHERE channel = ? AND event_key = ? AND status = 'sent' LIMIT 1",
            (channel, event_key),
        ).fetchone()
        return row is not None
    finally:
        _close_if_owned(conn)


def record(
    channel: str,
    event_type: str,
    status: str,
    *,
    event_key: Optional[str] = None,
    error: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if _get_conn is None:
        return
    try:
        conn = _conn()
    except Exception:  # noqa: BLE001
        return
    try:
        conn.execute(
            """
            INSERT INTO notification_log
                (channel, event_type, event_key, status, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel,
                event_type,
                event_key,
                status,
                (error or None),
                json.dumps(payload, default=str) if payload is not None else None,
                _now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("Could not record notification log row: %s", exc)
    finally:
        _close_if_owned(conn)


def recent_log(limit: int = 50) -> list[dict[str, Any]]:
    if _get_conn is None:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, channel, event_type, event_key, status, error, created_at "
            "FROM notification_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        _close_if_owned(conn)


# --------------------------------------------------------------------------- send


def post_webhook(url: str, payload: dict[str, Any]) -> None:
    """Raise ``requests.RequestException`` on any delivery failure."""
    if _transport is not None:
        _transport(url, payload)
        return
    response = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
    response.raise_for_status()


def send_teams_notification(
    event: str,
    *,
    title: str,
    text: Optional[str] = None,
    facts: Optional[Iterable[tuple[str, Any]]] = None,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    columns: Optional[Sequence[str]] = None,
    link: Optional[str] = None,
    link_label: str = "Open in Warehouse Ops",
    footer: Optional[str] = None,
    event_key: Optional[str] = None,
    allow_fallback: bool = True,
    force: bool = False,
) -> bool:
    """Post one Adaptive Card to Teams. Returns True when delivered.

    ``force`` bypasses the enabled-events check (used by the settings test).
    Never raises.
    """
    if event not in TEAMS_EVENTS:
        _logger.warning("Unknown Teams event '%s'; sending anyway.", event)
    url = webhook_url()
    if not url:
        _logger.info("Teams webhook not configured; skipping %s notification.", event)
        record("teams", event, "skipped", event_key=event_key, error="webhook not configured")
        return False
    if not force and not event_enabled(event):
        _logger.info("Teams event %s is disabled in settings; skipping.", event)
        record("teams", event, "skipped", event_key=event_key, error="event disabled")
        return False
    if already_sent("teams", event_key):
        _logger.info("Teams notification %s already sent (key %s); skipping.", event, event_key)
        return True

    card = build_card(
        title=title,
        text=text,
        facts=facts,
        rows=rows,
        columns=columns,
        link=link,
        link_label=link_label,
        footer=footer,
    )
    payload = build_payload(card)
    try:
        post_webhook(url, payload)
    except requests.RequestException as exc:
        _logger.exception("Failed to send Teams notification (%s): %s", event, exc)
        record("teams", event, "failed", event_key=event_key, error=str(exc), payload=payload)
        if allow_fallback and _send_email is not None:
            try:
                _send_email(
                    subject=f"Warehouse Ops alert: Teams notification failed ({title})",
                    body=(
                        "A Teams notification could not be delivered.\n\n"
                        f"Error: {exc}\n\n"
                        "Original card:\n"
                        f"{card_to_text(card)}"
                    ),
                    allow_fallback=False,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                _logger.warning("Email fallback for Teams notification failed: %s", fallback_exc)
        return False
    record("teams", event, "sent", event_key=event_key, payload=payload)
    return True


def send_test(url: Optional[str] = None) -> tuple[bool, str]:
    """Settings-page test. Uses the saved webhook when ``url`` is blank."""
    target = (url or "").strip() or webhook_url()
    if not target:
        return False, "Teams webhook URL is required."
    if not target.lower().startswith("https://"):
        return False, "Teams webhook URL must start with https://."
    card = build_card(
        title="Warehouse Ops test message",
        text="If you can read this, the Teams webhook is wired up.",
        facts=[("Sent", _now_iso())],
        link=public_url() or None,
    )
    try:
        post_webhook(target, build_payload(card))
    except requests.RequestException as exc:
        _logger.exception("Teams settings test failed: %s", exc)
        record("teams", "test", "failed", error=str(exc))
        return False, f"Teams request failed: {exc}"
    record("teams", "test", "sent")
    return True, "Teams test card sent successfully."
