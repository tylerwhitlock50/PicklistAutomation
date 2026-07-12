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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Any, Optional
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
from sqlalchemy import create_engine, text

import audit_store
import audit_universe
import pick_store
import recon
import serial_history
import shortage

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
AUDIT_QUERY_FILE = resolve_path_setting(
    os.getenv("AUDIT_QUERY_FILE", "sql/audit_serialized_inventory.sql")
)
AUDIT_LOCATIONS_SYNC_FILE = resolve_path_setting(
    os.getenv("AUDIT_LOCATIONS_SYNC_FILE", "sql/audit_locations_sync.sql")
)
AUDIT_DWELL_QUERY_FILE = resolve_path_setting(
    os.getenv("AUDIT_DWELL_QUERY_FILE", "sql/audit_dwell_time.sql")
)
AUDIT_LOCATION_SYNC_MAX_AGE_MINUTES = 15
SERIAL_HISTORY_TRACE_FILE = resolve_path_setting(
    os.getenv("SERIAL_HISTORY_TRACE_FILE", "sql/serial_history_trace.sql")
)
SERIAL_HISTORY_TRANSACTIONS_FILE = resolve_path_setting(
    os.getenv("SERIAL_HISTORY_TRANSACTIONS_FILE", "sql/serial_history_transactions.sql")
)
SERIAL_HISTORY_SHIPMENTS_FILE = resolve_path_setting(
    os.getenv("SERIAL_HISTORY_SHIPMENTS_FILE", "sql/serial_history_shipments.sql")
)
SERIAL_MAX_LENGTH = 50
RECON_SHIPMENTS_FILE = resolve_path_setting(
    os.getenv("RECON_SHIPMENTS_FILE", "sql/recon_shipments.sql")
)
PICK_SERIAL_LOOKUP_FILE = resolve_path_setting(
    os.getenv("PICK_SERIAL_LOOKUP_FILE", "sql/pick_serial_lookup.sql")
)
# How many daily picklist plan snapshots to keep for shipping reconciliation.
PLAN_SNAPSHOT_RETENTION_DAYS = int(os.getenv("PLAN_SNAPSHOT_RETENTION_DAYS", "60"))
# Staged shipments should leave the building within this many hours.
STAGE_AGING_TARGET_HOURS = float(os.getenv("STAGE_AGING_TARGET_HOURS", "48"))
# SHIPPING locations whose ID contains this term count as staging bins.
STAGE_LOCATION_TERM = os.getenv("STAGE_LOCATION_TERM", "STAGE").strip().upper()
# Component shortage report: which product codes count as shippable
# components, how far out to look, and how long to cache the ERP pull.
SHORTAGE_QUERY_FILE = resolve_path_setting(
    os.getenv("SHORTAGE_QUERY_FILE", "sql/shortage_components.sql")
)
SHORTAGE_LOOKAHEAD_DAYS = int(os.getenv("SHORTAGE_LOOKAHEAD_DAYS", "10"))
SHORTAGE_PRODUCT_CODES = [
    code.strip().upper()
    for code in os.getenv(
        "SHORTAGE_PRODUCT_CODES",
        "FG-COMP,FG-STOCK,FG-APPAREL,COMPONENT,FG-BASE,FG-RING,FG-BARREL,FG-MUZZLE",
    ).split(",")
    if code.strip()
]
SHORTAGE_CACHE_MINUTES = float(os.getenv("SHORTAGE_CACHE_MINUTES", "5"))
SHORTAGE_PRODUCT_CODES_TOKEN = "__SHORTAGE_PRODUCT_CODES__"
GUNS_DEFAULT_LOOKAHEAD_DAYS = 10
GUNS_MAX_LOOKAHEAD_DAYS = 365
GUNS_BASE_EXCLUDED_CUSTOMERS = ("CA MARK",)
GUNS_MAX_ADDITIONAL_CUSTOMERS = 10
GUNS_LOOKAHEAD_TOKEN = "__GUNS_LOOKAHEAD_DAYS__"
GUNS_EXCLUDED_CUSTOMERS_TOKEN = "__GUNS_EXCLUDED_CUSTOMERS__"
DEFAULT_QUERY_TYPE = os.getenv("DEFAULT_QUERY_TYPE", "guns").lower()
if DEFAULT_QUERY_TYPE not in QUERY_FILES:
    DEFAULT_QUERY_TYPE = "guns"
