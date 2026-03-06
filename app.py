import atexit
import fcntl
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import create_engine

try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:
    Fernet = None  # type: ignore[assignment]

    class InvalidToken(Exception):
        pass

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
def resolve_path_setting(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def resolve_db_path() -> Path:
    configured = resolve_path_setting(
        os.getenv("RUN_HISTORY_DB_PATH", "data/picklist_history.db")
    )
    # If a directory is mounted at the DB path, place the db file within it.
    if configured.exists() and configured.is_dir():
        return configured / "picklist_history.db"
    return configured


def _normalize_schedule_timezone(value: str) -> str:
    """Convert UTC±N values to IANA Etc/GMT timezone names."""
    if not value:
        return "UTC"

    normalized = value.strip()
    if normalized.upper() == "UTC":
        return "UTC"

    match = re.match(r"^UTC([+-])(\d{1,2})$", normalized, re.IGNORECASE)
    if not match:
        return normalized

    sign, hours_text = match.group(1), match.group(2)
    hours = int(hours_text)
    if hours == 0:
        return "UTC"
    if hours > 14:
        return normalized

    # IANA Etc/GMT uses opposite sign: Etc/GMT+7 means UTC-7.
    etc_sign = "-" if sign == "+" else "+"
    return f"Etc/GMT{etc_sign}{hours}"


DB_PATH = resolve_db_path()
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"


QUERY_FILES = {
    "guns": resolve_path_setting(os.getenv("GUNS_QUERY_FILE", "sql/query_guns.sql")),
    "components": resolve_path_setting(
        os.getenv("COMPONENTS_QUERY_FILE", "sql/query_components.sql")
    ),
}
DEFAULT_QUERY_TYPE = os.getenv("DEFAULT_QUERY_TYPE", "guns").lower()
if DEFAULT_QUERY_TYPE not in QUERY_FILES:
    DEFAULT_QUERY_TYPE = "guns"
MAX_RUNS = int(os.getenv("MAX_RUNS_TO_KEEP", "10"))
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "05:00")
RAW_SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC")
SCHEDULE_TIMEZONE = _normalize_schedule_timezone(RAW_SCHEDULE_TIMEZONE)
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE")
UI_REFRESH_INTERVAL_SECONDS = int(os.getenv("UI_REFRESH_INTERVAL_SECONDS", "15"))
ACCESS_MODE = os.getenv("ACCESS_MODE", "private").lower()
ACCESS_ALLOWED_CIDRS = os.getenv("ACCESS_ALLOWED_CIDRS", "")
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
SETTINGS_PASSWORD = os.getenv("SETTINGS_PASSWORD", "")
SETTINGS_ENCRYPTION_KEY = os.getenv("SETTINGS_ENCRYPTION_KEY", "").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
SETTINGS_SESSION_KEY = "_settings_access_granted"
SCHEDULER_LOCK_PATH = BASE_DIR / ".scheduler.lock"
ENCRYPTED_SETTING_PREFIX = "enc:v1:"
SENSITIVE_SETTING_KEYS = {
    "mssql_connection_string",
    "telegram_bot_token",
    "smtp_password",
}

SCHEDULER_LOCK_FILE = None
RUN_STATE_LOCK = threading.Lock()
RUN_STATE: dict[str, dict[str, Optional[datetime] | bool]] = {
    query_type: {"running": False, "started_at": None}
    for query_type in QUERY_FILES
}
SETTINGS_CIPHER: Optional[object] = None
SETTINGS_CIPHER_INITIALIZED = False

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("picklist-app")
if RAW_SCHEDULE_TIMEZONE.strip() != SCHEDULE_TIMEZONE:
    logger.info(
        "Normalized SCHEDULE_TIMEZONE '%s' to '%s'.",
        RAW_SCHEDULE_TIMEZONE,
        SCHEDULE_TIMEZONE,
    )

app = Flask(__name__)
flask_secret_key = os.getenv("FLASK_SECRET_KEY")
if not flask_secret_key:
    flask_secret_key = secrets.token_urlsafe(48)
    logger.warning(
        "FLASK_SECRET_KEY is not set; generated an ephemeral key for this process. "
        "Set FLASK_SECRET_KEY for stable sessions."
    )
app.secret_key = flask_secret_key
scheduler = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)