MAX_RUNS = int(os.getenv("MAX_RUNS_TO_KEEP", "10"))
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "05:00")
RAW_SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC")
SCHEDULE_TIMEZONE = _normalize_schedule_timezone(RAW_SCHEDULE_TIMEZONE)
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE")
# Dashboard status polling when no run is active (browser polls faster while a run is running).
UI_REFRESH_INTERVAL_SECONDS = int(os.getenv("UI_REFRESH_INTERVAL_SECONDS", "5"))
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
REPORTING_MIN_DISTINCT_RUN_MINUTES = 5
ESTIMATED_MINUTES_SAVED_PER_RUN = 5

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reporting_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_timestamp TEXT NOT NULL,
                source_run_id INTEGER NOT NULL,
                query_type TEXT NOT NULL
            )
            """
        )
        # One picklist plan per (day, query type) for shipping reconciliation.
        # The runs table only keeps the last MAX_RUNS runs, so recon gets its
        # own copy — the FIRST successful run of the day is that day's plan.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_snapshots (
                plan_date TEXT NOT NULL,
                query_type TEXT NOT NULL,
                run_id INTEGER NOT NULL,
                run_timestamp TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (plan_date, query_type)
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


def delete_setting(key: str) -> None:
    with get_sqlite_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


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


def normalize_customer_term(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).upper()


def get_default_guns_query_options() -> dict[str, Any]:
    return {
        "lookahead_days": GUNS_DEFAULT_LOOKAHEAD_DAYS,
        "base_excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "additional_excluded_customers": [],
        "excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "has_overrides": False,
    }


def parse_guns_lookahead_days(raw_value: Any) -> int:
    if raw_value is None or raw_value == "":
        return GUNS_DEFAULT_LOOKAHEAD_DAYS

    try:
        lookahead_days = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Lookahead days must be a whole number.") from exc

    if lookahead_days < 1 or lookahead_days > GUNS_MAX_LOOKAHEAD_DAYS:
        raise ValueError(
            f"Lookahead days must be between 1 and {GUNS_MAX_LOOKAHEAD_DAYS}."
        )
    return lookahead_days


def parse_additional_excluded_customers(raw_value: Any) -> list[str]:
    if raw_value is None or raw_value == "" or raw_value == []:
        return []

    parsed_value = raw_value
    if isinstance(raw_value, str):
        try:
            parsed_value = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed_value = [item.strip() for item in raw_value.split(",")]

    if not isinstance(parsed_value, list):
        raise ValueError("Excluded customers must be provided as a list.")

    base_terms = {normalize_customer_term(value) for value in GUNS_BASE_EXCLUDED_CUSTOMERS}
    normalized_terms: list[str] = []
    seen_terms: set[str] = set()

    for item in parsed_value:
        if not isinstance(item, str):
            raise ValueError("Excluded customers must be text values.")
        normalized = normalize_customer_term(item)
        if not normalized or normalized in base_terms or normalized in seen_terms:
            continue
        normalized_terms.append(normalized)
        seen_terms.add(normalized)

    if len(normalized_terms) > GUNS_MAX_ADDITIONAL_CUSTOMERS:
        raise ValueError(
            f"You can exclude up to {GUNS_MAX_ADDITIONAL_CUSTOMERS} additional customers."
        )

    return normalized_terms


def build_guns_query_options(
    raw_lookahead_days: Any = None,
    raw_additional_customers: Any = None,
) -> dict[str, Any]:
    lookahead_days = parse_guns_lookahead_days(raw_lookahead_days)
    additional_customers = parse_additional_excluded_customers(raw_additional_customers)
    excluded_customers = [*GUNS_BASE_EXCLUDED_CUSTOMERS, *additional_customers]
    return {
        "lookahead_days": lookahead_days,
        "base_excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "additional_excluded_customers": additional_customers,
        "excluded_customers": excluded_customers,
        "has_overrides": (
            lookahead_days != GUNS_DEFAULT_LOOKAHEAD_DAYS
            or bool(additional_customers)
        ),
    }


def parse_query_run_options(query_type: str, payload: Any) -> dict[str, Any]:
    if query_type != "guns":
        return {}

    raw_additional_customers = payload.get("guns_excluded_customers")
    if raw_additional_customers is None or raw_additional_customers == "":
        raw_additional_customers = payload.get("guns_excluded_customers_json")

    return build_guns_query_options(
        raw_lookahead_days=payload.get("guns_lookahead_days"),
        raw_additional_customers=raw_additional_customers,
    )


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


def sql_quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_guns_query(
    query_template: str,
    query_options: Optional[dict[str, Any]] = None,
) -> str:
    resolved_options = get_default_guns_query_options()
    if query_options:
        resolved_options = {**resolved_options, **query_options}

    excluded_customer_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(customer)} AS CUSTOMER_TERM"
        for customer in resolved_options["excluded_customers"]
    )
    return (
        query_template.replace(GUNS_LOOKAHEAD_TOKEN, str(resolved_options["lookahead_days"]))
        .replace(GUNS_EXCLUDED_CUSTOMERS_TOKEN, excluded_customer_rows)
    )


def load_query(query_type: str, query_options: Optional[dict[str, Any]] = None) -> str:
    query_file = QUERY_FILES[query_type]
    if not query_file.exists():
        raise FileNotFoundError(f"Query file not found at: {query_file}")
    query_template = query_file.read_text(encoding="utf-8")
    if query_type == "guns":
        return render_guns_query(query_template, query_options=query_options)
    return query_template


# One pooled engine per connection string for the process lifetime: building
# an engine per query paid engine construction plus a fresh ODBC login on
# every request. Keyed by connection string so a settings change takes effect
# without a restart.
_erp_engines: dict[str, Any] = {}
_erp_engines_lock = threading.Lock()


def get_erp_engine():
    mssql_conn_string = get_config_value(
        setting_key="mssql_connection_string",
        env_key="MSSQL_CONNECTION_STRING",
    )
    if not mssql_conn_string:
        raise ValueError(
            "MSSQL_CONNECTION_STRING is not set. Configure it in Settings or in your .env file."
        )
    with _erp_engines_lock:
        engine = _erp_engines.get(mssql_conn_string)
        if engine is None:
            logger.info(
                "Connecting to SQL Server using %s",
                mask_connection_string(mssql_conn_string),
            )
            engine = create_engine(mssql_conn_string, pool_pre_ping=True, pool_recycle=1800)
            _erp_engines[mssql_conn_string] = engine
    return engine


def fetch_picklist_from_mssql(
    query_type: str,
    query_options: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    query = load_query(query_type, query_options=query_options)
    if query_type == "guns":
        applied_options = get_default_guns_query_options()
        if query_options:
            applied_options = {**applied_options, **query_options}
        logger.info(
            "Using guns query options: lookahead_days=%s excluded_customers=%s",
            applied_options["lookahead_days"],
            ",".join(applied_options["excluded_customers"]),
        )
    engine = get_erp_engine()
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
        logger.info("Picklist query returned %d rows.", len(df.index))
        return df


def run_audit_sql_file(query_file: Path, description: str) -> pd.DataFrame:
    """Run one of the serialized-audit SQL files (no parameters) against SQL Server.

    Sent as a raw string, not sqlalchemy.text(), so literal colons in the file
    can never be misparsed as bind parameters.
    """
    if not query_file.exists():
        raise FileNotFoundError(f"Audit query file not found at: {query_file}")
    query = query_file.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running %s", description)
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
        logger.info("%s returned %d rows.", description, len(df.index))
        return df


def run_erp_query_file(query_file: Path, params: dict, description: str) -> pd.DataFrame:
    """Run a parameterized read-only ERP query from a .sql file.

    Unlike run_audit_sql_file, user-supplied values go in as bound parameters
    (:name placeholders via sqlalchemy.text) — never string interpolation.
    """
    if not query_file.exists():
        raise FileNotFoundError(f"ERP query file not found at: {query_file}")
    query = query_file.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running %s", description)
    with engine.connect() as connection:
        df = pd.read_sql_query(text(query), connection, params=params)
        logger.info("%s returned %d rows.", description, len(df.index))
        return df


def fetch_audit_expected() -> pd.DataFrame:
    """Full serialized expected-inventory universe (all locations + tied WOs)."""
    return run_audit_sql_file(AUDIT_QUERY_FILE, "serialized audit expected query")


# Guards against overlapping background syncs when several dashboard requests
# cross the TTL at once.
_audit_sync_running = threading.Lock()


def _run_audit_location_sync() -> None:
    df = run_audit_sql_file(AUDIT_LOCATIONS_SYNC_FILE, "audit location sync query")
    audit_store.sync_locations(df.to_dict(orient="records"))


def _background_audit_location_sync() -> None:
    try:
        _run_audit_location_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Background audit location sync failed; keeping stored locations. (%s)", exc)
    finally:
        _audit_sync_running.release()


def sync_audit_locations_from_erp(force: bool = False) -> Optional[str]:
    """Refresh the auditable-location list from the ERP if stale.

    The routine TTL refresh runs in a background thread so no page render
    blocks on the ERP aggregation — the triggering request serves the stored
    list. Two cases run synchronously and return an error message on failure:
    force=True (the dashboard's "Refresh now" link) and a store that has never
    synced (there is nothing stored to show yet).
    """
    if not audit_store.is_available():
        return None
    never_synced = audit_store.last_synced_at() is None
    if force or never_synced:
        try:
            _run_audit_location_sync()
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Audit location sync failed; showing stored locations. (%s)", exc)
            return f"Could not refresh locations from the ERP: {exc}"
    if not audit_store.needs_sync(AUDIT_LOCATION_SYNC_MAX_AGE_MINUTES):
        return None
    if _audit_sync_running.acquire(blocking=False):
        threading.Thread(
            target=_background_audit_location_sync,
            name="audit-location-sync",
            daemon=True,
        ).start()
    return None


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


def _execute_picklist_run_core(
    query_type: str,
    query_options: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    started_at = time.perf_counter()
    run_timestamp = datetime.utcnow()

    try:
        df = fetch_picklist_from_mssql(
            query_type,
            query_options=query_options,
        )
        run_id = save_run(
            df=df,
            status="success",
            query_type=query_type,
            run_timestamp=run_timestamp,
        )
        try:
            save_plan_snapshot(
                run_id=run_id,
                query_type=query_type,
                run_timestamp=run_timestamp,
                rows=df.to_dict(orient="records"),
            )
        except Exception as exc:  # noqa: BLE001 — recon snapshot must never fail the run
            logger.exception("Failed to save plan snapshot for run %s: %s", run_id, exc)
        export_path = generate_export(
            df=df,
            run_id=run_id,
            query_type=query_type,
            run_timestamp=run_timestamp,
        )
        reporting_event_counted = record_reporting_event(
            source_run_id=run_id,
            query_type=query_type,
            event_timestamp=run_timestamp,
        )
        elapsed_seconds = time.perf_counter() - started_at

        message = (
            f"✅ Picklist run {run_id} ({query_type}) succeeded with {len(df.index)} rows "
            f"in {elapsed_seconds:.2f}s."
        )
        if reporting_event_counted:
            message += (
                f" Counted toward reporting metrics "
                f"({ESTIMATED_MINUTES_SAVED_PER_RUN} minutes saved estimated)."
            )
        if query_type == "components":
            # The picklist only shows what CAN pick — put the shortfall note
            # in the same notification so transfers get requested while the
            # day is young. Never let this extra ERP call fail the run.
            try:
                shortage_payload = build_shortage_payload()
                shortage_summary = shortage_payload.get("summary")
                if not shortage_payload.get("error") and shortage_summary and (
                    shortage_summary["transfer_units"] or shortage_summary["stockout_units"]
                ):
                    message += (
                        f" Shortage check: {shortage_summary['transfer_units']} open units "
                        f"need a transfer from MAIN and {shortage_summary['stockout_units']} "
                        f"have no stock anywhere — move list on the Shipping page."
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Shortage note for run notification failed: %s", exc)
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
            query_type=query_type,
            run_timestamp=run_timestamp,
            error_message=str(exc),
        )
        message = (
            f"❌ Picklist run {run_id} ({query_type}) failed "
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


def execute_picklist_run(
    query_type: Optional[str] = None,
    query_options: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    normalized_query_type = get_query_type(query_type)
    if not try_mark_run_started(normalized_query_type):
        logger.info(
            "Skipped picklist run for %s because another run is already active.",
            normalized_query_type,
        )
        return None

    try:
        return _execute_picklist_run_core(
            normalized_query_type,
            query_options=query_options,
        )
    finally:
        mark_run_finished(normalized_query_type)


def start_picklist_run_async(
    query_type: Optional[str] = None,
    query_options: Optional[dict[str, Any]] = None,
) -> bool:
    normalized_query_type = get_query_type(query_type)
    if not try_mark_run_started(normalized_query_type):
        logger.info(
            "Skipped background picklist run for %s because another run is already active.",
            normalized_query_type,
        )
        return False

    background_query_options = dict(query_options or {})

    def run_in_background() -> None:
        try:
            _execute_picklist_run_core(
                normalized_query_type,
                query_options=background_query_options,
            )
        finally:
            mark_run_finished(normalized_query_type)

    thread = threading.Thread(
        target=run_in_background,
        name=f"picklist-run-{normalized_query_type}",
        daemon=True,
    )
    thread.start()
    return True


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


def validate_optional_chat_id(chat_id: str) -> Optional[str]:
    if chat_id and not re.fullmatch(r"-?\d+", chat_id):
        return "Chat ID must be numeric (optional leading -)."
    return None


def validate_optional_port(port_value: str) -> Optional[str]:
    if not port_value:
        return None
    if not re.fullmatch(r"\d+", port_value):
        return "SMTP port must be a number between 1 and 65535."
    port = int(port_value)
    if port < 1 or port > 65535:
        return "SMTP port must be a number between 1 and 65535."
    return None


def email_address_is_valid(address: str) -> bool:
    return bool(re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", address))


def validate_optional_email(address: str, label: str) -> Optional[str]:
    if address and not email_address_is_valid(address):
        return f"{label} must be a valid email address."
    return None


def validate_optional_recipient_list(recipients_raw: str) -> Optional[str]:
    recipients = parse_recipient_addresses(recipients_raw)
    if recipients and not recipients_are_valid(recipients):
        invalid_recipient = next(
            (address for address in recipients if not email_address_is_valid(address)),
            recipients[0],
        )
        return f"Invalid email address: {invalid_recipient}"
    return None


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


def check_audit_universe_sql() -> None:
    """Warn when an audit SQL file has drifted from the canonical bin universe.

    The filter is hardcoded in each file (they must stay runnable in SSMS);
    this catches the add-a-cage-but-miss-a-file mistake at boot instead of as
    silently wrong audit results.
    """
    for sql_file in (AUDIT_QUERY_FILE, AUDIT_LOCATIONS_SYNC_FILE, AUDIT_DWELL_QUERY_FILE):
        try:
            missing = audit_universe.missing_terms(sql_file.read_text(encoding="utf-8"))
        except OSError:
            continue  # missing file surfaces later with a clear error of its own
        if missing:
            logger.warning(
                "%s is missing audited-universe terms (%s) — update it to match audit_universe.py.",
                sql_file.name,
                ", ".join(missing),
            )


initialize_db()
migrate_sensitive_settings_encryption()
audit_store.initialize()
pick_store.initialize(get_sqlite_conn)
backfill_plan_snapshots()
check_audit_universe_sql()
start_scheduler()


@app.route("/")
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
    )


@app.route("/settings", methods=["GET", "POST"])
@require_trusted_client
def settings():
    reporting_metrics = get_reporting_metrics()

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

        telegram_chat_id = (request.form.get("telegram_chat_id") or "").strip()
        smtp_port = (request.form.get("smtp_port") or "").strip()
        smtp_sender = (request.form.get("smtp_sender") or "").strip()
        smtp_recipient = (request.form.get("smtp_recipient") or "").strip()

        validation_error = (
            validate_optional_chat_id(telegram_chat_id)
            or validate_optional_port(smtp_port)
            or validate_optional_email(smtp_sender, "SMTP sender")
            or validate_optional_recipient_list(smtp_recipient)
        )
        if validation_error:
            flash(validation_error, "error")
            return redirect(url_for("settings"))

        mssql_connection_string = (request.form.get("mssql_connection_string") or "").strip()
        if request.form.get("clear_mssql_connection_string"):
            delete_setting("mssql_connection_string")
        elif mssql_connection_string:
            set_setting("mssql_connection_string", mssql_connection_string)

        telegram_bot_token = (request.form.get("telegram_bot_token") or "").strip()
        if request.form.get("clear_telegram_bot_token"):
            delete_setting("telegram_bot_token")
        elif telegram_bot_token:
            set_setting("telegram_bot_token", telegram_bot_token)

        if telegram_chat_id:
            set_setting("telegram_chat_id", telegram_chat_id)
        else:
            delete_setting("telegram_chat_id")

        smtp_host = (request.form.get("smtp_host") or "").strip()
        if smtp_host:
            set_setting("smtp_host", smtp_host)
        else:
            delete_setting("smtp_host")

        if smtp_port:
            set_setting("smtp_port", smtp_port)
        else:
            delete_setting("smtp_port")

        smtp_user = (request.form.get("smtp_user") or "").strip()
        if smtp_user:
            set_setting("smtp_user", smtp_user)
        else:
            delete_setting("smtp_user")

        smtp_password = (request.form.get("smtp_password") or "").strip()
        if request.form.get("clear_smtp_password"):
            delete_setting("smtp_password")
        elif smtp_password:
            set_setting("smtp_password", smtp_password)

        if smtp_sender:
            set_setting("smtp_sender", smtp_sender)
        else:
            delete_setting("smtp_sender")

        if smtp_recipient:
            set_setting("smtp_recipient", smtp_recipient)
        else:
            delete_setting("smtp_recipient")

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
            reporting_metrics=reporting_metrics,
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
        reporting_metrics=reporting_metrics,
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
    try:
        query_options = parse_query_run_options(query_type, request.form)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index", query_type=query_type))

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


# ---------------------------------------------------------------------------
# Serialized inventory audit
# ---------------------------------------------------------------------------
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


def _audit_unavailable_response():
    """Consistent handling when the Postgres audit store is not configured."""
    if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503
    flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
    return render_template(
        "audit.html",
        audit_available=False,
        warehouses=[],
        tied_row=None,
        recent_sessions=[],
        due_locations=[],
        sync_error=None,
        last_synced_display=None,
    )


AUDIT_UNAVAILABLE_MESSAGE = (
    "The serialized audit feature is unavailable because the Postgres store "
    "(DATABASE_URL) is not configured or could not be reached."
)


@app.get("/audit")
@require_trusted_client
def audit_dashboard():
    if not audit_store.is_available():
        return _audit_unavailable_response()

    sync_error = sync_audit_locations_from_erp(force=request.args.get("sync") == "1")

    locations = audit_store.list_location_status()
    tied_row = None
    by_warehouse: dict[str, list[dict]] = {}
    for loc in locations:
        loc["last_inventoried_display"] = _audit_dt_display(loc.get("last_inventoried"))
        if loc["scope"] == audit_store.TIED_WO_SCOPE:
            tied_row = loc
        else:
            by_warehouse.setdefault(loc["warehouse_id"], []).append(loc)
    warehouses = [
        {"warehouse_id": wh, "locations": locs, "serial_total": sum(l["serial_count"] for l in locs)}
        for wh, locs in sorted(by_warehouse.items())
    ]

    recent = audit_store.recent_sessions(limit=10)
    for row in recent:
        row["started_display"] = _audit_dt_display(row.get("started_at"))
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))

    due_locations = [loc for loc in locations if loc.get("due")]
    return render_template(
        "audit.html",
        audit_available=True,
        warehouses=warehouses,
        tied_row=tied_row,
        recent_sessions=recent,
        due_locations=due_locations,
        sync_error=sync_error,
        last_synced_display=_audit_dt_display(audit_store.last_synced_at()),
    )


@app.post("/audit/session/start")
@require_trusted_client
@require_csrf
def audit_session_start():
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit_dashboard"))

    try:
        target = audit_store.build_target(
            kind=request.form.get("target_kind"),
            warehouse=request.form.get("warehouse"),
            location=request.form.get("location"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("audit_dashboard"))

    if not audit_store.target_exists(target):
        flash(
            f"Unknown audit location: {target['label']}. "
            "Refresh the dashboard and try again.",
            "error",
        )
        return redirect(url_for("audit_dashboard"))

    operator = (request.form.get("operator") or "").strip() or None

    try:
        df = fetch_audit_expected()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load audit expected list: %s", exc)
        flash(f"Could not load the expected serial list: {exc}", "error")
        return redirect(url_for("audit_dashboard"))

    expected_rows = df.to_dict(orient="records") if not df.empty else []
    try:
        session_id = audit_store.start_session(target, expected_rows, operator)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start audit session: %s", exc)
        flash(f"Could not start the audit session: {exc}", "error")
        return redirect(url_for("audit_dashboard"))

    logger.info(
        "Started audit session #%s (%s, %d serials snapshotted).",
        session_id,
        target["label"],
        len(expected_rows),
    )
    return redirect(url_for("audit_session_page", session_id=session_id))


@app.get("/audit/session/<int:session_id>")
@require_trusted_client
def audit_session_page(session_id: int):
    if not audit_store.is_available():
        return _audit_unavailable_response()

    session_row = audit_store.get_session(session_id)
    if not session_row:
        flash(f"Audit session #{session_id} was not found.", "error")
        return redirect(url_for("audit_dashboard"))

    items = audit_store.get_expected_items(session_id)
    for item in items:
        item["scanned_at_display"] = _audit_dt_display(item.get("scanned_at"))
    unexpected = audit_store.get_unexpected_scans(session_id)
    for row in unexpected:
        row["first_scanned_display"] = _audit_dt_display(row.get("first_scanned_at"))
    counts = audit_store.compute_counts(session_id)

    return render_template(
        "audit_session.html",
        session=session_row,
        session_scope_label=session_row.get("label")
        or session_row.get("scope")
        or "Full audit (all locations)",
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        items=items,
        unexpected=unexpected,
        counts=counts,
        known_location_ids=audit_store.list_active_location_ids(),
        ui_refresh_interval_seconds=UI_REFRESH_INTERVAL_SECONDS,
    )


@app.post("/api/audit/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_audit_scan(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify({"error": "completed", "message": "This audit session is already completed."}), 409

    payload = request.get_json(silent=True) or {}
    serial = (payload.get("serial") or "").strip()
    location = (payload.get("location") or "").strip()
    operator = (payload.get("operator") or "").strip() or None
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400

    try:
        result = audit_store.record_scan(session_id, serial, location, operator)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record audit scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    return jsonify(
        {
            "result": result["result"],
            "is_duplicate": result["is_duplicate"],
            "item": result["item"],
            "counts": result["counts"],
            "serial": serial,
            "location": location,
        }
    ), 200


@app.post("/api/audit/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_audit_complete(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    try:
        completed = audit_store.complete_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to complete audit session: %s", exc)
        return jsonify({"error": "complete_failed", "message": str(exc)}), 500

    logger.info("Completed audit session #%s.", session_id)
    return jsonify(
        {
            "status": "completed",
            "session": {
                "id": completed["id"],
                "accuracy_pct": float(completed["accuracy_pct"]) if completed.get("accuracy_pct") is not None else None,
                "expected_count": completed["expected_count"],
                "verified_count": completed["verified_count"],
                "misplaced_count": completed["misplaced_count"],
                "missing_count": completed["missing_count"],
                "unexpected_count": completed["unexpected_count"],
            },
            "redirect": url_for("audit_session_page", session_id=session_id),
        }
    ), 200


@app.get("/api/audit/session/<int:session_id>/state")
@require_trusted_client
def api_audit_state(session_id: int):
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    return jsonify(
        {
            "status": session_row.get("status"),
            "counts": audit_store.compute_counts(session_id),
        }
    ), 200


@app.get("/audit/session/<int:session_id>/export")
@require_trusted_client
def audit_session_export(session_id: int):
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit_dashboard"))

    session_row = audit_store.get_session(session_id)
    if not session_row:
        flash(f"Audit session #{session_id} was not found.", "error")
        return redirect(url_for("audit_dashboard"))

    items = audit_store.get_expected_items(session_id)
    unexpected = audit_store.get_unexpected_scans(session_id)

    expected_df = pd.DataFrame(items)
    unexpected_df = pd.DataFrame(unexpected)
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (expected_df if not expected_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Expected"
        )
        (unexpected_df if not unexpected_df.empty else pd.DataFrame(columns=["scanned_serial"])).to_excel(
            writer, index=False, sheet_name="Unexpected"
        )
    output.seek(0)
    scope_label = (session_row.get("scope") or "ALL").replace("/", "-")
    filename = f"audit_{scope_label}_session{session_id}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# Accuracy every completed audit is held against on the analytics page.
AUDIT_ACCURACY_TARGET_PCT = float(os.getenv("AUDIT_ACCURACY_TARGET_PCT", "99"))
AUDIT_ANALYTICS_DEFAULT_DAYS = 30

# Dwell time: guns should clear the staging bins within this many hours.
AUDIT_DWELL_TARGET_HOURS = float(os.getenv("AUDIT_DWELL_TARGET_HOURS", "24"))
# Warehouse/location pairs the clearance metric watches.
AUDIT_DWELL_LOCATIONS = [
    tuple(pair.strip().upper().split("/", 1))
    for pair in os.getenv(
        "AUDIT_DWELL_LOCATIONS",
        "MAIN/C2-SERIALIZED,SHIPPING/STAGE,SHIPPING/STAGE1,SHIPPING/STAGE2,SHIPPING/STAGE3",
    ).split(",")
    if "/" in pair
]
AUDIT_DWELL_CACHE_MINUTES = float(os.getenv("AUDIT_DWELL_CACHE_MINUTES", "10"))
_dwell_cache: dict[str, Any] = {"df": None, "fetched_at": None}


def _dwell_age_display(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f} d"
    return f"{hours:.0f} h"


def _fetch_dwell_raw() -> pd.DataFrame:
    """Dwell rows for the whole serialized universe, cached briefly (ERP ~5s).

    The dwell SQL returns every SHIPPING location plus the MAIN cage bins;
    both the audit dwell metric and shipping stage aging filter this one
    cached snapshot. dwell_hours is computed against the ERP server's own
    clock (ERP_NOW), which is on the same clock as CREATE_DATE — the app
    host's timezone never enters into it. Ages drift up to the cache TTL;
    the pages show the snapshot time.
    """
    cached_at = _dwell_cache["fetched_at"]
    if (
        _dwell_cache["df"] is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=AUDIT_DWELL_CACHE_MINUTES)
    ):
        return _dwell_cache["df"]
    df = run_audit_sql_file(AUDIT_DWELL_QUERY_FILE, "serialized dwell-time query")
    df["ARRIVED_AT"] = pd.to_datetime(df["ARRIVED_AT"])
    df["ERP_NOW"] = pd.to_datetime(df["ERP_NOW"])
    df["dwell_hours"] = (
        (df["ERP_NOW"] - df["ARRIVED_AT"]).dt.total_seconds() / 3600.0
    ).clip(lower=0.0)
    _dwell_cache["df"] = df
    _dwell_cache["fetched_at"] = datetime.now()
    return df


def fetch_dwell_df() -> pd.DataFrame:
    """Dwell rows filtered to the audit-watched staging locations."""
    df = _fetch_dwell_raw()
    watched = {f"{wh}/{loc}" for wh, loc in AUDIT_DWELL_LOCATIONS}
    keys = (
        df["WAREHOUSE_ID"].astype(str).str.upper()
        + "/"
        + df["LOCATION_ID"].astype(str).str.upper()
    )
    return df[keys.isin(watched)].copy()


def build_dwell_metrics() -> dict[str, Any]:
    """Snapshot of how long guns have sat in the watched staging locations.

    Unlike the rest of the analytics payload this is a *current* snapshot from
    the ERP, not a windowed aggregate. Failure to reach the ERP degrades to an
    error message so the audit analytics (Postgres-backed) still render.
    """
    target = AUDIT_DWELL_TARGET_HOURS
    result: dict[str, Any] = {
        "target_hours": target,
        "locations": [f"{wh}/{loc}" for wh, loc in AUDIT_DWELL_LOCATIONS],
        "error": None,
    }
    try:
        df = fetch_dwell_df()
    except Exception as exc:  # ERP down/misconfigured — degrade, don't 500
        logger.exception("Dwell-time query failed")
        result["error"] = str(exc)
        return result
    result["as_of"] = (
        df["ERP_NOW"].max().to_pydatetime() if len(df) else datetime.now()
    )

    over = df[df["dwell_hours"] > target].sort_values("ARRIVED_AT")

    total = int(len(df))
    over_count = int(len(over))
    result["summary"] = {
        "on_hand": total,
        "within_target": total - over_count,
        "over_target": over_count,
        "clearance_pct": round(100.0 * (total - over_count) / total, 1) if total else None,
        "median_dwell_hours": round(float(df["dwell_hours"].median()), 1) if total else None,
        "oldest_dwell_hours": round(float(df["dwell_hours"].max()), 1) if total else None,
    }

    by_location = []
    for label in result["locations"]:
        wh, loc = label.split("/", 1)
        sub = df[
            (df["WAREHOUSE_ID"].str.upper() == wh) & (df["LOCATION_ID"].str.upper() == loc)
        ]
        loc_over = int((sub["dwell_hours"] > target).sum())
        oldest_hours = round(float(sub["dwell_hours"].max()), 1) if len(sub) else None
        by_location.append(
            {
                "location": label,
                "on_hand": int(len(sub)),
                "over_target": loc_over,
                "oldest_dwell_hours": oldest_hours,
                "oldest_display": (
                    _dwell_age_display(oldest_hours) if oldest_hours is not None else None
                ),
            }
        )
    result["by_location"] = by_location

    result["aged_serials"] = [
        {
            "serial": row["SERIAL_NO"],
            "part_id": row["PART_ID"],
            "part_description": row["PART_DESCRIPTION"],
            "location": f"{row['WAREHOUSE_ID']}/{row['LOCATION_ID']}",
            "arrived_at": row["ARRIVED_AT"].to_pydatetime(),
            "dwell_hours": round(float(row["dwell_hours"]), 1),
            "age_display": _dwell_age_display(float(row["dwell_hours"])),
        }
        for _, row in over.iterrows()
    ]
    return result


def _audit_json_safe(value):
    """Recursively convert analytics rows to JSON-friendly types."""
    if isinstance(value, dict):
        return {k: _audit_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _audit_analytics_window() -> int:
    try:
        days = int(request.args.get("days", AUDIT_ANALYTICS_DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = AUDIT_ANALYTICS_DEFAULT_DAYS
    return max(1, min(days, 365))


def build_audit_analytics(days: int) -> dict[str, Any]:
    """Everything the analytics dashboard / endpoint reports for the window."""
    sessions = audit_store.completed_sessions_since(days)
    serials_audited = sum(r.get("expected_count") or 0 for r in sessions)
    verified = sum(r.get("verified_count") or 0 for r in sessions)
    misplaced = sum(r.get("misplaced_count") or 0 for r in sessions)
    missing = sum(r.get("missing_count") or 0 for r in sessions)
    unexpected = sum(r.get("unexpected_count") or 0 for r in sessions)
    weighted_accuracy = (
        round(100.0 * verified / serials_audited, 2) if serials_audited else None
    )
    session_accuracies = [
        float(r["accuracy_pct"]) for r in sessions if r.get("accuracy_pct") is not None
    ]
    avg_session_accuracy = (
        round(sum(session_accuracies) / len(session_accuracies), 2)
        if session_accuracies
        else None
    )

    locations = audit_store.list_location_status()
    locations_active = len(locations)
    locations_due = sum(1 for loc in locations if loc.get("due"))
    cadence_compliance = (
        round(100.0 * (locations_active - locations_due) / locations_active, 1)
        if locations_active
        else None
    )

    open_exceptions = audit_store.current_exceptions()
    open_missing = sum(1 for r in open_exceptions if r.get("status") == "missing")
    open_misplaced = sum(1 for r in open_exceptions if r.get("status") == "misplaced")

    return {
        "window_days": days,
        "target_accuracy_pct": AUDIT_ACCURACY_TARGET_PCT,
        "generated_at": datetime.now(timezone.utc),
        "summary": {
            "sessions_completed": len(sessions),
            "serials_audited": serials_audited,
            "verified": verified,
            "misplaced": misplaced,
            "missing": missing,
            "unexpected": unexpected,
            "weighted_accuracy_pct": weighted_accuracy,
            "avg_session_accuracy_pct": avg_session_accuracy,
            "meets_target": (
                weighted_accuracy is not None
                and weighted_accuracy >= AUDIT_ACCURACY_TARGET_PCT
            ),
            "locations_active": locations_active,
            "locations_due": locations_due,
            "cadence_compliance_pct": cadence_compliance,
            "open_missing": open_missing,
            "open_misplaced": open_misplaced,
        },
        "sessions": sessions,
        "open_exceptions": open_exceptions,
        "problem_locations": audit_store.location_error_breakdown(days),
        "problem_serials": audit_store.repeat_offender_serials(days),
        "unexpected_serials": audit_store.unexpected_serials_since(days),
        "dwell": build_dwell_metrics(),
    }


@app.get("/audit/analytics")
@require_trusted_client
def audit_analytics_page():
    if not audit_store.is_available():
        return _audit_unavailable_response()

    days = _audit_analytics_window()
    payload = build_audit_analytics(days)

    # Human-readable timestamps for the tables (charts use the ISO values).
    for row in payload["sessions"]:
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    for row in payload["open_exceptions"]:
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    for row in payload["problem_locations"]:
        row["last_audited_display"] = _audit_dt_display(row.get("last_audited_at"))
    for row in payload["problem_serials"]:
        row["last_audited_display"] = _audit_dt_display(row.get("last_audited_at"))
    for row in payload["unexpected_serials"]:
        row["last_scanned_display"] = _audit_dt_display(row.get("last_scanned_at"))
    # ERP timestamps are company-local naive — format without tz conversion.
    for row in payload["dwell"].get("aged_serials", []):
        row["arrived_display"] = row["arrived_at"].strftime("%Y-%m-%d %H:%M")

    return render_template(
        "audit_analytics.html",
        audit_available=True,
        data=_audit_json_safe(payload),
        window_options=[7, 30, 90, 365],
    )


@app.get("/api/audit/analytics")
@require_trusted_client
def api_audit_analytics():
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503
    return jsonify(_audit_json_safe(build_audit_analytics(_audit_analytics_window()))), 200


# ---------------------------------------------------------------------------
# Serial number history
# ---------------------------------------------------------------------------
def _clean_serial_input(raw: Optional[str]) -> str:
    return (raw or "").strip().upper()


@app.get("/serial-history")
@require_trusted_client
def serial_history_page():
    prefill = _clean_serial_input(request.args.get("serial"))[:SERIAL_MAX_LENGTH]
    return render_template("serial_history.html", prefill_serial=prefill)


@app.get("/api/serial-history")
@require_trusted_client
def api_serial_history():
    serial = _clean_serial_input(request.args.get("serial"))
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400
    if len(serial) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Serial number is too long."}), 400

    try:
        trace_df = run_erp_query_file(
            SERIAL_HISTORY_TRACE_FILE, {"serial": serial}, "serial history trace lookup"
        )
        if trace_df.empty:
            txns_df = trace_df
            shipments_df = trace_df
        else:
            txns_df = run_erp_query_file(
                SERIAL_HISTORY_TRANSACTIONS_FILE, {"serial": serial}, "serial history transactions"
            )
            shipments_df = run_erp_query_file(
                SERIAL_HISTORY_SHIPMENTS_FILE, {"serial": serial}, "serial history shipments"
            )
    except Exception:
        logger.exception("Serial history lookup failed for %s", serial)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )

    payload = serial_history.build_serial_history(serial, trace_df, txns_df, shipments_df)
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Shipping ops: end-of-day reconciliation + stage aging + pick confirm
# ---------------------------------------------------------------------------
def build_stage_aging() -> dict[str, Any]:
    """How long inventory has been sitting in the SHIPPING stage bins.

    Same live-ERP snapshot the audit dwell metric uses (one cached query),
    but scoped to every SHIPPING location whose ID contains
    STAGE_LOCATION_TERM — staged goods are sold and boxed, so anything aging
    here is an order that has not actually left.
    """
    target = STAGE_AGING_TARGET_HOURS
    result: dict[str, Any] = {
        "target_hours": target,
        "term": STAGE_LOCATION_TERM,
        "error": None,
    }
    try:
        raw = _fetch_dwell_raw()
    except Exception as exc:  # ERP down/misconfigured — degrade, don't 500
        logger.exception("Stage aging query failed")
        result["error"] = str(exc)
        return result

    mask = (
        raw["WAREHOUSE_ID"].astype(str).str.upper().eq("SHIPPING")
        & raw["LOCATION_ID"].astype(str).str.upper().str.contains(
            STAGE_LOCATION_TERM, regex=False
        )
    )
    df = raw[mask].copy()
    result["as_of"] = (
        raw["ERP_NOW"].max().to_pydatetime() if len(raw) else datetime.now()
    )

    total = int(len(df))
    over = df[df["dwell_hours"] > target].sort_values("ARRIVED_AT")
    over_count = int(len(over))
    result["summary"] = {
        "on_hand": total,
        "within_target": total - over_count,
        "over_target": over_count,
        "clearance_pct": round(100.0 * (total - over_count) / total, 1) if total else None,
        "median_dwell_hours": round(float(df["dwell_hours"].median()), 1) if total else None,
        "oldest_dwell_hours": round(float(df["dwell_hours"].max()), 1) if total else None,
    }

    by_location = []
    if total:
        for location_id, sub in df.groupby(df["LOCATION_ID"].astype(str).str.upper()):
            loc_over = int((sub["dwell_hours"] > target).sum())
            oldest_hours = round(float(sub["dwell_hours"].max()), 1)
            by_location.append(
                {
                    "location": f"SHIPPING/{location_id}",
                    "on_hand": int(len(sub)),
                    "over_target": loc_over,
                    "oldest_dwell_hours": oldest_hours,
                    "oldest_display": _dwell_age_display(oldest_hours),
                }
            )
        by_location.sort(key=lambda row: row["location"])
    result["by_location"] = by_location

    result["aged_serials"] = [
        {
            "serial": row["SERIAL_NO"],
            "part_id": row["PART_ID"],
            "part_description": row["PART_DESCRIPTION"],
            "location": f"{row['WAREHOUSE_ID']}/{row['LOCATION_ID']}",
            "arrived_at": row["ARRIVED_AT"].to_pydatetime(),
            "arrived_display": row["ARRIVED_AT"].strftime("%Y-%m-%d %H:%M"),
            "dwell_hours": round(float(row["dwell_hours"]), 1),
            "age_display": _dwell_age_display(float(row["dwell_hours"])),
        }
        for _, row in over.iterrows()
    ]
    return result


_shortage_cache: dict[str, Any] = {"payload": None, "fetched_at": None}


def _fetch_shortage_rows() -> list[dict]:
    """Line-level shortage demand from the ERP.

    The product-code list is rendered into the SQL as quoted literals (same
    token pattern as the guns excluded-customers list) because an IN-list
    cannot be a bind parameter; the lookahead is a normal bound value.
    """
    if not SHORTAGE_QUERY_FILE.exists():
        raise FileNotFoundError(f"Shortage query file not found at: {SHORTAGE_QUERY_FILE}")
    template = SHORTAGE_QUERY_FILE.read_text(encoding="utf-8")
    product_code_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(code)} AS PRODUCT_CODE"
        for code in SHORTAGE_PRODUCT_CODES
    )
    query = template.replace(SHORTAGE_PRODUCT_CODES_TOKEN, product_code_rows)
    engine = get_erp_engine()
    logger.info("Running component shortage query")
    with engine.connect() as connection:
        df = pd.read_sql_query(
            text(query), connection, params={"lookahead_days": SHORTAGE_LOOKAHEAD_DAYS}
        )
        logger.info("Component shortage query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def build_shortage_payload(force: bool = False) -> dict[str, Any]:
    """Cached shortage payload; degrades to an error message when ERP is down."""
    cached_at = _shortage_cache["fetched_at"]
    if (
        not force
        and _shortage_cache["payload"] is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=SHORTAGE_CACHE_MINUTES)
    ):
        return _shortage_cache["payload"]
    try:
        rows = _fetch_shortage_rows()
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Component shortage query failed")
        return {
            "error": str(exc),
            "summary": None,
            "lines": [],
            "transfers": [],
            "lookahead_days": SHORTAGE_LOOKAHEAD_DAYS,
        }
    payload = shortage.build_shortage(rows, SHORTAGE_LOOKAHEAD_DAYS)
    payload["error"] = None
    payload["lookahead_days"] = SHORTAGE_LOOKAHEAD_DAYS
    payload["as_of"] = datetime.now(timezone.utc)
    _shortage_cache["payload"] = payload
    _shortage_cache["fetched_at"] = datetime.now()
    return payload


def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone(resolve_timezone()).date()


def _resolve_recon_date(requested: Optional[str], available: list[str]) -> Optional[str]:
    """Requested date if we have a plan for it; else the newest date before
    today (the 'how did yesterday go' default); else the newest we have."""
    if requested:
        requested = requested.strip()
        if requested in available:
            return requested
        return requested  # let the caller report "no plan for this date"
    today_iso = _today_local().isoformat()
    for plan_date in available:  # newest first
        if plan_date < today_iso:
            return plan_date
    return available[0] if available else None


def build_recon_payload(requested_date: Optional[str]) -> dict[str, Any]:
    """Reconciliation payload for the shipping page / API."""
    available = list_plan_dates()
    plan_date_iso = _resolve_recon_date(requested_date, available)
    payload: dict[str, Any] = {
        "available_dates": available,
        "plan_date": plan_date_iso,
        "error": None,
    }
    if not plan_date_iso:
        payload["error"] = "no_plans"
        payload["message"] = (
            "No picklist plans have been captured yet. Reconciliation starts "
            "working after the next successful picklist run."
        )
        return payload

    try:
        plan_day = date.fromisoformat(plan_date_iso)
    except ValueError:
        payload["error"] = "bad_date"
        payload["message"] = f"'{plan_date_iso}' is not a valid date."
        return payload

    plans = get_plan_snapshots(plan_date_iso)
    if not plans:
        payload["error"] = "no_plan_for_date"
        payload["message"] = f"No picklist plan was captured on {plan_date_iso}."
        return payload

    end_day = _today_local() + timedelta(days=1)
    try:
        shipments_df = run_erp_query_file(
            RECON_SHIPMENTS_FILE,
            {"start_date": plan_date_iso, "end_date": end_day.isoformat()},
            "shipping reconciliation shipments",
        )
    except Exception as exc:  # noqa: BLE001 — degrade like the dwell metric
        logger.exception("Reconciliation shipments query failed")
        payload["error"] = "erp_failed"
        payload["message"] = f"Could not load shipments from the ERP: {exc}"
        return payload

    result = recon.build_reconciliation(
        plan_day, plans, shipments_df.to_dict(orient="records")
    )
    result.update(payload)
    for qt, plan in result["plans"].items():
        plan["run_timestamp_display"] = (
            format_run_timestamp(plan["run_timestamp"]) if plan.get("run_timestamp") else None
        )
    return result


@app.get("/shipping")
@require_trusted_client
def shipping_page():
    recon_payload = build_recon_payload(request.args.get("date"))
    stage = build_stage_aging()

    sessions = pick_store.recent_sessions(limit=10)
    for row in sessions:
        row["started_display"] = _audit_dt_display(row.get("started_at"))
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))

    latest_success_by_type = {}
    for query_type in QUERY_FILES:
        summary = get_latest_successful_run_summary(query_type)
        latest_success_by_type[query_type] = (
            {
                "id": summary["id"],
                "row_count": summary["row_count"],
                "run_timestamp_display": format_run_timestamp(summary["run_timestamp"]),
            }
            if summary
            else None
        )

    return render_template(
        "shipping.html",
        recon=_audit_json_safe(recon_payload),
        stage=_audit_json_safe(stage),
        shortages=_audit_json_safe(build_shortage_payload()),
        pick_sessions=sessions,
        latest_success_by_type=latest_success_by_type,
        query_options=list(QUERY_FILES.keys()),
        today_iso=_today_local().isoformat(),
    )


@app.get("/api/shipping/shortages")
@require_trusted_client
def api_shipping_shortages():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_shortage_payload(force=force))), 200


@app.get("/shipping/shortages/export")
@require_trusted_client
def shipping_shortages_export():
    payload = build_shortage_payload()
    if payload.get("error"):
        flash(f"Shortage data is unavailable: {payload['error']}", "error")
        return redirect(url_for("shipping_page"))

    summary_df = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in (payload["summary"] or {}).items()]
    )
    transfers_df = pd.DataFrame([
        {
            "Part": t["part_id"],
            "Description": t["part_description"],
            "Product code": t["product_code"],
            "Qty needed": t["qty_needed"],
            "Stock locations": t["stock_locations"],
        }
        for t in payload["transfers"]
    ])
    lines_df = pd.DataFrame([
        {
            "Reason": l["reason"],
            "Past due": "yes" if l["past_due"] else "",
            "Desired ship": l["desired_ship_date"],
            "Customer": l.get("customer_name") or l.get("customer_id") or "",
            "Order": l["cust_order_id"],
            "Line": l["line_no"],
            "Part": l["part_id"],
            "Description": l["part_description"],
            "Open qty": l["open_qty"],
            "Will print": l["will_print_qty"],
            "Short": l["short_qty"],
            "Coverable by transfer": l["transfer_qty"],
            "No stock anywhere": l["stockout_qty"],
            "No ship-to on order": "yes" if l["shipto_missing"] else "",
            "Stock locations": l["stock_locations"],
        }
        for l in payload["lines"]
    ])

    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        (transfers_df if not transfers_df.empty else pd.DataFrame(columns=["Part"])).to_excel(
            writer, index=False, sheet_name="Transfer move list"
        )
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["Reason"])).to_excel(
            writer, index=False, sheet_name="Shortage lines"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"component_shortages_{_today_local().isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/shipping/recon")
@require_trusted_client
def api_shipping_recon():
    return jsonify(_audit_json_safe(build_recon_payload(request.args.get("date")))), 200


@app.get("/shipping/recon/export")
@require_trusted_client
def shipping_recon_export():
    payload = build_recon_payload(request.args.get("date"))
    if payload.get("error"):
        flash(payload.get("message") or "Reconciliation data is unavailable.", "error")
        return redirect(url_for("shipping_page"))

    summary = {k: v for k, v in payload["summary"].items() if k != "by_type"}
    summary_df = pd.DataFrame([{"metric": k, "value": v} for k, v in summary.items()])
    lines_df = pd.DataFrame([
        {
            "Status": l["status"],
            "Customer": l.get("customer_name") or l.get("customer_id") or "",
            "Order": l["cust_order_id"],
            "Part": l["part_id"],
            "Picklist": ", ".join(l["types"]),
            "Locations": ", ".join(l["locations"]),
            "Planned": l["planned_qty"],
            "Shipped same day": l["shipped_same_day"],
            "Shipped late": l["shipped_late"],
            "Voided qty": l["voided_qty"],
            "Packlists": ", ".join(str(p["packlist_id"]) for p in l["packlists"]),
            "Tracking": ", ".join(l["tracking"]),
        }
        for l in payload["lines"]
    ])
    unplanned_df = pd.DataFrame([
        {
            "Customer": u.get("customer_name") or u.get("customer_id") or "",
            "Order": u["cust_order_id"],
            "Part": u.get("part_id") or "",
            "Qty": u["qty"],
            "Packlists": ", ".join(str(p["packlist_id"]) for p in u["packlists"]),
            "Tracking": ", ".join(u["tracking"]),
        }
        for u in payload["unplanned"]
    ])

    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["Status"])).to_excel(
            writer, index=False, sheet_name="Planned lines"
        )
        (unplanned_df if not unplanned_df.empty else pd.DataFrame(columns=["Order"])).to_excel(
            writer, index=False, sheet_name="Unplanned"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"ship_recon_{payload['plan_date']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Pick confirm
# ---------------------------------------------------------------------------
@app.post("/pick/session/start")
@require_trusted_client
@require_csrf
def pick_session_start():
    query_type = get_query_type(request.form.get("query_type"))
    operator = (request.form.get("operator") or "").strip() or None

    run, rows = get_latest_successful_run(query_type=query_type)
    if not run:
        flash(
            f"No successful {query_type} picklist run to pick against. Run the picklist first.",
            "error",
        )
        return redirect(url_for("shipping_page"))
    if not rows:
        flash(
            f"The latest {query_type} run has no rows — nothing to pick.",
            "error",
        )
        return redirect(url_for("shipping_page"))

    session_id = pick_store.start_session(
        run_id=run["id"],
        query_type=query_type,
        plan_rows=rows,
        operator=operator,
    )
    logger.info(
        "Started pick session #%s from %s run %s (%d rows).",
        session_id,
        query_type,
        run["id"],
        len(rows),
    )
    return redirect(url_for("pick_session_page", session_id=session_id))


@app.get("/pick/session/<int:session_id>")
@require_trusted_client
def pick_session_page(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        flash(f"Pick session #{session_id} was not found.", "error")
        return redirect(url_for("shipping_page"))

    lines = pick_store.get_lines(session_id)
    scans = pick_store.get_scans(session_id, limit=100)
    for scan in scans:
        scan["scanned_display"] = _audit_dt_display(scan.get("scanned_at"))

    return render_template(
        "pick_session.html",
        session=session_row,
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        lines=lines,
        scans=scans,
        counts=pick_store.compute_counts(session_id),
    )


def _resolve_pick_candidates(scan: str, lines: list[dict]) -> tuple[Optional[str], list[dict], bool]:
    """(serial, part_candidates, erp_failed) for one scanned value.

    A scan matching a picklist part ID directly is a part-barcode pick
    (components); anything else is treated as a serial and resolved in the
    ERP to the part(s) it represents plus where it currently sits.
    """
    line_parts = {str(l["part_id"] or "").strip().upper() for l in lines}
    if scan in line_parts:
        return None, [{"part_id": scan, "locations": []}], False

    try:
        df = run_erp_query_file(
            PICK_SERIAL_LOOKUP_FILE, {"serial": scan}, "pick serial lookup"
        )
    except Exception:  # noqa: BLE001
        logger.exception("Pick serial lookup failed for %s", scan)
        return scan, [], True
    if df.empty:
        return scan, [], False

    rows = df.to_dict(orient="records")
    on_hand = [r for r in rows if r.get("NET_QTY") is not None and pd.notna(r.get("NET_QTY")) and r["NET_QTY"] > 0]
    pool = on_hand or rows
    by_part: dict[str, list[str]] = {}
    for row in pool:
        part = str(row.get("PART_ID") or "").strip()
        if not part:
            continue
        locations = by_part.setdefault(part, [])
        loc = str(row.get("LOCATION_ID") or "").strip()
        if loc and loc not in locations:
            locations.append(loc)
    candidates = [{"part_id": part, "locations": locs} for part, locs in by_part.items()]
    return scan, candidates, False


@app.post("/api/pick/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_pick_scan(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify({"error": "completed", "message": "This pick session is already completed."}), 409

    payload = request.get_json(silent=True) or {}
    scan = (payload.get("scan") or "").strip().upper()
    target_order = (payload.get("order") or "").strip().upper() or None
    operator = (payload.get("operator") or "").strip() or None
    if not scan:
        return jsonify({"error": "invalid", "message": "A scanned value is required."}), 400
    if len(scan) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Scanned value is too long."}), 400

    lines = pick_store.get_lines(session_id)
    serial, candidates, erp_failed = _resolve_pick_candidates(scan, lines)
    if erp_failed:
        return (
            jsonify(
                {
                    "error": "erp_failed",
                    "message": "The ERP serial lookup failed — scan not recorded. Try again.",
                }
            ),
            502,
        )

    try:
        result = pick_store.record_scan(
            session_id,
            scan,
            target_order=target_order,
            serial=serial,
            part_candidates=candidates,
            operator=operator,
            unknown=(serial is not None and not candidates),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record pick scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    result["scan"] = scan
    return jsonify(result), 200


@app.post("/api/pick/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_pick_complete(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404

    completed = pick_store.complete_session(session_id)
    logger.info("Completed pick session #%s.", session_id)
    return jsonify(
        {
            "status": "completed",
            "counts": completed.get("counts"),
            "redirect": url_for("pick_session_page", session_id=session_id),
        }
    ), 200


@app.get("/pick/session/<int:session_id>/export")
@require_trusted_client
def pick_session_export(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        flash(f"Pick session #{session_id} was not found.", "error")
        return redirect(url_for("shipping_page"))

    lines_df = pd.DataFrame(pick_store.get_lines(session_id))
    scans_df = pd.DataFrame(pick_store.get_scans(session_id, limit=10000))
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (lines_df if not lines_df.empty else pd.DataFrame(columns=["part_id"])).to_excel(
            writer, index=False, sheet_name="Lines"
        )
        (scans_df if not scans_df.empty else pd.DataFrame(columns=["scan_value"])).to_excel(
            writer, index=False, sheet_name="Scans"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pick_session{session_id}_{session_row['query_type']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def parse_recipient_addresses(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def recipients_are_valid(recipients: list[str]) -> bool:
    return all(email_address_is_valid(address) for address in recipients)


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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=FLASK_DEBUG,
        use_reloader=False,
    )