@app.before_request
def log_request_start() -> None:
    request.environ["request_start_time"] = time.perf_counter()


@app.after_request
def log_request_complete(response):
    if request.path.startswith("/static/"):
        return response

    started_at = request.environ.get("request_start_time")
    elapsed_ms = 0.0
    if isinstance(started_at, float):
        elapsed_ms = (time.perf_counter() - started_at) * 1000

    logger.info(
        "HTTP %s %s -> %s (%.1fms) from %s",
        request.method,
        request.path,
        response.status_code,
        elapsed_ms,
        request.remote_addr or "unknown",
    )
    return response


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


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def mask_connection_string(connection_string: str) -> str:
    if "://" not in connection_string:
        return "<invalid-connection-string>"

    scheme, rest = connection_string.split("://", maxsplit=1)
    if "@" in rest:
        credentials, host_part = rest.split("@", maxsplit=1)
        if ":" in credentials:
            username, _ = credentials.split(":", maxsplit=1)
            return f"{scheme}://{username}:***@{host_part}"
        return f"{scheme}://***@{host_part}"
    return f"{scheme}://{rest}"


def get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                row_count INTEGER DEFAULT 0,
                export_path TEXT,
                query_type TEXT NOT NULL DEFAULT 'guns',
                error_message TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "query_type" not in columns:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN query_type TEXT NOT NULL DEFAULT 'guns'"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                row_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def get_settings_cipher() -> Optional[object]:
    global SETTINGS_CIPHER_INITIALIZED, SETTINGS_CIPHER  # noqa: PLW0603
    if SETTINGS_CIPHER_INITIALIZED:
        return SETTINGS_CIPHER

    SETTINGS_CIPHER_INITIALIZED = True
    if not SETTINGS_ENCRYPTION_KEY:
        return None

    if Fernet is None:
        logger.warning(
            "cryptography is not installed; sensitive settings will remain plaintext."
        )
        return None

    try:
        SETTINGS_CIPHER = Fernet(SETTINGS_ENCRYPTION_KEY.encode("utf-8"))
        return SETTINGS_CIPHER
    except ValueError:
        logger.error(
            "Invalid SETTINGS_ENCRYPTION_KEY. Sensitive settings will remain plaintext."
        )
        SETTINGS_CIPHER = None
        return None


def encrypt_setting_value(key: str, value: str) -> str:
    if key not in SENSITIVE_SETTING_KEYS or not value:
        return value
    if value.startswith(ENCRYPTED_SETTING_PREFIX):
        return value

    cipher = get_settings_cipher()
    if not cipher:
        logger.warning(
            "Saving sensitive setting '%s' without encryption. Set SETTINGS_ENCRYPTION_KEY to encrypt at rest.",
            key,
        )
        return value
    encrypted = cipher.encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_SETTING_PREFIX}{encrypted}"


def decrypt_setting_value(key: str, value: str) -> str:
    if key not in SENSITIVE_SETTING_KEYS or not value:
        return value
    if not value.startswith(ENCRYPTED_SETTING_PREFIX):
        return value

    cipher = get_settings_cipher()
    if not cipher:
        logger.error(
            "Cannot decrypt sensitive setting '%s' because SETTINGS_ENCRYPTION_KEY is unavailable.",
            key,
        )
        return ""

    encrypted_value = value[len(ENCRYPTED_SETTING_PREFIX) :]
    try:
        return cipher.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error(
            "Failed to decrypt sensitive setting '%s'. Check SETTINGS_ENCRYPTION_KEY.",
            key,
        )
        return ""


def get_setting(key: str) -> Optional[str]:
    with get_sqlite_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return decrypt_setting_value(key, row["value"])


def set_setting(key: str, value: str) -> None:
    stored_value = encrypt_setting_value(key, value)
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, stored_value, datetime.utcnow().isoformat()),
        )


def migrate_sensitive_settings_encryption() -> None:
    cipher = get_settings_cipher()
    if not cipher:
        return

    with get_sqlite_conn() as conn:
        existing_rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
            tuple(SENSITIVE_SETTING_KEYS),
        ).fetchall()

        for row in existing_rows:
            key = row["key"]
            value = row["value"] or ""
            if not value or value.startswith(ENCRYPTED_SETTING_PREFIX):
                continue
            encrypted = encrypt_setting_value(key, value)
            if encrypted == value:
                continue
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
                (encrypted, datetime.utcnow().isoformat(), key),
            )
            logger.info("Encrypted existing sensitive setting '%s'.", key)


def get_config_value(setting_key: str, env_key: str, default: Optional[str] = None) -> Optional[str]:
    setting_value = get_setting(setting_key)
    if setting_value is not None and setting_value != "":
        return setting_value

    env_value = os.getenv(env_key)
    if env_value is not None and env_value != "":
        return env_value
    return default


def get_client_ip() -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    candidate = request.remote_addr
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
    if not candidate:
        return None

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def parse_networks(cidr_list: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in (item.strip() for item in cidr_list.split(",")):
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid CIDR in ACCESS_ALLOWED_CIDRS: %s", cidr)
    return networks


ALLOWED_NETWORKS = parse_networks(ACCESS_ALLOWED_CIDRS)


def request_is_allowed() -> bool:
    if ACCESS_MODE == "off":
        return True

    client_ip = get_client_ip()
    if not client_ip:
        return False

    if ACCESS_MODE == "cidr":
        if not ALLOWED_NETWORKS:
            logger.warning(
                "ACCESS_MODE=cidr but ACCESS_ALLOWED_CIDRS is empty; denying request."
            )
            return False
        return any(client_ip in network for network in ALLOWED_NETWORKS)

    # Default "private": allow local/private networks without user credentials.
    return client_ip.is_private or client_ip.is_loopback


def require_trusted_client(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if request_is_allowed():
            return view_func(*args, **kwargs)

        logger.warning(
            "Blocked request to %s from %s due to ACCESS_MODE=%s.",
            request.path,
            request.remote_addr,
            ACCESS_MODE,
        )
        abort(403)

    return wrapped


def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def require_csrf(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        validate_csrf()
        return view_func(*args, **kwargs)

    return wrapped


def validate_csrf() -> None:
    sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    session_token = session.get("_csrf_token")
    if not sent_token or not session_token:
        abort(400, description="Missing CSRF token.")
    if not secrets.compare_digest(sent_token, session_token):
        abort(400, description="Invalid CSRF token.")


app.jinja_env.globals["csrf_token"] = get_csrf_token


def get_query_type(value: Optional[str]) -> str:
    if value in QUERY_FILES:
        return value
    return DEFAULT_QUERY_TYPE


def try_mark_run_started(query_type: str) -> bool:
    started_at = datetime.now(timezone.utc)
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        if bool(state["running"]):
            return False
        state["running"] = True
        state["started_at"] = started_at
    return True


def mark_run_finished(query_type: str) -> None:
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        state["running"] = False
        state["started_at"] = None


def is_run_active(query_type: str) -> bool:
    with RUN_STATE_LOCK:
        state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
        return bool(state["running"])


def any_run_active() -> bool:
    with RUN_STATE_LOCK:
        return any(bool(state["running"]) for state in RUN_STATE.values())


def get_run_state_snapshot() -> dict[str, dict[str, Optional[str] | bool]]:
    snapshot: dict[str, dict[str, Optional[str] | bool]] = {}
    with RUN_STATE_LOCK:
        for query_type in QUERY_FILES:
            state = RUN_STATE.setdefault(query_type, {"running": False, "started_at": None})
            started_at_value = state["started_at"]
            started_at_iso = None
            started_at_display = None
            if isinstance(started_at_value, datetime):
                started_at_iso = started_at_value.isoformat()
                started_at_display = format_datetime_for_display(started_at_value)
            snapshot[query_type] = {
                "running": bool(state["running"]),
                "started_at": started_at_iso,
                "started_at_display": started_at_display,
            }
    return snapshot


def load_query(query_type: str) -> str:
    query_file = QUERY_FILES[query_type]
    if not query_file.exists():
        raise FileNotFoundError(f"Query file not found at: {query_file}")
    return query_file.read_text(encoding="utf-8")


def fetch_picklist_from_mssql(query_type: str) -> pd.DataFrame:
    query = load_query(query_type)
    mssql_conn_string = get_config_value(
        setting_key="mssql_connection_string",
        env_key="MSSQL_CONNECTION_STRING",
    )
    if not mssql_conn_string:
        raise ValueError(
            "MSSQL_CONNECTION_STRING is not set. Configure it in Settings or in your .env file."
        )

    logger.info(
        "Connecting to SQL Server using %s",
        mask_connection_string(mssql_conn_string),
    )
    engine = create_engine(mssql_conn_string)
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
        logger.info("Picklist query returned %d rows.", len(df.index))
        return df


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


def send_telegram_notification(message: str, *, allow_fallback: bool = True) -> None:
    bot_token = get_config_value("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    chat_id = get_config_value("telegram_chat_id", "TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.info("Telegram credentials not configured; skipping Telegram notification.")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.exception("Failed to send Telegram notification: %s", exc)
        if allow_fallback:
            send_email_notification(
                subject="Picklist alert: Telegram notification failed",
                body=(
                    "A Telegram notification could not be delivered.\n\n"
                    f"Error: {exc}\n\n"
                    "Original Telegram message:\n"
                    f"{message}"
                ),
                allow_fallback=False,
            )


def send_email_notification(
    subject: str,
    body: str,
    attachment: Optional[Path] = None,
    *,
    allow_fallback: bool = True,
) -> None:
    smtp_host = get_config_value("smtp_host", "SMTP_HOST")
    smtp_port_raw = get_config_value("smtp_port", "SMTP_PORT", default="587")
    smtp_user = get_config_value("smtp_user", "SMTP_USER")
    smtp_password = get_config_value("smtp_password", "SMTP_PASSWORD")
    smtp_sender = get_config_value("smtp_sender", "SMTP_SENDER")
    smtp_recipient = get_config_value("smtp_recipient", "SMTP_RECIPIENT")
    smtp_use_tls = parse_bool(
        get_config_value("smtp_use_tls", "SMTP_USE_TLS", default="true"),
        default=True,
    )
    recipients = [item.strip() for item in smtp_recipient.split(",")] if smtp_recipient else []
    recipients = [item for item in recipients if item]

    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, recipients]):
        logger.info("SMTP credentials incomplete; skipping email notification.")
        return

    try:
        smtp_port = int(smtp_port_raw or "587")
    except ValueError:
        logger.warning("Invalid SMTP_PORT '%s'. Falling back to 587.", smtp_port_raw)
        smtp_port = 587

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    if attachment and attachment.exists():
        with attachment.open("rb") as file:
            msg.add_attachment(
                file.read(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=attachment.name,
            )

    try:
        import smtplib

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
            logger.info("Email sent to %s with subject '%s'.", msg["To"], subject)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send SMTP email: %s", exc)
        if allow_fallback:
            send_telegram_notification(
                (
                    "Picklist alert: SMTP notification failed.\n"
                    f"Error: {exc}\n"
                    f"Intended subject: {subject}"
                ),
                allow_fallback=False,
            )


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


def get_dummy_picklist_rows() -> list[dict[str, str]]:
    return [
        {
            "Order": "SO-10425",
            "SKU": "LAMP-BASE-01",
            "Description": "Desk Lamp Base",
            "Location": "A1-03",
            "Qty": "8",
            "Priority": "High",
        },
        {
            "Order": "SO-10426",
            "SKU": "SHADE-IVY-08",
            "Description": "Ivy Fabric Shade",
            "Location": "B2-11",
            "Qty": "4",
            "Priority": "Medium",
        },
        {
            "Order": "SO-10427",
            "SKU": "BULB-WARM-60",
            "Description": "Warm White Bulb 60W",
            "Location": "C1-07",
            "Qty": "12",
            "Priority": "High",
        },
    ]


def execute_picklist_run(query_type: Optional[str] = None) -> Optional[Path]:
    started_at = time.perf_counter()
    run_timestamp = datetime.utcnow()
    normalized_query_type = get_query_type(query_type)
    if not try_mark_run_started(normalized_query_type):
        logger.info(
            "Skipped picklist run for %s because another run is already active.",
            normalized_query_type,
        )
        return None

    try:
        df = fetch_picklist_from_mssql(normalized_query_type)
        run_id = save_run(
            df=df,
            status="success",
            query_type=normalized_query_type,
            run_timestamp=run_timestamp,
        )
        export_path = generate_export(
            df=df,
            run_id=run_id,
            query_type=normalized_query_type,
            run_timestamp=run_timestamp,
        )
        elapsed_seconds = time.perf_counter() - started_at

        message = (
            f"✅ Picklist run {run_id} ({normalized_query_type}) succeeded with {len(df.index)} rows "
            f"in {elapsed_seconds:.2f}s."
        )
        logger.info(message)
        try:
            send_telegram_notification(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Telegram notification error: %s", exc)
        try:
            send_email_notification(
                subject=f"Picklist run {run_id} succeeded",
                body=message,
                attachment=export_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected SMTP notification error: %s", exc)
        return export_path
    except Exception as exc:  # noqa: BLE001
        elapsed_seconds = time.perf_counter() - started_at
        logger.exception("Picklist run failed: %s", exc)
        run_id = save_run(
            pd.DataFrame(),
            status="failed",
            query_type=normalized_query_type,
            run_timestamp=run_timestamp,
            error_message=str(exc),
        )
        message = (
            f"❌ Picklist run {run_id} ({normalized_query_type}) failed "
            f"after {elapsed_seconds:.2f}s: {exc}"
        )
        try:
            send_telegram_notification(message)
        except Exception as notify_exc:  # noqa: BLE001
            logger.exception("Unexpected Telegram notification error: %s", notify_exc)
        try:
            send_email_notification(
                subject=f"Picklist run {run_id} failed",
                body=(
                    f"{message}\n\n"
                    "Please review logs/app.log for the full traceback and failure context."
                ),
            )
        except Exception as notify_exc:  # noqa: BLE001
            logger.exception("Unexpected SMTP notification error: %s", notify_exc)
        return None
    finally:
        mark_run_finished(normalized_query_type)


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


def acquire_scheduler_lock() -> bool:
    global SCHEDULER_LOCK_FILE  # noqa: PLW0603
    if SCHEDULER_LOCK_FILE is not None:
        return True

    lock_file = SCHEDULER_LOCK_PATH.open("w")
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


def get_config_source(setting_key: str, env_key: str) -> str:
    setting_value = get_setting(setting_key)
    if setting_value not in {None, ""}:
        return "database"
    env_value = os.getenv(env_key)
    if env_value not in {None, ""}:
        return "environment"
    return "unset"


def settings_access_granted() -> bool:
    return bool(session.get(SETTINGS_SESSION_KEY, False))


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


def send_telegram_notification_with_credentials(
    bot_token: str, chat_id: str, message: str
) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Bot token and chat ID are required."

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True, "Telegram test message sent successfully."
    except requests.RequestException as exc:
        logger.exception("Telegram settings test failed: %s", exc)
        return False, f"Telegram request failed: {exc}"


def send_email_notification_with_config(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_sender: str,
    smtp_recipients: list[str],
    smtp_use_tls: bool,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, smtp_recipients]):
        return False, "SMTP host/user/password/sender/recipient are required."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(smtp_recipients)
    msg.set_content(body)

    try:
        import smtplib

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True, "SMTP test email sent successfully."
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP settings test failed: %s", exc)
        return False, f"SMTP test failed: {exc}"


initialize_db()
migrate_sensitive_settings_encryption()
start_scheduler()


@app.route("/")
@require_trusted_client
def index():
    query_type = get_query_type(request.args.get("query_type"))
    latest_run, rows = get_latest_run(query_type=query_type)
    using_dummy_data = False
    if not rows:
        rows = get_dummy_picklist_rows()
        using_dummy_data = True

    query_types, latest_runs_by_type, recent_runs_by_type, latest_success_age_by_type = (
        build_dashboard_data(recent_limit=5)
    )

    formatted_latest_run = None
    if latest_run:
        formatted_latest_run = dict(latest_run)
        formatted_latest_run["formatted_run_timestamp"] = format_run_timestamp(
            latest_run["run_timestamp"]
        )

    columns = list(rows[0].keys()) if rows else []
    next_run = get_next_scheduled_run()
    formatted_next_run = format_datetime_for_display(next_run) if next_run else None
    time_diagnostics = build_time_diagnostics(next_run)
    run_state_by_type = get_run_state_snapshot()
    return render_template(
        "index.html",
        latest_run=formatted_latest_run,
        rows=rows,
        columns=columns,
        next_run=formatted_next_run,
        latest_runs_by_type=latest_runs_by_type,
        latest_success_age_by_type=latest_success_age_by_type,
        recent_runs_by_type=recent_runs_by_type,
        using_dummy_data=using_dummy_data,
        active_query_type=query_type,
        query_options=query_types,
        schedule_time_display=format_schedule_time(SCHEDULE_TIME),
        timezone_label=get_timezone_label(),
        time_diagnostics=time_diagnostics,
        run_state_by_type=run_state_by_type,
        ui_refresh_interval_seconds=UI_REFRESH_INTERVAL_SECONDS,
    )


@app.route("/settings", methods=["GET", "POST"])
@require_trusted_client
def settings():
    if request.method == "POST":
        validate_csrf()
        action = (request.form.get("action") or "save").strip().lower()

        if action == "unlock":
            if not SETTINGS_PASSWORD:
                logger.error("SETTINGS_PASSWORD is not configured.")
                flash("SETTINGS_PASSWORD is not configured on this server.", "error")
                return redirect(url_for("settings"))
            submitted_password = request.form.get("settings_password") or ""
            if secrets.compare_digest(submitted_password, SETTINGS_PASSWORD):
                session[SETTINGS_SESSION_KEY] = True
                logger.info("Settings unlocked for client %s.", request.remote_addr)
                flash("Settings unlocked.", "success")
            else:
                logger.warning("Failed settings unlock attempt from %s.", request.remote_addr)
                flash("Invalid settings password.", "error")
            return redirect(url_for("settings"))

        if action == "logout":
            session.pop(SETTINGS_SESSION_KEY, None)
            logger.info("Settings locked for client %s.", request.remote_addr)
            flash("Settings locked.", "success")
            return redirect(url_for("settings"))

        if not settings_access_granted():
            logger.warning(
                "Blocked settings save attempt without unlock from %s.",
                request.remote_addr,
            )
            flash("Unlock settings before saving changes.", "error")
            return redirect(url_for("settings"))

        mssql_connection_string = (request.form.get("mssql_connection_string") or "").strip()
        if mssql_connection_string:
            set_setting("mssql_connection_string", mssql_connection_string)

        telegram_bot_token = (request.form.get("telegram_bot_token") or "").strip()
        if telegram_bot_token:
            set_setting("telegram_bot_token", telegram_bot_token)

        telegram_chat_id = (request.form.get("telegram_chat_id") or "").strip()
        if telegram_chat_id:
            set_setting("telegram_chat_id", telegram_chat_id)

        smtp_host = (request.form.get("smtp_host") or "").strip()
        if smtp_host:
            set_setting("smtp_host", smtp_host)

        smtp_port = (request.form.get("smtp_port") or "").strip()
        if smtp_port:
            set_setting("smtp_port", smtp_port)

        smtp_user = (request.form.get("smtp_user") or "").strip()
        if smtp_user:
            set_setting("smtp_user", smtp_user)

        smtp_password = (request.form.get("smtp_password") or "").strip()
        if smtp_password:
            set_setting("smtp_password", smtp_password)

        smtp_sender = (request.form.get("smtp_sender") or "").strip()
        if smtp_sender:
            set_setting("smtp_sender", smtp_sender)

        smtp_recipient = (request.form.get("smtp_recipient") or "").strip()
        if smtp_recipient:
            set_setting("smtp_recipient", smtp_recipient)

        set_setting("smtp_use_tls", "true" if request.form.get("smtp_use_tls") else "false")
        logger.info("Settings updated by client %s.", request.remote_addr)
        flash("Settings saved. Values entered here override .env values.", "success")
        return redirect(url_for("settings"))

    next_run = get_next_scheduled_run()
    time_diagnostics = build_time_diagnostics(next_run)
    schedule_time_display = format_schedule_time(SCHEDULE_TIME)

    if not settings_access_granted():
        return render_template(
            "settings.html",
            settings_unlocked=False,
            time_diagnostics=time_diagnostics,
            schedule_time_display=schedule_time_display,
        )

    mssql_value = get_config_value("mssql_connection_string", "MSSQL_CONNECTION_STRING")
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

    return render_template(
        "settings.html",
        settings_unlocked=True,
        time_diagnostics=time_diagnostics,
        schedule_time_display=schedule_time_display,
        mssql_connection_string_masked=mask_connection_string(mssql_value)
        if mssql_value
        else "Not configured",
        mssql_source=get_config_source("mssql_connection_string", "MSSQL_CONNECTION_STRING"),
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
    )


@app.post("/run")
@require_trusted_client
@require_csrf
def run_picklist():
    query_type = get_query_type(request.form.get("query_type"))
    if is_run_active(query_type):
        flash(f"{query_type.capitalize()} run is already in progress.", "error")
        return redirect(url_for("index", query_type=query_type))

    export_path = execute_picklist_run(query_type=query_type)
    if export_path:
        flash(
            f"Picklist run complete for {query_type}. Export created at {export_path.name}",
            "success",
        )
    else:
        latest_summary = get_latest_run_summary(query_type)
        if latest_summary and latest_summary["status"] == "failed":
            flash(
                f"{query_type.capitalize()} run failed. No export was created. Try again, and contact support if the failure continues.",
                "error",
            )
        else:
            flash(
                f"{query_type.capitalize()} run could not be started. Wait for any active run to finish, then try again.",
                "error",
            )
    return redirect(url_for("index", query_type=query_type))


@app.post("/run-both")
@require_trusted_client
@require_csrf
def run_both_picklists():
    if any_run_active():
        flash("Another run is already in progress. Wait before using Run Both.", "error")
        return redirect(url_for("index", query_type=DEFAULT_QUERY_TYPE))

    results: list[str] = []
    all_succeeded = True

    for query_type in QUERY_FILES:
        export_path = execute_picklist_run(query_type=query_type)
        status = "success" if export_path else "failed"
        if status == "failed":
            all_succeeded = False
        results.append(f"{query_type}: {status}")

    flash_message = "Run both complete. " + " | ".join(results)
    flash(flash_message, "success" if all_succeeded else "error")
    return redirect(url_for("index", query_type=DEFAULT_QUERY_TYPE))


@app.get("/export")
@require_trusted_client
def export_latest():
    query_type = get_query_type(request.args.get("query_type"))
    if is_run_active(query_type):
        flash(
            f"{query_type.capitalize()} is currently running. Wait for it to finish before exporting.",
            "error",
        )
        return redirect(url_for("index", query_type=query_type))

    latest_run = get_latest_run_summary(query_type=query_type)
    if not latest_run:
        flash(f"No {query_type} runs have completed yet. Run it first.", "error")
        return redirect(url_for("index", query_type=query_type))

    if latest_run["status"] != "success":
        flash(
            f"The latest {query_type} run did not succeed, so export is not ready. Run it again first.",
            "error",
        )
        return redirect(url_for("index", query_type=query_type))

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
        return redirect(url_for("index", query_type=query_type))

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


@app.get("/export/run/<int:run_id>")
@require_trusted_client
def export_run(run_id: int):
    query_type = get_query_type(request.args.get("query_type"))
    run = get_run_by_id(run_id=run_id, query_type=query_type)
    if not run:
        flash(f"Run #{run_id} was not found for {query_type}.", "error")
        return redirect(url_for("index", query_type=query_type))

    if run["status"] != "success":
        flash(f"Run #{run_id} is not successful and cannot be exported.", "error")
        return redirect(url_for("index", query_type=query_type))

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
        return redirect(url_for("index", query_type=query_type))

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


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/api/status")
@require_trusted_client
def api_status():
    return jsonify(build_status_payload()), 200


@app.get("/api/csrf")
@require_trusted_client
def api_csrf():
    return jsonify({"csrf_token": get_csrf_token()}), 200


def parse_recipient_addresses(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def recipients_are_valid(recipients: list[str]) -> bool:
    email_pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    return all(email_pattern.match(address) for address in recipients)


@app.post("/api/settings/test-telegram")
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


@app.post("/api/settings/test-smtp")
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


@app.post("/api/run")
@require_trusted_client
@require_csrf
def api_run_picklist():
    payload = request.get_json(silent=True) or {}
    query_type = get_query_type(payload.get("query_type"))
    if is_run_active(query_type):
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

    export_path = execute_picklist_run(query_type=query_type)
    latest_run, _ = get_latest_run(query_type=query_type)

    return (
        jsonify(
            {
                "status": "success" if export_path else "failed",
                "query_type": query_type,
                "run_id": latest_run["id"] if latest_run else None,
                "export_file": export_path.name if export_path else None,
            }
        ),
        200 if export_path else 500,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=FLASK_DEBUG,
        use_reloader=False,
    )
