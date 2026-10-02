import atexit
import csv
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
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from flask import (
    g,
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

import allocation
import allocation_store
import audit_store
import audit_universe
import excess
import identity
import notifier
import pick_store
import readiness
import readiness_service
import readiness_store
import recon
import ffl_docs
import release_gate
import request_service
import request_store
import serial_history
import shipments
import shipping_metrics
import shipping_store
import shortage
import stock
import verify_store

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
PICK_UPC_LOOKUP_FILE = resolve_path_setting(
    os.getenv("PICK_UPC_LOOKUP_FILE", "sql/pick_upc_lookup.sql")
)
PACKLIST_SERIALS_FILE = resolve_path_setting(
    os.getenv("PACKLIST_SERIALS_FILE", "sql/packlist_serials.sql")
)
PACKLIST_DAILY_FILE = resolve_path_setting(
    os.getenv("PACKLIST_DAILY_FILE", "sql/packlist_daily.sql")
)
# How long to cache the verify dashboard's daily ERP pull.
VERIFY_DAILY_CACHE_SECONDS = float(os.getenv("VERIFY_DAILY_CACHE_SECONDS", "60"))
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
# Excess packlist cost report: what one avoidable extra shipment costs and
# how long to cache the ERP pull (short, so "still fixable" stays actionable).
EXCESS_PACKLISTS_FILE = resolve_path_setting(
    os.getenv("EXCESS_PACKLISTS_FILE", "sql/excess_packlists.sql")
)
EXCESS_PACKLIST_COST_DEFAULT = 51.0
EXCESS_CACHE_MINUTES = float(os.getenv("EXCESS_CACHE_MINUTES", "5"))
# JP / SLT shipping scorecard. The SQL returns raw order and shipment facts;
# shipping_metrics.py owns the documented grains and denominators.
SHIPPING_METRICS_FILE = resolve_path_setting(
    os.getenv("SHIPPING_METRICS_FILE", "sql/shipping_metrics.sql")
)
SHIPPING_METRICS_CACHE_MINUTES = float(
    os.getenv("SHIPPING_METRICS_CACHE_MINUTES", "5")
)
# Order-release candidates are evaluated before the normal picklist allocation.
RELEASE_CANDIDATES_FILE = resolve_path_setting(
    os.getenv("RELEASE_CANDIDATES_FILE", "sql/release_candidates.sql")
)
RELEASE_SERIAL_SUPPLY_FILE = resolve_path_setting(
    os.getenv("RELEASE_SERIAL_SUPPLY_FILE", "sql/release_serial_supply.sql")
)
RELEASE_SHIPTO_HISTORY_FILE = resolve_path_setting(
    os.getenv("RELEASE_SHIPTO_HISTORY_FILE", "sql/release_shipto_history.sql")
)
RELEASE_GATE_CACHE_SECONDS = float(os.getenv("RELEASE_GATE_CACHE_SECONDS", "60"))
RELEASE_GATE_FILTER_TOKEN = "__RELEASE_GATE_FILTER__"
RELEASE_LOOKAHEAD_TOKEN = "__RELEASE_LOOKAHEAD_DAYS__"
RELEASE_COMPONENT_CODES_TOKEN = "__COMPONENT_PRODUCT_CODES__"
RELEASE_EXCLUDED_CUSTOMERS_TOKEN = "__RELEASE_EXCLUDED_CUSTOMERS__"

# Order readiness ("why can't this ship?")
READINESS_SQL_DIR = Path(__file__).resolve().parent / "sql"
READINESS_CANDIDATES_FILE = READINESS_SQL_DIR / "readiness_candidates.sql"
ORDER_DETAIL_FILE = READINESS_SQL_DIR / "order_detail.sql"
ORDER_PART_LOCATIONS_FILE = READINESS_SQL_DIR / "order_part_locations.sql"
ORDER_DOCUMENTS_FILE = READINESS_SQL_DIR / "order_documents.sql"
READINESS_LOOKAHEAD_DAYS = int(os.getenv("READINESS_LOOKAHEAD_DAYS", "30"))
READINESS_REFRESH_MINUTES = int(os.getenv("READINESS_REFRESH_MINUTES", "15"))
READINESS_CACHE_SECONDS = int(os.getenv("READINESS_CACHE_SECONDS", "300"))
ORDER_SHIPMENTS_FILE = READINESS_SQL_DIR / "order_shipments.sql"
SHIPMENTS_LOOKUP_FILE = READINESS_SQL_DIR / "shipments_lookup.sql"
STOCK_BY_PART_FILE = READINESS_SQL_DIR / "stock_by_part.sql"
SHIPPED_DIGEST_CHECK_MINUTES = int(os.getenv("SHIPPED_DIGEST_CHECK_MINUTES", "5"))
SHIPMENTS_LOOKUP_MAX_DAYS = int(os.getenv("SHIPMENTS_LOOKUP_MAX_DAYS", "120"))
RELEASE_SERIAL_PART_FILTER_TOKEN = "__RELEASE_SERIAL_PART_FILTER__"
# Allocation screen: per-SKU supply/demand model + Promise Del Date editing.
ALLOC_SUPPLY_FILE = resolve_path_setting(
    os.getenv("ALLOC_SUPPLY_FILE", "sql/alloc_supply.sql")
)
ALLOC_DEMAND_FILE = resolve_path_setting(
    os.getenv("ALLOC_DEMAND_FILE", "sql/alloc_demand.sql")
)
ALLOC_PARTS_FILE = resolve_path_setting(
    os.getenv("ALLOC_PARTS_FILE", "sql/alloc_parts.sql")
)
ALLOC_PART_SEARCH_MIN_CHARS = 2
ALLOC_PART_ID_MAX_LENGTH = 30
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
    "mssql_write_connection_string",
    "telegram_bot_token",
    "smtp_password",
    "teams_webhook_url",
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


@app.template_filter("local_dt")
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


def apply_release_gate_filter(query: str, gate_payload: Optional[dict]) -> str:
    """Inject the released order set before the SQL allocation CTE runs."""
    if RELEASE_GATE_FILTER_TOKEN not in query:
        return query
    if not gate_payload or gate_payload.get("mode") != "enforced":
        return query.replace(RELEASE_GATE_FILTER_TOKEN, "")
    released = sorted(
        {
            str(order_id).strip().upper()
            for order_id in gate_payload.get("released_orders") or []
            if str(order_id).strip()
        }
    )
    if not released:
        clause = "AND 1 = 0  -- release gate: no orders currently eligible"
    else:
        # Accepted trade-off: escaped literals in an IN list (read-only query,
        # bounded by the open-order count; both picklist queries already run
        # with OPTION (RECOMPILE), so plan-reuse loss is moot).
        literals = ", ".join(sql_quote_literal(order_id) for order_id in released)
        clause = f"AND co.ID IN ({literals})  -- release gate policy v{gate_payload.get('policy_version', 1)}"
    return query.replace(RELEASE_GATE_FILTER_TOKEN, clause)


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


def get_erp_write_engine():
    """Engine for the one ERP write path (Promise Del Date saves).

    Uses mssql_write_connection_string / MSSQL_WRITE_CONNECTION_STRING when
    configured — intended to be a dedicated login whose only write right is a
    column-level UPDATE grant on CUST_ORDER_LINE.PROMISE_DEL_DATE — and falls
    back to the read connection until that login exists. Shares the engine
    cache, so a settings change takes effect without a restart.
    """
    write_conn_string = get_config_value(
        setting_key="mssql_write_connection_string",
        env_key="MSSQL_WRITE_CONNECTION_STRING",
    )
    if not write_conn_string:
        return get_erp_engine()
    with _erp_engines_lock:
        engine = _erp_engines.get(write_conn_string)
        if engine is None:
            logger.info(
                "Connecting to SQL Server (write) using %s",
                mask_connection_string(write_conn_string),
            )
            engine = create_engine(write_conn_string, pool_pre_ping=True, pool_recycle=1800)
            _erp_engines[write_conn_string] = engine
    return engine


# The app's only ERP write. UPDLOCK on the SELECT holds the row for the
# duration of the transaction; the UPDATE's old-value predicate is the
# optimistic-concurrency check against edits made before we loaded the screen.
# CAST(... AS date): the UI only ever saw date precision, so a stray time
# component in the column must not read as a conflict.
ALLOC_SELECT_LINE_SQL = """
SELECT PART_ID, CAST(PROMISE_DEL_DATE AS date) AS PROMISE_DEL_DATE
FROM dbo.CUST_ORDER_LINE WITH (UPDLOCK, ROWLOCK)
WHERE CUST_ORDER_ID = :so AND LINE_NO = :line
"""
ALLOC_UPDATE_SQL = """
UPDATE dbo.CUST_ORDER_LINE
SET PROMISE_DEL_DATE = :new_value
WHERE CUST_ORDER_ID = :so AND LINE_NO = :line
  AND ((CAST(PROMISE_DEL_DATE AS date) = :old_value)
       OR (PROMISE_DEL_DATE IS NULL AND :old_value IS NULL))
"""


def fetch_picklist_from_mssql(
    query_type: str,
    query_options: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    query = load_query(query_type, query_options=query_options)
    gate_payload = None
    gate_mode = get_release_gate_mode()
    if gate_mode != "off":
        # A generated picklist is the operational boundary: bypass the dashboard cache
        # and revalidate sticky serial reservations against live ERP immediately before
        # released order IDs are injected into the allocation SQL.
        gate_payload = build_release_gate_payload(
            force=True,
            query_options=query_options,
            require_serial_tracking=gate_mode == "enforced",
            persist=True,
        )
        if gate_payload.get("error") and gate_mode == "enforced":
            raise RuntimeError(
                "Release gate is enforced but could not be evaluated: "
                f"{gate_payload.get('error')}"
            )
    query = apply_release_gate_filter(query, gate_payload)
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
    try:
        df = _apply_manual_hold_exclusions(df)
    except Exception as exc:  # noqa: BLE001 - a hold ledger problem must not fail the run
        logger.exception("Manual hold exclusion failed: %s", exc)
    if gate_payload is not None:
        df.attrs["release_gate_payload"] = gate_payload
        if gate_payload and not gate_payload.get("error") and not df.empty:
            decision_map = {
                str(row["order_id"]).strip().upper(): row
                for row in gate_payload.get("decisions") or []
            }
            order_column = "Cust Order ID"
            if order_column in df.columns:
                df["Release Gate"] = df[order_column].map(
                    lambda value: (decision_map.get(str(value).strip().upper()) or {}).get(
                        "decision", "UNREVIEWED"
                    )
                )
                df["Release Reason"] = df[order_column].map(
                    lambda value: (decision_map.get(str(value).strip().upper()) or {}).get(
                        "label", "No gate decision"
                    )
                )
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


def fetch_serial_onhand_locations(serials: list[str]) -> dict[str, list[dict]]:
    """Live ERP on-hand balances for a set of serials.

    Returns {SERIAL: [{warehouse, location, qty, last_txn_at}, ...]} keeping
    only positive net balances — the same inference the expected-list query
    uses, but at request time instead of snapshot time.
    """
    serials = sorted({(s or "").strip().upper() for s in serials if (s or "").strip()})
    if not serials:
        return {}
    binds = {f"s{i}": s for i, s in enumerate(serials)}
    placeholders = ", ".join(f":{k}" for k in binds)
    query = text(
        f"""
        SELECT UPPER(tit.TRACE_ID) AS SERIAL_NO,
               it.WAREHOUSE_ID, it.LOCATION_ID,
               SUM(tit.QTY) AS NET_QTY,
               MAX(it.CREATE_DATE) AS LAST_TXN_AT
        FROM dbo.TRACE_INV_TRANS tit
        INNER JOIN dbo.INVENTORY_TRANS it
            ON it.TRANSACTION_ID = tit.TRANSACTION_ID
           AND it.PART_ID        = tit.PART_ID
        WHERE UPPER(tit.TRACE_ID) IN ({placeholders})
        GROUP BY UPPER(tit.TRACE_ID), it.WAREHOUSE_ID, it.LOCATION_ID
        HAVING SUM(tit.QTY) > 0
        """
    )
    engine = get_erp_engine()
    result: dict[str, list[dict]] = {}
    with engine.connect() as connection:
        for row in connection.execute(query, binds).mappings():
            result.setdefault(row["SERIAL_NO"], []).append(
                {
                    "warehouse": row["WAREHOUSE_ID"],
                    "location": row["LOCATION_ID"],
                    "qty": float(row["NET_QTY"]),
                    "last_txn_at": row["LAST_TXN_AT"],
                }
            )
    return result


def recheck_unexpected_scans(session_id: int) -> list[dict]:
    """Re-query the ERP for a session's unexpected serials.

    The snapshot is point-in-time: a WO receipt posted after the session
    started makes a perfectly-placed gun scan as "unexpected". For each
    unexpected serial now on hand in the scanned location, record an automatic
    'erp_synced' resolution. Returns one summary row per unexpected serial.
    """
    unexpected = audit_store.get_unexpected_scans(session_id)
    if not unexpected:
        return []
    onhand = fetch_serial_onhand_locations([r["scanned_serial"] for r in unexpected])
    summary = []
    for row in unexpected:
        serial = (row.get("scanned_serial") or "").strip().upper()
        scanned_loc = (row.get("scanned_location") or "").strip().upper()
        balances = onhand.get(serial, [])
        matched = next(
            (b for b in balances if (b["location"] or "").strip().upper() == scanned_loc),
            None,
        )
        auto_resolved = False
        if matched:
            posted = matched["last_txn_at"]
            posted_str = posted.strftime("%Y-%m-%d %H:%M") if hasattr(posted, "strftime") else str(posted)
            note = (
                f"ERP re-check: now on hand in {matched['warehouse']}/{matched['location']} "
                f"(last transaction {posted_str} ERP time) — receipt posted after the audit snapshot."
            )
            auto_resolved = audit_store.auto_resolve_unexpected(session_id, serial, note)
        summary.append(
            {
                "serial": serial,
                "scanned_location": scanned_loc,
                "erp_locations": [
                    f"{b['warehouse']}/{b['location']}" for b in balances
                ],
                "now_in_scanned_location": matched is not None,
                "auto_resolved": auto_resolved,
            }
        )
    return summary


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


def get_excess_packlist_cost() -> float:
    """$ per avoidable extra packlist; settings page overrides env."""
    raw = get_config_value(
        "excess_packlist_cost_usd", "EXCESS_PACKLIST_COST_USD", str(EXCESS_PACKLIST_COST_DEFAULT)
    )
    try:
        value = float(str(raw).strip() or EXCESS_PACKLIST_COST_DEFAULT)
    except (TypeError, ValueError):
        return EXCESS_PACKLIST_COST_DEFAULT
    return value if value >= 0 else EXCESS_PACKLIST_COST_DEFAULT


def get_release_gate_mode() -> str:
    """off | advisory | enforced; new installs deliberately start advisory."""
    raw = get_config_value("release_gate_mode", "RELEASE_GATE_MODE", "advisory")
    mode = str(raw or "advisory").strip().lower()
    return mode if mode in {"off", "advisory", "enforced"} else "advisory"


def get_release_gate_due_override_days() -> int:
    raw = get_config_value(
        "release_gate_due_override_days", "RELEASE_GATE_DUE_OVERRIDE_DAYS", "1"
    )
    try:
        return max(0, min(int(str(raw).strip()), 30))
    except (TypeError, ValueError):
        return 1


def get_release_gate_min_ship_to_cooldown_days() -> int:
    """Global floor for the ship-to cooldown; protects every customer from
    same-day duplicate picklists even without an explicit policy."""
    raw = get_config_value(
        "release_gate_min_ship_to_cooldown_days",
        "RELEASE_GATE_MIN_SHIP_TO_COOLDOWN_DAYS",
        "1",
    )
    try:
        return max(0, min(int(str(raw).strip()), 30))
    except (TypeError, ValueError):
        return 1


def parse_release_gate_customer_policies(raw: Any) -> dict[str, dict[str, Any]]:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("Customer release policies must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Customer release policies must be a JSON object keyed by customer ID.")

    result: dict[str, dict[str, Any]] = {}
    for customer_id, policy in parsed.items():
        customer = str(customer_id or "").strip().upper()
        if not customer or not isinstance(policy, dict):
            raise ValueError("Each customer release policy must be an object.")
        normalized: dict[str, Any] = {}
        account_type = policy.get("account_type")
        if account_type not in (None, ""):
            account_type_value = str(account_type).strip().lower()
            if account_type_value not in {"major", "standard"}:
                raise ValueError(f"{customer}: account_type must be major or standard.")
            normalized["account_type"] = account_type_value
        accumulate = policy.get("accumulate", False)
        if not isinstance(accumulate, bool):
            raise ValueError(f"{customer}: accumulate must be true or false.")
        normalized["accumulate"] = accumulate
        try:
            min_guns = int(policy.get("min_guns", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{customer}: min_guns must be a whole number.") from exc
        if min_guns < 0:
            raise ValueError(f"{customer}: min_guns must be zero or greater.")
        normalized["min_guns"] = min_guns
        if "target_guns" in policy:
            try:
                target_guns = int(policy.get("target_guns", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{customer}: target_guns must be a whole number.") from exc
            if target_guns < 0:
                raise ValueError(f"{customer}: target_guns must be zero or greater.")
            normalized["target_guns"] = target_guns
        if "mix_orders" in policy:
            mix_orders = policy.get("mix_orders")
            if not isinstance(mix_orders, bool):
                raise ValueError(f"{customer}: mix_orders must be true or false.")
            normalized["mix_orders"] = mix_orders
        if "release_cadence" in policy:
            cadence = str(policy.get("release_cadence") or "").strip().lower()
            if cadence not in {"threshold", "daily", "completion"}:
                raise ValueError(
                    f"{customer}: release_cadence must be threshold, daily, or completion."
                )
            normalized["release_cadence"] = cadence
        if "daily_release_time" in policy:
            daily_release_time = str(policy.get("daily_release_time") or "").strip()
            try:
                parsed_release_time = datetime.strptime(daily_release_time, "%H:%M")
            except ValueError as exc:
                raise ValueError(
                    f"{customer}: daily_release_time must use 24-hour HH:MM format."
                ) from exc
            normalized["daily_release_time"] = parsed_release_time.strftime("%H:%M")
        if "max_hold_days" in policy:
            try:
                max_hold_days = int(policy.get("max_hold_days", 7))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{customer}: max_hold_days must be a whole number.") from exc
            if max_hold_days < 0 or max_hold_days > 90:
                raise ValueError(f"{customer}: max_hold_days must be from 0 to 90.")
            normalized["max_hold_days"] = max_hold_days
        if "ship_to_cooldown_days" in policy:
            try:
                cooldown_days = int(policy.get("ship_to_cooldown_days", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{customer}: ship_to_cooldown_days must be a whole number."
                ) from exc
            if cooldown_days < 0 or cooldown_days > 30:
                raise ValueError(
                    f"{customer}: ship_to_cooldown_days must be from 0 to 30."
                )
            normalized["ship_to_cooldown_days"] = cooldown_days
        sweep = policy.get("sweep_weekday")
        if sweep not in (None, ""):
            try:
                sweep_value = int(sweep)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{customer}: sweep_weekday must be 0 (Monday) through 6 (Sunday)."
                ) from exc
            if sweep_value not in range(7):
                raise ValueError(
                    f"{customer}: sweep_weekday must be 0 (Monday) through 6 (Sunday)."
                )
            normalized["sweep_weekday"] = sweep_value
        result[customer] = normalized
    return result


def get_release_gate_customer_policies() -> dict[str, dict[str, Any]]:
    raw = get_config_value(
        "release_gate_customer_policies_json",
        "RELEASE_GATE_CUSTOMER_POLICIES_JSON",
        "{}",
    )
    try:
        return parse_release_gate_customer_policies(raw)
    except ValueError as exc:
        logger.error("Invalid release-gate customer policy configuration: %s", exc)
        return {}


def ensure_release_gate_policy_version(changed_by: str = "system") -> dict[str, Any]:
    desired = {
        "mode": get_release_gate_mode(),
        "due_override_days": get_release_gate_due_override_days(),
        "customer_policies": get_release_gate_customer_policies(),
    }
    latest = shipping_store.latest_policy_config()
    if latest and all(latest.get(key) == value for key, value in desired.items()):
        return latest
    version = shipping_store.save_policy_config(changed_by=changed_by, **desired)
    return {"version": version, **desired, "changed_by": changed_by}


def get_operator_roster() -> list[dict[str, str]]:
    """Roster for the operator picker (Settings overrides OPERATOR_ROSTER_JSON)."""
    raw = get_config_value("operator_roster_json", "OPERATOR_ROSTER_JSON", "[]")
    try:
        return identity.parse_roster(raw)
    except ValueError as exc:
        logger.error("Invalid operator roster configuration: %s", exc)
        return []


def get_teams_digest_time() -> str:
    return (get_config_value("teams_digest_time", "TEAMS_DIGEST_TIME", "16:30") or "16:30").strip()


def get_max_runs_per_day() -> int:
    """Per-list generation budget; 0 = unlimited. Settings page overrides env."""
    raw = get_config_value("max_runs_per_day", "MAX_RUNS_PER_DAY", "0")
    try:
        return max(0, int(str(raw).strip() or 0))
    except (TypeError, ValueError):
        return 0


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
            gate_payload = df.attrs.get("release_gate_payload")
            if gate_payload and not gate_payload.get("error"):
                released = [
                    row
                    for row in gate_payload.get("decisions") or []
                    if row.get("decision") == "RELEASE"
                ]
                shipping_store.record_released_ship_tos(
                    run_id=run_id,
                    released_decisions=released,
                    released_date=_today_local(),
                )
        except Exception as exc:  # noqa: BLE001 — ledger write must never fail the run
            logger.exception("Failed to record released ship-tos for run %s: %s", run_id, exc)
        try:
            save_plan_snapshot(
                run_id=run_id,
                query_type=query_type,
                run_timestamp=run_timestamp,
                rows=df.to_dict(orient="records"),
            )
        except Exception as exc:  # noqa: BLE001 — recon snapshot must never fail the run
            logger.exception("Failed to save plan snapshot for run %s: %s", run_id, exc)
        try:
            if feature_enabled("orders"):
                threading.Thread(
                    target=readiness_service.refresh,
                    kwargs={"trigger": "picklist_run", "force": True},
                    name="readiness-after-picklist",
                    daemon=True,
                ).start()
        except Exception as exc:  # noqa: BLE001 — readiness refresh must never fail the run
            logger.exception("Failed to start readiness refresh after run %s: %s", run_id, exc)
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
        exclusions = df.attrs.get("manual_hold_exclusions") or []
        if exclusions:
            message += (
                f" {len(exclusions)} order{'s' if len(exclusions) != 1 else ''} excluded by manual holds: "
                + ", ".join(
                    f"{row['order_id']} ({row.get('hold_kind') or 'hold'} until {row.get('expires_at') or '?'})"
                    for row in exclusions[:10]
                )
                + (" ..." if len(exclusions) > 10 else "")
                + "."
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
    budget = get_run_budget(normalized_query_type)
    if budget["exhausted"]:
        logger.info(
            "Skipped picklist run for %s: daily run limit reached (%d used, resets %s).",
            normalized_query_type,
            budget["used"],
            budget["resets_at_display"],
        )
        return None
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
    budget = get_run_budget(normalized_query_type)
    if budget["exhausted"]:
        logger.info(
            "Skipped background picklist run for %s: daily run limit reached (%d used, resets %s).",
            normalized_query_type,
            budget["used"],
            budget["resets_at_display"],
        )
        return False
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


# Feature rollout toggles. Disabled features are removed from the top nav and
# their pages/APIs are blocked, so new modules can be introduced one at a time.
FEATURE_FLAGS = {
    "shipping": {
        "setting_key": "feature_shipping_enabled",
        "label": "Shipping",
        "path_prefixes": (
            "/shipping",
            "/pick",
            "/verify",
            "/api/shipping",
            "/api/pick",
            "/api/verify",
        ),
    },
    "audit": {
        "setting_key": "feature_audit_enabled",
        "label": "Audit",
        "path_prefixes": ("/audit", "/api/audit"),
    },
    "serial": {
        "setting_key": "feature_serial_enabled",
        "label": "Serial Lookup",
        "path_prefixes": ("/serial-history", "/api/serial-history"),
    },
    "allocation": {
        "setting_key": "feature_allocation_enabled",
        "label": "Allocation",
        "path_prefixes": ("/allocation", "/api/allocation"),
    },
    "orders": {
        "setting_key": "feature_orders_enabled",
        "label": "Orders",
        "path_prefixes": (
            "/orders",
            "/api/orders",
            "/api/readiness",
            "/shipments",
            "/api/shipments",
            "/stock",
            "/api/stock",
            "/api/shipping/digest",
        ),
    },
    "requests": {
        "setting_key": "feature_requests_enabled",
        "label": "Requests",
        "path_prefixes": ("/requests", "/api/requests", "/api/holds"),
    },
}


def feature_enabled(feature: str) -> bool:
    return parse_bool(get_setting(FEATURE_FLAGS[feature]["setting_key"]), default=True)


def get_feature_flags() -> dict[str, bool]:
    return {name: feature_enabled(name) for name in FEATURE_FLAGS}


@app.context_processor
def inject_feature_flags():
    return {"feature_flags": get_feature_flags()}


@app.context_processor
def inject_request_badge():
    """Open-request count for the Requests tab. Best effort: a missing or
    unconfigured request store must never break a page render."""
    badge = {"open": 0, "overdue": 0}
    if feature_enabled("requests"):
        try:
            badge = request_store.open_counts()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Request badge unavailable: %s", exc)
    return {"request_badge": badge}


@app.context_processor
def inject_operator_roster():
    return {
        "operator_roster": get_operator_roster(),
        "operator_teams": [(team, identity.TEAM_LABELS[team]) for team in identity.TEAMS],
    }


@app.before_request
def enforce_feature_flags():
    for name, definition in FEATURE_FLAGS.items():
        if not request.path.startswith(definition["path_prefixes"]):
            continue
        if feature_enabled(name):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": f"{definition['label']} is currently disabled."}), 404
        flash(f"{definition['label']} is currently turned off in Settings.", "error")
        return redirect(url_for("index"))
    return None


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


def get_readiness_config() -> dict[str, Any]:
    codes = get_config_value("readiness_credit_check_codes", "READINESS_CREDIT_CHECK_CODES", "C") or "C"
    return {
        "require_ffl_doc": parse_bool(
            get_config_value("readiness_require_ffl_doc", "READINESS_REQUIRE_FFL_DOC", "true"),
            default=True,
        ),
        "credit_check_codes": tuple(
            code.strip().upper() for code in str(codes).split(",") if code.strip()
        ),
    }


def get_ffl_doc_config() -> dict[str, Any]:
    """Tier-2 (document OCR) settings. Off unless READINESS_OCR_ENABLED is true."""
    def _float(key: str, env: str, default: float) -> float:
        try:
            return float(get_config_value(key, env, str(default)) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": parse_bool(get_config_value("readiness_ocr_enabled", "READINESS_OCR_ENABLED", "false"), default=False),
        "path_map": get_config_value("document_path_map", "DOCUMENT_PATH_MAP", "") or "",
        "roots": get_config_value("document_roots", "DOCUMENT_ROOTS", "") or "",
        "ocr_dpi": _int_setting("readiness_ocr_dpi", "READINESS_OCR_DPI", ffl_docs.DEFAULT_OCR_DPI),
        "ocr_pages": _int_setting("readiness_ocr_pages", "READINESS_OCR_PAGES", ffl_docs.DEFAULT_OCR_PAGES),
        "max_docs_per_run": _int_setting("readiness_ocr_max_docs_per_run", "READINESS_OCR_MAX_DOCS_PER_RUN", ffl_docs.DEFAULT_MAX_DOCS_PER_RUN),
        "name_threshold": _float("readiness_ffl_name_threshold", "READINESS_FFL_NAME_THRESHOLD", ffl_docs.DEFAULT_NAME_THRESHOLD),
        "addr_threshold": _float("readiness_ffl_addr_threshold", "READINESS_FFL_ADDR_THRESHOLD", ffl_docs.DEFAULT_ADDR_THRESHOLD),
    }


def fetch_order_documents(order_id: str) -> list[dict]:
    df = run_erp_query_file(ORDER_DOCUMENTS_FILE, {"so": str(order_id).strip().upper()}, f"order documents {order_id}")
    return df.to_dict(orient="records")


def _readiness_doc_findings(orders: list[dict]) -> dict[str, list[dict]]:
    """Tier-2 FFL document comparison for the readiness refresh (flag-gated)."""
    cfg = get_ffl_doc_config()
    if not cfg["enabled"]:
        return {}
    ffl_docs.configure(
        path_map=cfg["path_map"],
        roots=cfg["roots"],
        ocr_enabled=True,
        ocr_dpi=cfg["ocr_dpi"],
        ocr_pages=cfg["ocr_pages"],
        max_docs_per_run=cfg["max_docs_per_run"],
        name_threshold=cfg["name_threshold"],
        addr_threshold=cfg["addr_threshold"],
        logger=logger,
    )
    return ffl_docs.findings_for_orders(
        orders,
        fetch_documents=fetch_order_documents,
        cache_get=readiness_store.get_doc_cache,
        cache_put=readiness_store.put_doc_cache,
    )


def _order_documents_view(order_id: str) -> list[dict]:
    """Attachments for the order page, classified; never fails the page."""
    try:
        rows = fetch_order_documents(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Order documents lookup failed for %s: %s", order_id, exc)
        return []
    out = []
    for row in rows:
        document_id = str(row.get("DOCUMENT_ID") or "").strip()
        folder = str(row.get("DOC_FILE_PATH") or "").strip()
        created = row.get("CREATE_DATE")
        out.append({
            "document_id": document_id,
            "folder": folder,
            "kind": ffl_docs.classify_kind(document_id, folder),
            "description": str(row.get("DESCRIPTION") or "").strip() or None,
            "created": created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else (str(created)[:10] if created else None),
        })
    return out


def _render_readiness_sql(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Readiness query not found at: {path}")
    template = path.read_text(encoding="utf-8")
    return render_release_candidates_query(
        template, query_options={"lookahead_days": READINESS_LOOKAHEAD_DAYS}
    )


def fetch_readiness_candidate_rows() -> list[dict]:
    query = _render_readiness_sql(READINESS_CANDIDATES_FILE)
    engine = get_erp_engine()
    logger.info("Running order readiness candidate query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Order readiness candidate query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def fetch_order_detail_rows(order_id: str) -> list[dict]:
    query = _render_readiness_sql(ORDER_DETAIL_FILE)
    engine = get_erp_engine()
    logger.info("Running order detail query for %s", order_id)
    with engine.connect() as connection:
        df = pd.read_sql_query(text(query), connection, params={"so": order_id})
    return df.to_dict(orient="records")


def fetch_order_part_locations(order_id: str) -> list[dict]:
    df = run_erp_query_file(
        ORDER_PART_LOCATIONS_FILE, {"so": order_id}, f"order part locations query ({order_id})"
    )
    return df.to_dict(orient="records")


def _picklist_orders_today() -> Optional[set[str]]:
    """Order ids on today's latest guns + components runs; None when no run today."""
    today = _today_local()
    orders: set[str] = set()
    found = False
    for query_type in QUERY_FILES:
        run, rows = get_latest_successful_run(query_type=query_type)
        if not run:
            continue
        try:
            run_day = parse_run_timestamp(run["run_timestamp"]).astimezone(resolve_timezone()).date()
        except Exception:  # noqa: BLE001
            continue
        if run_day != today:
            continue
        found = True
        for row in rows:
            order_id = str(row.get("Cust Order ID") or "").strip().upper()
            if order_id:
                orders.add(order_id)
    return orders if found else None


def _readiness_gate_decisions() -> dict[str, dict]:
    mode = get_release_gate_mode()
    if mode == "off":
        return {}
    payload = build_release_gate_payload()
    if not payload or payload.get("error"):
        return {}
    # Decisions always flow through so the order page can show them; only an
    # enforced gate turns a HOLD / ACCUMULATING decision into a readiness hold.
    return {
        str(row.get("order_id") or "").upper(): {**row, "mode": mode, "enforced": mode == "enforced"}
        for row in payload.get("decisions") or []
        if row.get("order_id")
    }


def _readiness_pick_status(order_id: str) -> dict[str, Any]:
    claimed = order_id in pick_store.claimed_orders()
    ready = [
        row for row in pick_store.ready_for_pack_orders(limit=500)
        if str(row.get("cust_order_id") or "").upper() == order_id
    ]
    return {
        "claimed": claimed,
        "ready_for_pack": bool(ready),
        "packlist_id": next((row.get("packlist_id") for row in ready if row.get("packlist_id")), None),
        "operator": next((row.get("operator") for row in ready if row.get("operator")), None),
    }


def _active_manual_holds() -> list[dict]:
    """Manual holds from approved set-aside / exception requests."""
    try:
        return request_store.active_manual_holds()
    except Exception:  # noqa: BLE001 - never let the hold ledger break a page
        logger.exception("Manual hold read failed")
        return []


def invalidate_release_gate_cache() -> None:
    _release_gate_cache.update({"payload": None, "fetched_at": None, "signature": None})


def _int_setting(setting_key: str, env_key: str, default: int) -> int:
    raw = get_config_value(setting_key, env_key, str(default))
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def get_request_sla_overrides() -> dict[str, int]:
    raw = get_config_value("request_sla_hours_json", "REQUEST_SLA_HOURS_JSON", "{}") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Invalid REQUEST_SLA_HOURS_JSON; using defaults")
        return {}
    out: dict[str, int] = {}
    for key, value in (data or {}).items():
        try:
            out[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return out


def _readiness_blocking_holds(order_id: str) -> list[dict]:
    return [
        row for row in readiness_store.open_holds(cust_order_id=order_id)
        if row.get("blocking")
    ]


def _apply_manual_hold_exclusions(df: pd.DataFrame) -> pd.DataFrame:
    """Drop picklist rows for orders under an active manual hold; note them on df.attrs."""
    order_column = "Cust Order ID"
    held = {str(h.get("cust_order_id") or "").upper(): h for h in _active_manual_holds()}
    df.attrs["manual_hold_exclusions"] = []
    if not held or df.empty or order_column not in df.columns:
        return df
    mask = df[order_column].map(lambda value: str(value).strip().upper() in held)
    if not mask.any():
        return df
    excluded_ids = sorted({str(v).strip().upper() for v in df.loc[mask, order_column]})
    trimmed = df.loc[~mask].reset_index(drop=True)
    trimmed.attrs.update(df.attrs)
    trimmed.attrs["manual_hold_exclusions"] = [
        {
            "order_id": order_id,
            "hold_kind": held[order_id].get("hold_kind"),
            "expires_at": str(held[order_id].get("expires_at") or "")[:10],
            "created_by": held[order_id].get("created_by"),
        }
        for order_id in excluded_ids
    ]
    logger.info("Manual holds excluded %d order(s) from the picklist: %s", len(excluded_ids), ", ".join(excluded_ids))
    return trimmed


def fetch_order_shipment_rows(order_id: str) -> list[dict]:
    df = run_erp_query_file(ORDER_SHIPMENTS_FILE, {"so": order_id}, f"order shipments ({order_id})")
    return df.to_dict(orient="records")


def fetch_shipment_lookup_rows(
    start_date: str, end_date: str, customer: str = "", so: str = ""
) -> list[dict]:
    customer_term = (customer or "").strip().upper()
    so_term = (so or "").strip().upper()
    df = run_erp_query_file(
        SHIPMENTS_LOOKUP_FILE,
        {
            "start_date": start_date,
            "end_date": end_date,
            "customer_pattern": f"%{customer_term}%" if customer_term else "%",
            "so_pattern": f"%{so_term}%" if so_term else "%",
        },
        f"shipment lookup {start_date}..{end_date}",
    )
    return df.to_dict(orient="records")


def fetch_stock_rows(part_id: str) -> list[dict]:
    df = run_erp_query_file(STOCK_BY_PART_FILE, {"part_id": part_id}, f"stock lookup ({part_id})")
    return df.to_dict(orient="records")


def build_stock_lookup(part_id: str) -> dict[str, Any]:
    part = (part_id or "").strip().upper()
    rows = fetch_stock_rows(part)
    allocation_payload: Optional[dict[str, Any]]
    try:
        allocation_payload = _build_allocation_payload(part)
    except Exception as exc:  # noqa: BLE001 - allocation is supplemental here
        logger.exception("Allocation lookup for stock page failed (%s)", part)
        allocation_payload = {"error": str(exc)}
    try:
        reservations = shipping_store.serial_reservations_for_gate()
    except Exception:  # noqa: BLE001
        logger.exception("Serial reservation read failed for stock page")
        reservations = []
    return stock.build_stock_payload(
        part,
        location_rows=rows,
        allocation=allocation_payload,
        reservations=reservations,
        manual_holds=_active_manual_holds(),
    )


def _search_parts(term: str) -> list[dict[str, Any]]:
    df = run_erp_query_file(
        ALLOC_PARTS_FILE,
        {"pattern": f"%{term}%", "prefix": f"{term}%"},
        f"part search '{term}'",
    )
    return [
        {
            "part_id": str(row.get("PART_ID") or ""),
            "description": row.get("DESCRIPTION") if row.get("DESCRIPTION") == row.get("DESCRIPTION") else None,
            "on_hand": int(row.get("ON_HAND") or 0),
            "open_demand": int(row.get("OPEN_DEMAND") or 0),
        }
        for row in df.to_dict(orient="records")
    ]


def build_shipped_digest_payload(day: Optional[date] = None) -> dict[str, Any]:
    day = day or _today_local()
    rows = fetch_shipment_lookup_rows(day.isoformat(), (day + timedelta(days=1)).isoformat())
    return shipments.build_shipped_digest(rows, day=day)


def send_shipped_digest(day: Optional[date] = None, *, force: bool = False) -> dict[str, Any]:
    payload = build_shipped_digest_payload(day)
    result = {
        "day": payload["day"],
        "packlists": payload["packlist_count"],
        "orders": payload["order_count"],
        "missing_tracking": payload["missing_tracking"],
        "sent": False,
        "reason": None,
    }
    if payload["packlist_count"] == 0 and not force:
        result["reason"] = "nothing shipped"
        return result
    missing = payload["missing_tracking"]
    footer = (
        f"{len(missing)} packlist{'s' if len(missing) != 1 else ''} still without a tracking number: "
        + ", ".join(missing[:10])
        + (" ..." if len(missing) > 10 else "")
        if missing
        else None
    )
    chunks = notifier.chunk_rows(payload["rows"])
    sent_all = True
    for index, chunk in enumerate(chunks):
        suffix = f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        sent = notifier.send_teams_notification(
            "shipped_digest",
            title=payload["title"] + suffix,
            text=None if chunk else "Nothing shipped today.",
            rows=chunk or None,
            columns=payload["columns"] if chunk else None,
            link=notifier.public_url("/shipments"),
            footer=footer if index == len(chunks) - 1 else None,
            event_key=payload["event_key"] + (f":part{index + 1}" if index else ""),
            force=force,
        )
        sent_all = sent_all and bool(sent)
    result["sent"] = sent_all
    result["cards"] = len(chunks)
    if not sent:
        result["reason"] = "not delivered (see notification log)"
    return result


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


initialize_db()
migrate_sensitive_settings_encryption()
audit_store.initialize()
pick_store.initialize(get_sqlite_conn)
verify_store.initialize(get_sqlite_conn)
allocation_store.initialize(get_sqlite_conn)
shipping_store.initialize(get_sqlite_conn)
notifier.initialize(get_sqlite_conn)
notifier.configure(
    get_config_value=get_config_value,
    send_email_notification=send_email_notification,
    logger=logger,
)
identity.configure(get_roster=get_operator_roster)
readiness_store.initialize(get_sqlite_conn)
readiness_service.configure(
    fetch_candidates=fetch_readiness_candidate_rows,
    fetch_order_rows=fetch_order_detail_rows,
    fetch_order_locations=fetch_order_part_locations,
    picklist_orders_today=_picklist_orders_today,
    picklist_horizon=lambda: _today_local()
    + timedelta(days=int(get_default_guns_query_options()["lookahead_days"])),
    gate_decisions=_readiness_gate_decisions,
    manual_holds=_active_manual_holds,
    pick_status=_readiness_pick_status,
    fetch_order_shipments=fetch_order_shipment_rows,
    today=lambda: _today_local(),
    config=get_readiness_config,
    notify=notifier.send_teams_notification,
    public_url=notifier.public_url,
    cache_seconds=READINESS_CACHE_SECONDS,
    doc_findings=_readiness_doc_findings,
    logger=logger,
)
request_store.initialize(get_sqlite_conn)
request_store.configure(sla_overrides=get_request_sla_overrides())
request_service.configure(
    add_exception=lambda order_id, reason, actor, expires_at: shipping_store.add_exception(
        cust_order_id=order_id, reason=reason, created_by=actor, expires_at=expires_at
    ),
    revoke_exception=lambda exception_id, actor: shipping_store.revoke_exception(exception_id, actor),
    invalidate_gate_cache=invalidate_release_gate_cache,
    refresh_readiness=lambda: threading.Thread(
        target=readiness_service.refresh, kwargs={"trigger": "request", "force": True},
        name="readiness-after-request", daemon=True,
    ).start(),
    blocking_holds=_readiness_blocking_holds,
    notify=notifier.send_teams_notification,
    public_url=notifier.public_url,
    now=lambda: datetime.now(resolve_timezone()),
    expedite_max_hours=_int_setting("request_expedite_max_hours", "REQUEST_EXPEDITE_MAX_HOURS", 72),
    logger=logger,
)
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
    run_budget_by_type = {qt: get_run_budget(qt) for qt in query_types}
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
        run_budget_by_type=run_budget_by_type,
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

        mssql_write_connection_string = (
            request.form.get("mssql_write_connection_string") or ""
        ).strip()
        if request.form.get("clear_mssql_write_connection_string"):
            delete_setting("mssql_write_connection_string")
        elif mssql_write_connection_string:
            set_setting("mssql_write_connection_string", mssql_write_connection_string)

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

        teams_webhook_url = (request.form.get("teams_webhook_url") or "").strip()
        if request.form.get("clear_teams_webhook_url"):
            delete_setting("teams_webhook_url")
        elif teams_webhook_url:
            if not teams_webhook_url.lower().startswith("https://"):
                flash("Teams webhook URL must start with https://.", "error")
                return redirect(url_for("settings"))
            set_setting("teams_webhook_url", teams_webhook_url)

        selected_events = [
            name for name in notifier.TEAMS_EVENTS if request.form.get(f"teams_event_{name}")
        ]
        if len(selected_events) == len(notifier.TEAMS_EVENTS):
            set_setting("teams_enabled_events", "all")
        elif selected_events:
            set_setting("teams_enabled_events", ",".join(selected_events))
        else:
            set_setting("teams_enabled_events", "none")

        teams_digest_time = (request.form.get("teams_digest_time") or "").strip()
        if teams_digest_time:
            try:
                parse_schedule_time(teams_digest_time)
            except ValueError:
                flash("Teams digest time must be HH:MM (24-hour).", "error")
                return redirect(url_for("settings"))
            set_setting("teams_digest_time", teams_digest_time)
        else:
            delete_setting("teams_digest_time")

        app_public_url = (request.form.get("app_public_url") or "").strip().rstrip("/")
        if app_public_url:
            if not re.match(r"^https?://", app_public_url, re.IGNORECASE):
                flash("App public URL must start with http:// or https://.", "error")
                return redirect(url_for("settings"))
            set_setting("app_public_url", app_public_url)
        else:
            delete_setting("app_public_url")

        roster_raw = (request.form.get("operator_roster_json") or "").strip()
        try:
            roster = identity.parse_roster(roster_raw)
        except ValueError as exc:
            flash(f"Operator roster: {exc}", "error")
            return redirect(url_for("settings"))
        if roster:
            set_setting("operator_roster_json", json.dumps(roster, separators=(",", ":")))
        else:
            delete_setting("operator_roster_json")

        max_runs_per_day = (request.form.get("max_runs_per_day") or "").strip()
        if max_runs_per_day:
            try:
                max_runs_value = int(max_runs_per_day)
                if max_runs_value < 0:
                    raise ValueError
            except ValueError:
                flash("Max runs per day must be a whole number (0 = unlimited).", "error")
                return redirect(url_for("settings"))
            set_setting("max_runs_per_day", str(max_runs_value))
        else:
            delete_setting("max_runs_per_day")

        excess_cost = (request.form.get("excess_packlist_cost_usd") or "").strip()
        if excess_cost:
            try:
                excess_cost_value = float(excess_cost)
                if excess_cost_value < 0:
                    raise ValueError
            except ValueError:
                flash("Excess packlist cost must be a dollar amount of 0 or more.", "error")
                return redirect(url_for("settings"))
            set_setting("excess_packlist_cost_usd", f"{excess_cost_value:g}")
        else:
            delete_setting("excess_packlist_cost_usd")

        release_gate_mode = (request.form.get("release_gate_mode") or "advisory").strip().lower()
        if release_gate_mode not in {"off", "advisory", "enforced"}:
            flash("Release gate mode must be off, advisory, or enforced.", "error")
            return redirect(url_for("settings"))
        due_days_raw = (
            request.form.get("release_gate_due_override_days") or "1"
        ).strip()
        try:
            due_days = int(due_days_raw)
            if due_days < 0 or due_days > 30:
                raise ValueError
        except ValueError:
            flash("Commitment-protection days must be a whole number from 0 to 30.", "error")
            return redirect(url_for("settings"))
        policies_raw = (
            request.form.get("release_gate_customer_policies_json") or "{}"
        ).strip()
        try:
            policies = parse_release_gate_customer_policies(policies_raw)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings"))
        set_setting("release_gate_mode", release_gate_mode)
        set_setting("release_gate_due_override_days", str(due_days))
        set_setting(
            "release_gate_customer_policies_json",
            json.dumps(policies, sort_keys=True),
        )
        ensure_release_gate_policy_version(
            changed_by=f"settings:{request.remote_addr or 'unknown'}"
        )
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )

        set_setting("smtp_use_tls", "true" if request.form.get("smtp_use_tls") else "false")

        for feature_name, feature_def in FEATURE_FLAGS.items():
            set_setting(
                feature_def["setting_key"],
                "true" if request.form.get(f"feature_{feature_name}") else "false",
            )

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
    mssql_write_value = get_config_value(
        "mssql_write_connection_string", "MSSQL_WRITE_CONNECTION_STRING"
    )
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
    release_policies = get_release_gate_customer_policies()

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
        mssql_write_connection_string_masked=mask_connection_string(mssql_write_value)
        if mssql_write_value
        else "Not configured (saves use the main connection)",
        mssql_write_source=get_config_source(
            "mssql_write_connection_string", "MSSQL_WRITE_CONNECTION_STRING"
        ),
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
        max_runs_per_day=get_max_runs_per_day(),
        max_runs_source=get_config_source("max_runs_per_day", "MAX_RUNS_PER_DAY"),
        excess_packlist_cost_usd=get_excess_packlist_cost(),
        excess_cost_source=get_config_source(
            "excess_packlist_cost_usd", "EXCESS_PACKLIST_COST_USD"
        ),
        release_gate_mode=get_release_gate_mode(),
        release_gate_due_override_days=get_release_gate_due_override_days(),
        release_gate_customer_policies_json=json.dumps(
            release_policies, indent=2, sort_keys=True
        ),
        release_gate_customer_policies=release_policies,
        teams_webhook_configured=bool(get_config_value("teams_webhook_url", "TEAMS_WEBHOOK_URL")),
        teams_webhook_source=get_config_source("teams_webhook_url", "TEAMS_WEBHOOK_URL"),
        teams_events=list(notifier.TEAMS_EVENTS.items()),
        teams_enabled_events=notifier.enabled_events(),
        teams_digest_time=get_teams_digest_time(),
        app_public_url=get_config_value("app_public_url", "APP_PUBLIC_URL", "") or "",
        operator_roster_json=json.dumps(get_operator_roster(), separators=(",", ":")),
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

    budget = get_run_budget(query_type)
    if budget["exhausted"]:
        flash(
            f"{query_type.capitalize()} has used its {budget['limit']} run"
            f"{'' if budget['limit'] == 1 else 's'} for the day. "
            f"Export the existing list instead — the next run unlocks {budget['resets_at_display']}.",
            "error",
        )
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
        if get_run_budget(query_type)["exhausted"]:
            all_succeeded = False
            results.append(f"{query_type}: skipped (daily run limit)")
            continue
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
        today_iso=datetime.now(resolve_timezone()).date().isoformat(),
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
        try:
            row["near_matches"] = audit_store.find_near_matches(
                session_id, row.get("scanned_serial") or ""
            )
        except Exception:  # noqa: BLE001 — hints are best-effort
            row["near_matches"] = []
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
        resolutions=audit_store.get_resolutions(session_id),
        resolution_codes=audit_store.RESOLUTION_CODES,
        manual_resolution_codes=audit_store.MANUAL_RESOLUTION_CODES,
        open_resolution_codes=audit_store.OPEN_RESOLUTION_CODES,
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
            "near_matches": result.get("near_matches") or [],
            "serial": serial,
            "location": location,
        }
    ), 200


@app.post("/api/audit/session/<int:session_id>/resolve")
@require_trusted_client
@require_csrf
def api_audit_resolve(session_id: int):
    # Allowed on both in-progress and completed sessions: exceptions found by a
    # completed audit are a punch list worked after the fact.
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    payload = request.get_json(silent=True) or {}
    serial = (payload.get("serial") or "").strip()
    resolution = (payload.get("resolution") or "").strip()
    note = (payload.get("note") or "").strip() or None
    operator = (payload.get("operator") or "").strip() or None
    if not serial:
        return jsonify({"error": "invalid", "message": "A serial number is required."}), 400
    if resolution not in audit_store.RESOLUTION_CODES:
        return jsonify({"error": "invalid", "message": "A valid resolution status is required."}), 400

    try:
        result = audit_store.record_resolution(session_id, serial, resolution, note, operator)
    except ValueError as exc:
        return jsonify({"error": "invalid", "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record audit resolution: %s", exc)
        return jsonify({"error": "resolve_failed", "message": str(exc)}), 500

    return jsonify(
        {
            "resolution": result["resolution"],
            "resolution_label": result["resolution_label"],
            "exception_type": result["exception_type"],
            "item": result["item"],
            "counts": result["counts"],
            "serial": serial,
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

    # Best-effort: annotate unexpected scans the ERP has since caught up with
    # (e.g. a WO receipt posted after the snapshot). Never blocks completion.
    try:
        recheck = recheck_unexpected_scans(session_id)
        resolved = sum(1 for r in recheck if r["auto_resolved"])
        if resolved:
            logger.info(
                "Audit session #%s: ERP re-check auto-resolved %d unexpected scan(s).",
                session_id,
                resolved,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ERP re-check of unexpected scans failed for session #%s: %s", session_id, exc)

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


@app.post("/api/audit/session/<int:session_id>/recheck")
@require_trusted_client
@require_csrf
def api_audit_recheck(session_id: int):
    """Re-query the ERP for this session's unexpected serials on demand."""
    if not audit_store.is_available():
        return jsonify({"error": "audit_unavailable", "message": AUDIT_UNAVAILABLE_MESSAGE}), 503

    session_row = audit_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Audit session not found."}), 404

    try:
        summary = recheck_unexpected_scans(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ERP re-check failed for session #%s: %s", session_id, exc)
        return jsonify({"error": "recheck_failed", "message": str(exc)}), 500

    return jsonify(
        {
            "checked": len(summary),
            "auto_resolved": sum(1 for r in summary if r["auto_resolved"]),
            "rows": summary,
        }
    ), 200


@app.get("/audit/export")
@require_trusted_client
def audit_day_export():
    """One CSV covering every audit session on a local calendar day.

    Answers "what was scanned today and what were the issues in each area"
    without opening each location's session: expected rows (verified /
    misplaced / missing) and unexpected scans, unioned, with resolutions.
    """
    if not audit_store.is_available():
        flash(AUDIT_UNAVAILABLE_MESSAGE, "error")
        return redirect(url_for("audit_dashboard"))

    tz = resolve_timezone()
    day_param = (request.args.get("date") or "").strip()
    if day_param:
        try:
            day = date.fromisoformat(day_param)
        except ValueError:
            flash(f"Invalid date '{day_param}' — use YYYY-MM-DD.", "error")
            return redirect(url_for("audit_dashboard"))
    else:
        day = datetime.now(tz).date()

    try:
        data = audit_store.day_export_rows(day.isoformat(), str(tz))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Audit day export failed: %s", exc)
        flash(f"Could not build the audit export: {exc}", "error")
        return redirect(url_for("audit_dashboard"))

    if not data["sessions"]:
        flash(f"No audit sessions were started on {day.isoformat()}.", "error")
        return redirect(url_for("audit_dashboard"))

    columns = [
        "session_id", "session_label", "operator", "result", "serial",
        "part_id", "part_description", "location_scope", "expected_location",
        "scanned_location", "scanned_at", "tied_wo", "sales_order",
        "resolution", "resolution_note", "resolved_by",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    def resolution_label(code):
        return audit_store.RESOLUTION_CODES.get(code, code) if code else ""

    for row in data["expected"]:
        writer.writerow(
            {
                "session_id": row["session_id"],
                "session_label": row.get("session_label") or "",
                "operator": row.get("operator") or "",
                "result": row.get("status") or "",
                "serial": row.get("serial") or "",
                "part_id": row.get("part_id") or "",
                "part_description": row.get("part_description") or "",
                "location_scope": row.get("scope") or "",
                "expected_location": row.get("expected_location") or "",
                "scanned_location": row.get("scanned_location") or "",
                "scanned_at": _audit_dt_display(row.get("scanned_at")) or "",
                "tied_wo": "yes" if row.get("tied_wo") else "",
                "sales_order": row.get("cust_order_id") or "",
                "resolution": resolution_label(row.get("resolution")),
                "resolution_note": row.get("resolution_note") or "",
                "resolved_by": row.get("resolved_by") or "",
            }
        )
    for row in data["unexpected"]:
        writer.writerow(
            {
                "session_id": row["session_id"],
                "session_label": row.get("session_label") or "",
                "operator": row.get("operator") or "",
                "result": "unexpected",
                "serial": row.get("serial") or "",
                "scanned_location": row.get("scanned_location") or "",
                "scanned_at": _audit_dt_display(row.get("scanned_at")) or "",
                "resolution": resolution_label(row.get("resolution")),
                "resolution_note": row.get("resolution_note") or "",
                "resolved_by": row.get("resolved_by") or "",
            }
        )

    payload = output.getvalue().encode("utf-8-sig")  # BOM so Excel opens it cleanly
    return send_file(
        io.BytesIO(payload),
        as_attachment=True,
        download_name=f"audit_{day.isoformat()}.csv",
        mimetype="text/csv",
    )


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
    resolutions = audit_store.get_resolutions(session_id)

    for item in items:
        res = resolutions.get((item.get("serial") or "").upper())
        item["resolution"] = audit_store.RESOLUTION_CODES.get(res["resolution"]) if res else None
        item["resolution_note"] = res["note"] if res else None
    for row in unexpected:
        res = resolutions.get((row.get("scanned_serial") or "").upper())
        row["resolution"] = audit_store.RESOLUTION_CODES.get(res["resolution"]) if res else None
        row["resolution_note"] = res["note"] if res else None
    resolution_rows = [
        {
            "serial": res["serial"],
            "exception_type": res["exception_type"],
            "resolution": audit_store.RESOLUTION_CODES.get(res["resolution"], res["resolution"]),
            "note": res["note"],
            "resolved_by": res["resolved_by"],
            "resolved_at": _audit_dt_display(res["resolved_at"]),
        }
        for res in resolutions.values()
    ]

    expected_df = pd.DataFrame(items)
    unexpected_df = pd.DataFrame(unexpected)
    resolutions_df = pd.DataFrame(resolution_rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (expected_df if not expected_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Expected"
        )
        (unexpected_df if not unexpected_df.empty else pd.DataFrame(columns=["scanned_serial"])).to_excel(
            writer, index=False, sheet_name="Unexpected"
        )
        (resolutions_df if not resolutions_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Resolutions"
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
# Allocation: per-SKU supply/demand model + Promise Del Date reprioritization
# ---------------------------------------------------------------------------
def _clean_part_id(value: Optional[str]) -> str:
    return (value or "").strip().upper()[:ALLOC_PART_ID_MAX_LENGTH]


def _allocation_options() -> dict[str, Any]:
    """Lookahead + excluded-customer terms, same source as the guns picklist."""
    options = get_default_guns_query_options()
    return {
        "lookahead_days": options["lookahead_days"],
        "excluded_customers": tuple(options["excluded_customers"]),
    }


def _fetch_allocation_inputs(part_id: str) -> tuple[list[dict], list[dict]]:
    supply_df = run_erp_query_file(
        ALLOC_SUPPLY_FILE, {"part_id": part_id}, f"allocation supply for {part_id}"
    )
    demand_df = run_erp_query_file(
        ALLOC_DEMAND_FILE, {"part_id": part_id}, f"allocation demand for {part_id}"
    )
    return (
        supply_df.to_dict(orient="records"),
        demand_df.to_dict(orient="records"),
    )


def _build_allocation_payload(
    part_id: str,
    overrides: Optional[dict] = None,
) -> dict:
    options = _allocation_options()
    supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    payload = allocation.build_allocation(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        excluded_customer_terms=options["excluded_customers"],
        overrides=overrides,
    )
    payload["part_id"] = part_id
    return payload


@app.get("/allocation")
@require_trusted_client
def allocation_page():
    prefill = _clean_part_id(request.args.get("part"))
    return render_template("allocation.html", prefill_part=prefill)


@app.get("/api/allocation/parts")
@require_trusted_client
def api_allocation_parts():
    term = (request.args.get("q") or "").strip().upper()
    if len(term) < ALLOC_PART_SEARCH_MIN_CHARS:
        return jsonify({"parts": []}), 200
    try:
        df = run_erp_query_file(
            ALLOC_PARTS_FILE,
            {"pattern": f"%{term}%", "prefix": f"{term}%"},
            f"allocation part search '{term}'",
        )
    except Exception:
        logger.exception("Allocation part search failed for %s", term)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    parts = [
        {
            "part_id": str(row.get("PART_ID") or ""),
            "description": row.get("DESCRIPTION") if row.get("DESCRIPTION") == row.get("DESCRIPTION") else None,
            "on_hand": int(row.get("ON_HAND") or 0),
            "open_demand": int(row.get("OPEN_DEMAND") or 0),
        }
        for row in df.to_dict(orient="records")
    ]
    return jsonify({"parts": parts}), 200


@app.get("/api/allocation/history")
@require_trusted_client
def api_allocation_history():
    part_id = _clean_part_id(request.args.get("part_id")) or None
    so = (request.args.get("so") or "").strip() or None
    if not part_id and not so:
        return jsonify({"error": "invalid", "message": "part_id or so is required."}), 400
    changes = allocation_store.recent_changes(part_id=part_id, cust_order_id=so)
    return jsonify({"changes": changes}), 200


@app.get("/api/allocation/<part_id>")
@require_trusted_client
def api_allocation_detail(part_id: str):
    part_id = _clean_part_id(part_id)
    if not part_id:
        return jsonify({"error": "invalid", "message": "A part number is required."}), 400
    try:
        payload = _build_allocation_payload(part_id)
    except Exception:
        logger.exception("Allocation lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    if (
        not payload["supply"]["events"]
        and not payload["supply"]["informational"]
        and not payload["demand"]["lines"]
    ):
        return (
            jsonify(
                {
                    "error": "part_not_found",
                    "message": f"No open demand, stock, or production found for {part_id}.",
                }
            ),
            404,
        )
    return jsonify(payload), 200


def _parse_iso_date_field(payload: dict, field: str) -> tuple[Optional[date], Optional[str]]:
    """(value, error). None is a legal value for both promise-del fields."""
    raw = payload.get(field)
    if raw in (None, ""):
        return None, None
    try:
        return date.fromisoformat(str(raw)), None
    except ValueError:
        return None, f"{field} must be a YYYY-MM-DD date."


@app.post("/api/allocation/preview")
@require_trusted_client
@require_csrf
def api_allocation_preview():
    body = request.get_json(silent=True) or {}
    part_id = _clean_part_id(body.get("part_id"))
    so = (body.get("so") or "").strip()
    line_no = body.get("line_no")
    if not part_id or not so or not isinstance(line_no, int):
        return (
            jsonify({"error": "invalid", "message": "part_id, so, and line_no are required."}),
            400,
        )
    new_value, error = _parse_iso_date_field(body, "new_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400

    options = _allocation_options()
    try:
        supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    except Exception:
        logger.exception("Allocation preview lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    preview = allocation.preview_change(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        options["excluded_customers"],
        so,
        line_no,
        new_value,
    )
    preview["result"]["part_id"] = part_id
    return jsonify(preview), 200


@app.get("/api/allocation/suggest")
@require_trusted_client
def api_allocation_suggest():
    part_id = _clean_part_id(request.args.get("part_id"))
    so = (request.args.get("so") or "").strip()
    try:
        line_no = int(request.args.get("line_no", ""))
        target_position = int(request.args.get("target_position", ""))
    except ValueError:
        return (
            jsonify({"error": "invalid", "message": "line_no and target_position must be numbers."}),
            400,
        )
    if not part_id or not so:
        return jsonify({"error": "invalid", "message": "part_id and so are required."}), 400

    options = _allocation_options()
    try:
        supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    except Exception:
        logger.exception("Allocation suggest lookup failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Check the SQL Server connection and try again.",
                }
            ),
            500,
        )
    suggestion = allocation.suggest_promise_del(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        options["excluded_customers"],
        so,
        line_no,
        target_position,
    )
    status = 400 if suggestion.get("error") else 200
    return jsonify(suggestion), status


class _AllocationSaveConflict(Exception):
    def __init__(self, current_value: Optional[date]):
        super().__init__("Promise Del Date changed since the screen was loaded.")
        self.current_value = current_value


@app.post("/api/allocation/promise-del")
@require_trusted_client
@require_csrf
def api_allocation_save():
    body = request.get_json(silent=True) or {}
    part_id = _clean_part_id(body.get("part_id"))
    so = (body.get("so") or "").strip()
    line_no = body.get("line_no")
    changed_by = (body.get("changed_by") or "").strip()
    reason = (body.get("reason") or "").strip() or None
    if not part_id or not so or not isinstance(line_no, int):
        return (
            jsonify({"error": "invalid", "message": "part_id, so, and line_no are required."}),
            400,
        )
    if not changed_by:
        return (
            jsonify(
                {
                    "error": "invalid",
                    "message": "Set your operator name before saving — the audit trail requires it.",
                }
            ),
            400,
        )
    new_value, error = _parse_iso_date_field(body, "new_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400
    expected_old_value, error = _parse_iso_date_field(body, "expected_old_value")
    if error:
        return jsonify({"error": "invalid", "message": error}), 400

    # Server-computed baseline: never trust the client's idea of its position.
    try:
        baseline = _build_allocation_payload(part_id)
    except Exception:
        logger.exception("Allocation save baseline failed for %s", part_id)
        return (
            jsonify(
                {
                    "error": "lookup_failed",
                    "message": "The ERP lookup failed. Nothing was saved.",
                }
            ),
            500,
        )
    position_before = None
    line_found = False
    for line in baseline["demand"]["lines"]:
        if line["so"] == so and line["line_no"] == line_no:
            position_before = line["position"]
            line_found = True
            break
    if not line_found:
        return (
            jsonify(
                {
                    "error": "line_not_found",
                    "message": f"{so} line {line_no} has no open demand for {part_id}.",
                }
            ),
            404,
        )

    audit_id: Optional[int] = None
    erp_updated = False
    old_value_iso: Optional[str] = None
    try:
        engine = get_erp_write_engine()
        with engine.begin() as conn:
            row = (
                conn.execute(text(ALLOC_SELECT_LINE_SQL), {"so": so, "line": line_no})
                .mappings()
                .first()
            )
            if row is None:
                return (
                    jsonify(
                        {
                            "error": "line_not_found",
                            "message": f"{so} line {line_no} was not found in VISUAL.",
                        }
                    ),
                    404,
                )
            row_part = str(row["PART_ID"] or "").strip().upper()
            if row_part != part_id:
                return (
                    jsonify(
                        {
                            "error": "invalid",
                            "message": f"{so} line {line_no} is {row_part}, not {part_id}.",
                        }
                    ),
                    400,
                )
            current = row["PROMISE_DEL_DATE"]
            if isinstance(current, datetime):
                current = current.date()
            old_value_iso = current.isoformat() if current else None

            result = conn.execute(
                text(ALLOC_UPDATE_SQL),
                {
                    "new_value": new_value,
                    "so": so,
                    "line": line_no,
                    "old_value": expected_old_value,
                },
            )
            if result.rowcount == 0:
                raise _AllocationSaveConflict(current)
            erp_updated = True

            # Inside the ERP transaction on purpose: if the audit row cannot
            # be written, the ERP change must not survive.
            audit_id = allocation_store.record_change(
                changed_by=changed_by,
                cust_order_id=so,
                line_no=line_no,
                part_id=part_id,
                old_value=old_value_iso,
                new_value=new_value.isoformat() if new_value else None,
                reason=reason,
                position_before=position_before,
                position_after=None,
            )
    except _AllocationSaveConflict as conflict:
        return (
            jsonify(
                {
                    "error": "conflict",
                    "message": "Promise Del Date changed since this screen was loaded. Reloaded value shown — review and retry.",
                    "current_value": conflict.current_value.isoformat()
                    if conflict.current_value
                    else None,
                }
            ),
            409,
        )
    except Exception:
        if erp_updated and audit_id is None:
            logger.exception(
                "Audit write failed for %s line %s; ERP update rolled back.", so, line_no
            )
            return (
                jsonify(
                    {
                        "error": "audit_failed",
                        "message": "The audit trail could not be written, so the change was rolled back.",
                    }
                ),
                500,
            )
        if audit_id is not None:
            # Commit itself failed after the audit insert: compensate.
            try:
                allocation_store.delete_change(audit_id)
            except Exception:
                logger.exception("Compensating audit delete failed for id %s", audit_id)
        logger.exception("Promise Del save failed for %s line %s", so, line_no)
        return (
            jsonify(
                {
                    "error": "save_failed",
                    "message": "The save failed. Nothing was changed in VISUAL.",
                }
            ),
            500,
        )

    logger.info(
        "Promise Del Date for %s line %s (%s) changed %s -> %s by %s.",
        so,
        line_no,
        part_id,
        old_value_iso or "blank",
        new_value.isoformat() if new_value else "blank",
        changed_by,
    )

    fresh_payload: Optional[dict] = None
    position_after = None
    warning = None
    try:
        fresh_payload = _build_allocation_payload(part_id)
        for line in fresh_payload["demand"]["lines"]:
            if line["so"] == so and line["line_no"] == line_no:
                position_after = line["position"]
                break
        allocation_store.set_position_after(audit_id, position_after)
    except Exception:
        logger.exception("Post-save allocation rebuild failed for %s", part_id)
        warning = "Saved, but the refreshed allocation could not be loaded. Reload the page."

    return (
        jsonify(
            {
                "status": "saved",
                "audit_id": audit_id,
                "old_value": old_value_iso,
                "new_value": new_value.isoformat() if new_value else None,
                "position_before": position_before,
                "position_after": position_after,
                "result": fresh_payload,
                "warning": warning,
            }
        ),
        200,
    )


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


def render_release_candidates_query(
    query_template: str,
    query_options: Optional[dict[str, Any]] = None,
) -> str:
    options = get_default_guns_query_options()
    if query_options:
        options = {**options, **query_options}
    component_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(code)} AS PRODUCT_CODE"
        for code in SHORTAGE_PRODUCT_CODES
    ) or "SELECT '' AS PRODUCT_CODE"
    excluded_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(term)} AS CUSTOMER_TERM"
        for term in options["excluded_customers"]
    ) or "SELECT '' AS CUSTOMER_TERM"
    return (
        query_template.replace(RELEASE_LOOKAHEAD_TOKEN, str(options["lookahead_days"]))
        .replace(RELEASE_COMPONENT_CODES_TOKEN, component_rows)
        .replace(RELEASE_EXCLUDED_CUSTOMERS_TOKEN, excluded_rows)
    )


_release_gate_cache: dict[str, Any] = {
    "payload": None,
    "fetched_at": None,
    "signature": None,
}


def _fetch_release_candidate_rows(
    query_options: Optional[dict[str, Any]] = None,
) -> list[dict]:
    if not RELEASE_CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"Release candidate query not found at: {RELEASE_CANDIDATES_FILE}"
        )
    template = RELEASE_CANDIDATES_FILE.read_text(encoding="utf-8")
    query = render_release_candidates_query(template, query_options=query_options)
    engine = get_erp_engine()
    logger.info("Running order release candidate query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Order release candidate query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def _fetch_release_serial_rows(part_ids: Iterable[str]) -> list[dict]:
    if not RELEASE_SERIAL_SUPPLY_FILE.exists():
        raise FileNotFoundError(
            f"Release serial-supply query not found at: {RELEASE_SERIAL_SUPPLY_FILE}"
        )
    parts = sorted({str(part).strip().upper() for part in part_ids if str(part).strip()})
    template = RELEASE_SERIAL_SUPPLY_FILE.read_text(encoding="utf-8")
    if RELEASE_SERIAL_PART_FILTER_TOKEN not in template:
        raise ValueError(
            f"Release serial query must contain {RELEASE_SERIAL_PART_FILTER_TOKEN}."
        )
    if parts:
        literals = ", ".join(sql_quote_literal(part) for part in parts)
        part_filter = f"AND tit.PART_ID IN ({literals})"
    else:
        part_filter = "AND 1 = 0"
    query = template.replace(RELEASE_SERIAL_PART_FILTER_TOKEN, part_filter)
    engine = get_erp_engine()
    logger.info("Running release-gate serial supply query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Release serial-supply query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def _fetch_release_shipto_history_rows() -> list[dict]:
    if not RELEASE_SHIPTO_HISTORY_FILE.exists():
        raise FileNotFoundError(
            f"Release ship-to history query not found at: {RELEASE_SHIPTO_HISTORY_FILE}"
        )
    query = RELEASE_SHIPTO_HISTORY_FILE.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running release-gate ship-to history query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Release ship-to history query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def build_release_gate_payload(
    force: bool = False,
    query_options: Optional[dict[str, Any]] = None,
    require_serial_tracking: bool = False,
    persist: bool = False,
) -> dict[str, Any]:
    # persist=True is reserved for the operational boundary (picklist generation):
    # only then are sticky serial reservations reconciled and an audit evaluation
    # recorded. Dashboard and API reads stay side-effect free.
    policy = ensure_release_gate_policy_version()
    signature = json.dumps(
        {"version": policy["version"], "query_options": query_options or {}},
        sort_keys=True,
        default=str,
    )
    cached_at = _release_gate_cache["fetched_at"]
    if (
        not force
        and _release_gate_cache["payload"] is not None
        and _release_gate_cache["signature"] == signature
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(seconds=RELEASE_GATE_CACHE_SECONDS)
    ):
        return _release_gate_cache["payload"]

    evaluated_at = datetime.now(timezone.utc)
    source_as_of = evaluated_at.isoformat()
    try:
        rows = _fetch_release_candidate_rows(query_options=query_options)
        gun_parts = {
            str(row.get("PART_ID") or "").strip().upper()
            for row in rows
            if str(row.get("ITEM_TYPE") or "guns").strip().lower() == "guns"
            and float(row.get("AVAILABLE_QTY") or 0) > 0
            and str(row.get("PART_ID") or "").strip()
        }
        try:
            serial_rows: Optional[list[dict]] = _fetch_release_serial_rows(gun_parts)
            serial_error = None
        except Exception as serial_exc:  # noqa: BLE001 - advisory may show quantity fallback
            logger.exception("Release-gate serial supply query failed")
            serial_rows = None
            serial_error = str(serial_exc)
            if require_serial_tracking:
                raise RuntimeError(
                    "Live serial reservation validation is required for enforced picklists: "
                    f"{serial_exc}"
                ) from serial_exc
        try:
            ship_to_history: Optional[list[dict]] = _fetch_release_shipto_history_rows()
            ship_to_history_error = None
        except Exception as ship_to_exc:  # noqa: BLE001 - advisory exposes degraded check
            logger.exception("Release-gate ship-to history query failed")
            ship_to_history = None
            ship_to_history_error = str(ship_to_exc)
            if require_serial_tracking:
                raise RuntimeError(
                    "Live ship-to cooldown validation is required for enforced picklists: "
                    f"{ship_to_exc}"
                ) from ship_to_exc
        if ship_to_history is not None:
            # A picklist generated earlier today has no SHIPPED_DATE in VISUAL yet;
            # the local release ledger closes that same-day duplicate window.
            try:
                ship_to_history = list(ship_to_history) + (
                    shipping_store.recent_released_ship_tos()
                )
            except Exception:  # noqa: BLE001 - ledger is supplemental history
                logger.exception("Local release ledger read failed")
        existing_reservations = shipping_store.serial_reservations_for_gate()
        payload = release_gate.evaluate_release_gate(
            rows,
            today=_today_local(),
            mode=policy["mode"],
            due_override_days=policy["due_override_days"],
            customer_policies=policy["customer_policies"],
            exceptions=shipping_store.active_exceptions(),
            serial_inventory=serial_rows,
            reservations=existing_reservations,
            ship_to_history=ship_to_history,
            local_now=evaluated_at.astimezone(resolve_timezone()),
            policy_version=policy["version"],
            evaluated_at=evaluated_at,
            min_ship_to_cooldown_days=get_release_gate_min_ship_to_cooldown_days(),
            manual_holds=_active_manual_holds(),
        )
        if persist and serial_rows is not None:
            payload["reservation_sync"] = shipping_store.sync_serial_reservations(
                desired=payload.get("reservation_updates") or [],
                live_serials=serial_rows,
                evaluated_at=payload["evaluated_at"],
                policy_version=policy["version"],
            )
        else:
            payload["reservation_sync"] = None
        payload["serial_tracking_error"] = serial_error
        payload["ship_to_tracking_available"] = ship_to_history is not None
        payload["ship_to_tracking_error"] = ship_to_history_error
        payload["serial_reservations"] = shipping_store.recent_serial_reservations()
        payload["source_as_of"] = source_as_of
        payload["error"] = None
        payload["active_exceptions"] = shipping_store.active_exceptions()
        payload["evaluation_id"] = (
            shipping_store.record_evaluation(payload, source_as_of=source_as_of)
            if persist
            else None
        )
    except Exception as exc:  # noqa: BLE001 - dashboard degrades; enforced runs fail closed
        logger.exception("Release gate evaluation failed")
        payload = {
            "mode": policy["mode"],
            "policy_version": policy["version"],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "source_as_of": source_as_of,
            "due_override_days": policy["due_override_days"],
            "summary": {
                "orders": 0,
                "release": 0,
                "accumulating": 0,
                "hold": 0,
                "blocked": 0,
                "release_units": 0,
                "protected_units": 0,
                "protected_guns": 0,
                "held_ready_units": 0,
            },
            "released_orders": [],
            "decisions": [],
            "active_exceptions": shipping_store.active_exceptions(),
            "serial_tracking_available": False,
            "serial_tracking_error": str(exc),
            "ship_to_tracking_available": False,
            "ship_to_tracking_error": str(exc),
            "serial_reservations": shipping_store.recent_serial_reservations(),
            "reservation_updates": [],
            "error": str(exc),
        }
    _release_gate_cache.update(
        {"payload": payload, "fetched_at": datetime.now(), "signature": signature}
    )
    return payload


_shipping_scorecard_cache: dict[tuple[int, str], dict[str, Any]] = {}


def parse_scorecard_days(raw: Any) -> int:
    try:
        value = int(str(raw or "30").strip())
    except (TypeError, ValueError):
        return 30
    return value if value in {1, 7, 30, 90} else 30


def _fetch_shipping_metric_rows(start: date, end: date) -> list[dict]:
    span = end - start
    query_start = start - span
    df = run_erp_query_file(
        SHIPPING_METRICS_FILE,
        {"query_start": query_start.isoformat(), "end_date": end.isoformat()},
        "shipping management metrics",
    )
    return df.to_dict(orient="records")


def _empty_scorecard_payload(days: int, error: str) -> dict[str, Any]:
    today = _today_local()
    cards = {
        key: {
            **definition,
            "value": None,
            "prior_value": None,
            "delta": None,
            "numerator": None,
            "denominator": None,
            "companion": {},
        }
        for key, definition in shipping_metrics.METRIC_DEFINITIONS.items()
    }
    return {
        "error": error,
        "stale": False,
        "as_of": None,
        "period": {
            "start": (today - timedelta(days=days - 1)).isoformat(),
            "end_exclusive": (today + timedelta(days=1)).isoformat(),
            "days": days,
            "latest_complete_date": (today - timedelta(days=1)).isoformat(),
            "is_partial": True,
        },
        "cards": cards,
        "trends": [],
        "actions": {"late_orders": [], "single_shipments": []},
        "coverage": {},
        "source": {},
    }


def build_shipping_scorecard_payload(days: int = 30, force: bool = False) -> dict[str, Any]:
    days = parse_scorecard_days(days)
    today = _today_local()
    cache_key = (days, today.isoformat())
    cached = _shipping_scorecard_cache.get(cache_key)
    if (
        not force
        and cached
        and datetime.now() - cached["_cached_at"]
        < timedelta(minutes=SHIPPING_METRICS_CACHE_MINUTES)
    ):
        return cached["payload"]

    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    try:
        rows = _fetch_shipping_metric_rows(start, end)
        payload = shipping_metrics.build_shipping_metrics(
            rows, start, end, as_of=datetime.now(timezone.utc)
        )
        payload["period"]["is_partial"] = True
        payload["period"]["latest_complete_date"] = (
            today - timedelta(days=1)
        ).isoformat()
        payload["error"] = None
        payload["stale"] = False
        try:
            shipping_store.save_metric_snapshot(
                snapshot_date=today,
                period_days=days,
                payload=payload,
                source_as_of=payload.get("as_of"),
            )
        except Exception as exc:  # noqa: BLE001 - history should not hide fresh metrics
            logger.warning("Could not save shipping metric snapshot: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Shipping management metric query failed")
        snapshot = shipping_store.get_metric_snapshot(today, days)
        if snapshot:
            payload = snapshot
            payload["error"] = f"Live refresh failed; showing the latest saved snapshot: {exc}"
            payload["stale"] = True
        else:
            payload = _empty_scorecard_payload(days, str(exc))

    _shipping_scorecard_cache[cache_key] = {
        "payload": payload,
        "_cached_at": datetime.now(),
    }
    return payload


_excess_cache: dict[str, Any] = {"payload": None, "fetched_at": None}


def _fetch_excess_rows(today: date) -> list[dict]:
    """SHIPPER headers shipped since the month containing (today - 34 days),
    plus anything created today that has not shipped yet. Starting at a month
    boundary keeps calendar month-to-date fully inside the window even when
    the rolling 34 days would start mid-month."""
    anchor = today - timedelta(days=34)
    df = run_erp_query_file(
        EXCESS_PACKLISTS_FILE,
        {
            "start_date": anchor.replace(day=1).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "today_start": today.isoformat(),
        },
        "excess packlists",
    )
    return df.to_dict(orient="records")


def build_excess_packlist_payload(force: bool = False) -> dict[str, Any]:
    """Cached excess-packlist payload; degrades to an error message when ERP is down."""
    cost = get_excess_packlist_cost()
    cached = _excess_cache["payload"]
    cached_at = _excess_cache["fetched_at"]
    if (
        not force
        and cached is not None
        and cached_at is not None
        and datetime.now() - cached_at < timedelta(minutes=EXCESS_CACHE_MINUTES)
        # A cost change in settings invalidates the cache immediately.
        and (cached.get("summary") or {}).get("cost_per_excess") == round(cost, 2)
    ):
        return cached
    today = _today_local()
    try:
        rows = _fetch_excess_rows(today)
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Excess packlist query failed")
        return {
            "error": str(exc),
            "summary": None,
            "fixable": [],
            "groups": [],
        }
    payload = excess.build_excess(rows, today, cost)
    payload["error"] = None
    payload["as_of"] = datetime.now(timezone.utc)
    _excess_cache["payload"] = payload
    _excess_cache["fetched_at"] = datetime.now()
    return payload


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


# Verify dashboard: only the ERP day pull is cached — the local session join
# is recomputed every request so completed scans show up immediately.
_verify_daily_cache: dict[str, Any] = {"date": None, "rows": None, "fetched_at": None}


def _fetch_packlists_created_on(day_iso: str, force: bool = False) -> list[dict]:
    now = datetime.now()
    if (
        not force
        and _verify_daily_cache["rows"] is not None
        and _verify_daily_cache["date"] == day_iso
        and _verify_daily_cache["fetched_at"] is not None
        and now - _verify_daily_cache["fetched_at"]
        < timedelta(seconds=VERIFY_DAILY_CACHE_SECONDS)
    ):
        return _verify_daily_cache["rows"]
    end_iso = (date.fromisoformat(day_iso) + timedelta(days=1)).isoformat()
    df = run_erp_query_file(
        PACKLIST_DAILY_FILE,
        {"start_date": day_iso, "end_date": end_iso},
        "daily packlists",
    )
    rows = df.to_dict(orient="records")
    _verify_daily_cache.update({"date": day_iso, "rows": rows, "fetched_at": now})
    return rows


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


def _verify_session_display(session_row: dict) -> dict:
    row = dict(session_row)
    row["started_display"] = _audit_dt_display(row.get("started_at"))
    row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    return row


def _session_started_local_date(session_row: dict) -> Optional[str]:
    started = session_row.get("started_at")
    if not started:
        return None
    try:
        parsed = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(resolve_timezone()).date().isoformat()


def build_verify_daily_payload(
    requested_date: Optional[str], force: bool = False
) -> dict[str, Any]:
    """Every packlist created on the requested day lined up against its
    latest verification session, plus that day's sessions for older packlists."""
    day_iso = (requested_date or "").strip() or _today_local().isoformat()
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        day_iso = _today_local().isoformat()

    payload: dict[str, Any] = {
        "date": day_iso,
        "today": _today_local().isoformat(),
        "error": None,
        "packlists": [],
        "other_sessions": [],
        "summary": {},
    }

    try:
        erp_rows = _fetch_packlists_created_on(day_iso, force=force)
    except Exception as exc:  # noqa: BLE001 — degrade like the other ERP panels
        logger.exception("Daily packlist query failed")
        payload["error"] = f"Could not load packlists from the ERP: {exc}"
        erp_rows = []

    packlist_ids = [str(r.get("PACKLIST_ID") or "") for r in erp_rows]
    sessions_by_packlist = verify_store.latest_sessions_for_packlists(packlist_ids)

    summary = {
        "created": 0,
        "verified_clean": 0,
        "verified_issues": 0,
        "in_progress": 0,
        "not_verified": 0,
        "no_serials": 0,
        "voided": 0,
    }
    for row in erp_rows:
        packlist_id = str(row.get("PACKLIST_ID") or "").strip().upper()
        shipper_status = str(row.get("SHIPPER_STATUS") or "").strip().upper()
        serial_count = int(row.get("SERIAL_COUNT") or 0)
        session_row = sessions_by_packlist.get(packlist_id)
        if shipper_status in ("X", "V"):
            status = "voided"
        elif session_row and session_row.get("status") == "active":
            status = "in_progress"
        elif session_row and session_row.get("status") == "completed":
            status = (
                "verified_clean"
                if session_row.get("outcome") == verify_store.OUTCOME_CLEAN
                else "verified_issues"
            )
        elif serial_count == 0:
            status = "no_serials"
        else:
            status = "not_verified"
        summary["created"] += 1
        summary[status] += 1
        payload["packlists"].append(
            {
                "packlist_id": packlist_id,
                "created_display": _erp_local_time_display(row.get("CREATE_DATE")),
                "cust_order_id": row.get("CUST_ORDER_ID"),
                "customer": row.get("CUSTOMER_NAME") or row.get("CUSTOMER_ID"),
                "line_count": int(row.get("LINE_COUNT") or 0),
                "serial_count": serial_count,
                "status": status,
                "session": _verify_session_display(session_row) if session_row else None,
            }
        )
    payload["summary"] = summary

    # Put work that needs an operator first. The ERP query otherwise mixes
    # actionable rows with non-serialized packlists that need no verification.
    verify_status_order = {
        "in_progress": 0,
        "not_verified": 1,
        "verified_issues": 2,
        "verified_clean": 3,
        "no_serials": 4,
        "voided": 5,
    }
    payload["packlists"].sort(
        key=lambda row: (
            verify_status_order.get(str(row.get("status")), 99),
            str(row.get("created_display") or ""),
            str(row.get("packlist_id") or ""),
        )
    )

    # Sessions started this day for packlists created on other days
    # (re-verifying an older box) still deserve a spot on the dashboard.
    day_packlists = {p["packlist_id"] for p in payload["packlists"]}
    for session_row in verify_store.recent_sessions(limit=100):
        if session_row.get("packlist_id") in day_packlists:
            continue
        if _session_started_local_date(session_row) != day_iso:
            continue
        payload["other_sessions"].append(_verify_session_display(session_row))

    return payload


# /shipping?view=... serves two kinds of page. WORK_VIEWS are the scan-and-do
# screens that sit under the Work tab; REPORT_VIEWS are read-only and sit
# under Reports. The nav in _topnav.html groups them accordingly.
WORK_VIEWS = {
    "pick": "Pick orders",
    "verify": "Verify boxes",
}
REPORT_VIEWS = {
    "scorecard": "Scorecard",
    "shortages": "Shortages",
    "recon": "Reconciliation",
    "excess": "Excess packlists",
    "stage": "Staged shipments",
}
SHIPPING_VIEWS = {**WORK_VIEWS, **REPORT_VIEWS}
# Sub-views that used to live here and now have their own page. Old links and
# bookmarks land on the replacement.
RETIRED_SHIPPING_VIEWS = {
    "work": "work_page",
    "holds": "orders_page",
    "requests": "requests_page",
}


def build_pick_order_queue() -> dict[str, Any]:
    """Combine the latest guns/components plans into one order work queue."""
    source_runs: dict[str, int] = {}
    plan_rows: list[dict[str, Any]] = []
    for query_type in QUERY_FILES:
        run, rows = get_latest_successful_run(query_type=query_type)
        if not run:
            continue
        source_runs[query_type] = int(run["id"])
        for row in rows:
            plan_rows.append({**row, "_query_type": query_type})

    claimed = pick_store.claimed_orders()
    by_order: dict[str, dict[str, Any]] = {}
    for row in plan_rows:
        order_id = str(row.get("Cust Order ID") or "").strip().upper()
        if not order_id:
            continue
        entry = by_order.setdefault(
            order_id,
            {
                "order_id": order_id,
                "customer_id": str(row.get("Customer ID") or "").strip(),
                "guns": 0,
                "components": 0,
                "units": 0,
                "locations": set(),
                "desired_ship_date": None,
                "claimed": order_id in claimed,
            },
        )
        try:
            quantity = int(float(row.get("SO Qty") or 0))
        except (TypeError, ValueError):
            quantity = 0
        item_type = str(row.get("_query_type") or "").lower()
        entry[item_type] = int(entry.get(item_type) or 0) + quantity
        entry["units"] += quantity
        location = str(row.get("Location") or "").strip()
        if location:
            entry["locations"].add(location)
        desired = str(row.get("Desired Ship Date") or "").strip() or None
        if desired and (
            entry["desired_ship_date"] is None or desired < entry["desired_ship_date"]
        ):
            entry["desired_ship_date"] = desired

    orders = []
    for entry in by_order.values():
        entry["locations"] = sorted(entry["locations"])
        orders.append(entry)
    orders.sort(
        key=lambda row: (
            bool(row["claimed"]),
            str(row.get("desired_ship_date") or "9999-12-31"),
            row["order_id"],
        )
    )
    return {"orders": orders, "plan_rows": plan_rows, "source_runs": source_runs}


def _recent_sessions_for_display(store: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = store.recent_sessions(limit=limit)
    for row in rows:
        row["started_display"] = _audit_dt_display(row.get("started_at"))
        row["completed_display"] = _audit_dt_display(row.get("completed_at"))
    return rows


def _latest_success_by_type() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for query_type in QUERY_FILES:
        summary = get_latest_successful_run_summary(query_type)
        out[query_type] = (
            {
                "id": summary["id"],
                "row_count": summary["row_count"],
                "run_timestamp_display": format_run_timestamp(summary["run_timestamp"]),
            }
            if summary
            else None
        )
    return out


@app.get("/work")
@require_trusted_client
def work_page():
    """Today: the one screen that starts or resumes the day's shipping work."""
    flags = get_feature_flags()
    pick_sessions: list[dict[str, Any]] = []
    verify_sessions: list[dict[str, Any]] = []
    if flags["shipping"]:
        pick_sessions = _recent_sessions_for_display(pick_store)
        verify_sessions = _recent_sessions_for_display(verify_store)
    shipping_requests: list[dict[str, Any]] = []
    if flags["requests"]:
        try:
            shipping_requests = request_store.list_requests(owner_team="shipping", open_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Open request list failed: %s", exc)
    return render_template(
        "work.html",
        pick_sessions=pick_sessions,
        verify_sessions=verify_sessions,
        latest_success_by_type=_latest_success_by_type(),
        run_state_by_type=get_run_state_snapshot(),
        query_options=list(QUERY_FILES.keys()),
        shipping_requests=_audit_json_safe(shipping_requests),
    )


@app.get("/runs")
@require_trusted_client
def run_history_page():
    query_types, latest_runs_by_type, recent_runs_by_type, latest_success_age_by_type = (
        build_dashboard_data(recent_limit=25)
    )
    return render_template(
        "run_history.html",
        query_options=query_types,
        latest_runs_by_type=latest_runs_by_type,
        recent_runs_by_type=recent_runs_by_type,
        latest_success_age_by_type=latest_success_age_by_type,
        timezone_label=get_timezone_label(),
    )


@app.get("/lookup")
@require_trusted_client
def lookup_page():
    return render_template("lookup.html")


@app.get("/shipping")
@require_trusted_client
def shipping_page():
    view = request.args.get("view") or "scorecard"
    if view in RETIRED_SHIPPING_VIEWS:
        return redirect(url_for(RETIRED_SHIPPING_VIEWS[view]))
    if view not in SHIPPING_VIEWS:
        view = "scorecard"

    recon_payload = None
    stage = None
    shortages = None
    excess_payload = None
    verify_daily = None
    pick_sessions: list[dict[str, Any]] = []
    latest_success_by_type: dict[str, Any] = {}
    pick_orders: list[dict[str, Any]] = []
    ready_for_pack: list[dict[str, Any]] = []
    scorecard = None
    release_gate_payload = None
    hold_stats = None
    request_stats = None
    scorecard_days = parse_scorecard_days(request.args.get("days"))

    if view == "scorecard":
        scorecard = _audit_json_safe(
            build_shipping_scorecard_payload(scorecard_days)
        )
        release_gate_payload = _audit_json_safe(build_release_gate_payload())
        try:
            hold_stats = readiness_store.hold_durations(scorecard_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hold duration stats failed: %s", exc)
        try:
            request_stats = request_store.queue_summary(scorecard_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Request queue stats failed: %s", exc)
    elif view == "recon":
        recon_payload = _audit_json_safe(build_recon_payload(request.args.get("date")))
    elif view == "shortages":
        shortages = _audit_json_safe(build_shortage_payload())
    elif view == "excess":
        excess_payload = _audit_json_safe(build_excess_packlist_payload())
    elif view == "stage":
        stage = _audit_json_safe(build_stage_aging())
    elif view == "verify":
        verify_daily = _audit_json_safe(
            build_verify_daily_payload(request.args.get("date"))
        )
    elif view == "pick":
        pick_sessions = _recent_sessions_for_display(pick_store)
        latest_success_by_type = _latest_success_by_type()
        pick_orders = build_pick_order_queue()["orders"]
        ready_for_pack = pick_store.ready_for_pack_orders(limit=100)
        for row in ready_for_pack:
            row["completed_display"] = _audit_dt_display(row.get("completed_at"))

    return render_template(
        "shipping.html",
        view=view,
        view_options=SHIPPING_VIEWS,
        recon=recon_payload,
        stage=stage,
        shortages=shortages,
        excess=excess_payload,
        verify_daily=verify_daily,
        pick_sessions=pick_sessions,
        latest_success_by_type=latest_success_by_type,
        query_options=list(QUERY_FILES.keys()),
        pick_orders=pick_orders,
        ready_for_pack=ready_for_pack,
        max_pick_orders=pick_store.MAX_ORDERS_PER_SESSION,
        today_iso=_today_local().isoformat(),
        scorecard=scorecard,
        scorecard_days=scorecard_days,
        release_gate=release_gate_payload,
        hold_stats=hold_stats,
        request_stats=request_stats,
        hold_reason_labels={code: meta["label"] for code, meta in readiness.HOLD_REASONS.items()},
        hold_reasons=readiness_service.reason_options(),
        owner_labels=readiness.OWNER_LABELS,
        exception_kinds=request_store.EXCEPTION_KINDS,
    )


READINESS_WINDOWS = {
    "7": "Due in the next 7 days",
    "14": "Due in the next 14 days",
    "30": "Due in the next 30 days",
    "overdue": "Overdue only",
    "all": "Everything in the window",
}
READINESS_STALE_DAYS = int(os.getenv("READINESS_STALE_DAYS", "60"))


def _readiness_filters() -> dict[str, Any]:
    operator = identity.current_operator()
    if "owner" in request.args:
        owner = (request.args.get("owner") or "").strip().lower()
    else:
        owner = operator.team if operator and operator.team in ("sales", "finance", "shipping") else ""
    window = (request.args.get("window") or "14").strip().lower()
    if window not in READINESS_WINDOWS:
        window = "14"
    today = _today_local()
    due_before = due_after = None
    if window == "overdue":
        due_before = (today - timedelta(days=1)).isoformat()
    elif window != "all":
        due_before = (today + timedelta(days=int(window))).isoformat()
        due_after = (today - timedelta(days=READINESS_STALE_DAYS)).isoformat()
    return {
        "owner": owner,
        "state": (request.args.get("state") or "").strip().upper(),
        "reason": (request.args.get("reason") or "").strip().lower(),
        "customer": (request.args.get("customer") or "").strip(),
        "q": (request.args.get("q") or "").strip(),
        "firearms": request.args.get("firearms") == "1",
        "blocking": request.args.get("blocking") == "1",
        # Stock holds are noise for Sales/Finance/Shipping; show them only on request,
        # when Production is the selected owner, or when a stock reason is filtered.
        "stock": (
            request.args.get("stock") == "1"
            or owner == readiness.OWNER_PRODUCTION
            or (request.args.get("reason") or "").strip().lower() in readiness.STOCK_REASONS
        ),
        # RMA / warranty orders never go through the picklist; hidden unless asked for.
        "rma": request.args.get("rma") == "1" or (request.args.get("reason") or "").strip().lower() == "rma_excluded",
        "window": window,
        "due_before": due_before,
        "due_after": due_after,
    }


def _readiness_sorted(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"BLOCKED": 0, "ATTENTION": 1, "READY": 2}
    return sorted(
        orders,
        key=lambda o: (rank.get(o.get("state"), 3), o.get("due") or "9999-12-31", o.get("order_id") or ""),
    )


@app.get("/orders")
@require_trusted_client
def orders_page():
    payload = _audit_json_safe(readiness_service.current_payload())
    filters = _readiness_filters()
    payload = readiness_service.orders_view(payload, hide_stock=not filters["stock"], hide_rma=not filters["rma"])
    orders = _readiness_sorted(
        readiness_service.filter_orders(
            payload.get("orders") or [],
            owner=filters["owner"] or None,
            state=filters["state"] or None,
            reason=filters["reason"] or None,
            customer=filters["customer"] or None,
            query=filters["q"] or None,
            firearms_only=filters["firearms"],
            due_before=filters["due_before"],
            due_after=filters["due_after"],
            blocking_only=filters["blocking"],
        )
    )
    return render_template(
        "orders.html",
        windows=READINESS_WINDOWS,
        payload=payload,
        orders=orders,
        filters=filters,
        hold_reasons=readiness_service.reason_options(),
        owner_labels=readiness.OWNER_LABELS,
        states=readiness.ORDER_STATES,
        operator=identity.current_operator(),
    )


@app.get("/orders/<order_id>")
@require_trusted_client
def order_detail_page(order_id: str):
    detail = _audit_json_safe(readiness_service.order_detail(order_id))
    status = 200 if detail.get("found") or detail.get("error") else 404
    so = order_id.strip().upper()
    return (
        render_template(
            "order_detail.html",
            order=detail,
            owner_labels=readiness.OWNER_LABELS,
            bin_labels=readiness.BIN_CLASS_LABELS,
            operator=identity.current_operator(),
            order_requests=request_store.list_requests(cust_order_id=so, limit=50),
            order_holds=request_store.manual_holds_for_order(so),
            order_documents=_order_documents_view(so) if detail.get("found") else [],
            ocr_enabled=get_ffl_doc_config()["enabled"],
            exception_kinds=request_store.EXCEPTION_KINDS,
        ),
        status,
    )


@app.get("/api/orders")
@require_trusted_client
def api_orders():
    payload = _audit_json_safe(readiness_service.current_payload())
    filters = _readiness_filters()
    orders = _readiness_sorted(
        readiness_service.filter_orders(
            payload.get("orders") or [],
            owner=filters["owner"] or None,
            state=filters["state"] or None,
            reason=filters["reason"] or None,
            customer=filters["customer"] or None,
            query=filters["q"] or None,
            firearms_only=filters["firearms"],
            due_before=filters["due_before"],
            due_after=filters["due_after"],
            blocking_only=filters["blocking"],
        )
    )
    return jsonify(
        {
            "evaluated_at": payload.get("evaluated_at"),
            "summary": payload.get("summary"),
            "error": payload.get("error"),
            "filters": filters,
            "orders": orders,
        }
    )


@app.get("/api/orders/<order_id>")
@require_trusted_client
def api_order_detail(order_id: str):
    detail = _audit_json_safe(readiness_service.order_detail(order_id))
    if not detail.get("found") and not detail.get("error"):
        return jsonify({"error": f"{order_id} was not found.", **detail}), 404
    return jsonify(detail)


@app.post("/api/orders/<order_id>/holds/<int:hold_id>/ack")
@require_trusted_client
@require_csrf
@identity.require_operator
def api_order_hold_ack(order_id: str, hold_id: int):
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or request.form.get("note") or "").strip() or None
    try:
        hold = readiness_service.acknowledge(
            hold_id, actor=g.operator.name, actor_team=g.operator.team, note=note
        )
    except LookupError:
        return jsonify({"ok": False, "message": "Hold not found."}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if str(hold.get("cust_order_id") or "").upper() != order_id.strip().upper():
        return jsonify({"ok": False, "message": "Hold does not belong to this order."}), 400
    return jsonify({"ok": True, "hold": _audit_json_safe(hold)})


@app.post("/api/readiness/refresh")
@require_trusted_client
@require_csrf
def api_readiness_refresh():
    payload = readiness_service.refresh("manual", force=True)
    return jsonify(
        {
            "ok": payload.get("error") is None,
            "error": payload.get("error"),
            "evaluated_at": payload.get("evaluated_at"),
            "summary": payload.get("summary"),
            "reconcile": payload.get("reconcile"),
        }
    ), (200 if payload.get("error") is None else 502)


def _shipment_lookup_params() -> dict[str, Any]:
    today = _today_local()
    end_raw = (request.args.get("end") or "").strip()
    start_raw = (request.args.get("start") or "").strip()
    try:
        end_day = date.fromisoformat(end_raw) if end_raw else today
    except ValueError:
        end_day = today
    try:
        start_day = date.fromisoformat(start_raw) if start_raw else end_day - timedelta(days=7)
    except ValueError:
        start_day = end_day - timedelta(days=7)
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days > SHIPMENTS_LOOKUP_MAX_DAYS:
        start_day = end_day - timedelta(days=SHIPMENTS_LOOKUP_MAX_DAYS)
    return {
        "so": (request.args.get("so") or "").strip().upper(),
        "customer": (request.args.get("customer") or "").strip(),
        "serial": (request.args.get("serial") or "").strip().upper(),
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "end_exclusive": (end_day + timedelta(days=1)).isoformat(),
    }


def _shipment_lookup(params: dict[str, Any]) -> dict[str, Any]:
    if params["so"] and not params.get("customer"):
        # A specific order: ignore the date window so old shipments are found too.
        rows = fetch_order_shipment_rows(params["so"]) if "-" in params["so"] else fetch_shipment_lookup_rows(
            "1900-01-01", params["end_exclusive"], "", params["so"]
        )
    else:
        rows = fetch_shipment_lookup_rows(params["start"], params["end_exclusive"], params["customer"], params["so"])
    packlists = shipments.group_packlists(rows)
    return {"packlists": packlists, "summary": shipments.summarize(packlists), "params": params, "error": None}


@app.get("/api/orders/<order_id>/shipments")
@require_trusted_client
def api_order_shipments(order_id: str):
    try:
        packlists = shipments.group_packlists(fetch_order_shipment_rows(order_id.strip().upper()))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Order shipments lookup failed for %s", order_id)
        return jsonify({"error": str(exc), "order_id": order_id}), 502
    return jsonify(_audit_json_safe({
        "order_id": order_id.strip().upper(),
        "packlists": packlists,
        "summary": shipments.summarize(packlists),
    }))


@app.get("/shipments")
@require_trusted_client
def shipments_page():
    params = _shipment_lookup_params()
    if params["serial"]:
        return redirect(url_for("serial_history_page", serial=params["serial"]))
    result: dict[str, Any] = {"packlists": [], "summary": shipments.summarize([]), "params": params, "error": None}
    searched = bool(params["so"] or params["customer"] or request.args.get("start") or request.args.get("end") or request.args.get("go"))
    if searched:
        try:
            result = _shipment_lookup(params)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Shipment lookup failed")
            result["error"] = str(exc)
    return render_template(
        "shipments.html",
        result=_audit_json_safe(result),
        params=params,
        searched=searched,
        operator=identity.current_operator(),
    )


@app.get("/api/shipments")
@require_trusted_client
def api_shipments():
    params = _shipment_lookup_params()
    try:
        result = _shipment_lookup(params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Shipment lookup failed")
        return jsonify({"error": str(exc), "params": params}), 502
    return jsonify(_audit_json_safe(result))


@app.get("/api/shipping/digest/preview")
@require_trusted_client
def api_shipping_digest_preview():
    day_raw = (request.args.get("date") or "").strip()
    try:
        day = date.fromisoformat(day_raw) if day_raw else _today_local()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        payload = build_shipped_digest_payload(day)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Digest preview failed")
        return jsonify({"error": str(exc)}), 502
    payload.pop("packlists", None)
    return jsonify(_audit_json_safe(payload))


@app.post("/api/shipping/digest/send")
@require_trusted_client
@require_csrf
def api_shipping_digest_send():
    day_raw = (request.args.get("date") or (request.get_json(silent=True) or {}).get("date") or "").strip()
    try:
        day = date.fromisoformat(day_raw) if day_raw else _today_local()
    except ValueError:
        return jsonify({"ok": False, "message": "date must be YYYY-MM-DD"}), 400
    try:
        result = send_shipped_digest(day, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Digest send failed")
        return jsonify({"ok": False, "message": str(exc)}), 502
    return jsonify({"ok": result["sent"], **result}), (200 if result["sent"] else 400)


@app.get("/stock")
@require_trusted_client
def stock_page():
    part = _clean_part_id(request.args.get("part")) or ""
    serial = (request.args.get("serial") or "").strip().upper()
    payload: Optional[dict[str, Any]] = None
    serial_payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    if part:
        try:
            payload = build_stock_lookup(part)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stock lookup failed for %s", part)
            error = str(exc)
    elif serial:
        try:
            serial_payload = stock.serial_locations_payload(serial, fetch_serial_onhand_locations([serial]))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Serial stock lookup failed for %s", serial)
            error = str(exc)
    return render_template(
        "stock.html",
        part=part,
        serial=serial,
        payload=_audit_json_safe(payload) if payload else None,
        serial_payload=_audit_json_safe(serial_payload) if serial_payload else None,
        error=error,
        operator=identity.current_operator(),
    )


@app.get("/api/stock")
@require_trusted_client
def api_stock():
    part = _clean_part_id(request.args.get("part")) or ""
    serial = (request.args.get("serial") or "").strip().upper()
    if not part and not serial:
        return jsonify({"error": "part or serial is required"}), 400
    try:
        if part:
            return jsonify(_audit_json_safe(build_stock_lookup(part)))
        return jsonify(_audit_json_safe(stock.serial_locations_payload(serial, fetch_serial_onhand_locations([serial]))))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stock lookup failed")
        return jsonify({"error": str(exc)}), 502


@app.get("/api/stock/parts")
@require_trusted_client
def api_stock_parts():
    term = (request.args.get("q") or "").strip().upper()
    if len(term) < ALLOC_PART_SEARCH_MIN_CHARS:
        return jsonify({"parts": []}), 200
    try:
        return jsonify({"parts": _search_parts(term)}), 200
    except Exception:  # noqa: BLE001
        logger.exception("Part search failed for %s", term)
        return jsonify({"error": "lookup_failed", "parts": []}), 500


def _request_options() -> dict[str, Any]:
    return {
        "request_types": request_store.REQUEST_TYPES,
        "exception_kinds": request_store.EXCEPTION_KINDS,
        "problem_kinds": request_store.PROBLEM_KINDS,
        "service_levels": request_store.SERVICE_LEVELS,
        "statuses": request_store.STATUSES,
        "priorities": request_store.PRIORITIES,
        "team_labels": request_service.TEAM_LABELS,
        "transitions": {k: sorted(v) for k, v in request_store.TRANSITIONS.items()},
    }


def _request_list_for_args(operator) -> tuple[list[dict], dict[str, Any]]:
    scope = (request.args.get("scope") or ("mine" if operator else "all")).strip().lower()
    status = (request.args.get("status") or "open").strip().lower()
    rtype = (request.args.get("type") or "").strip()
    so = (request.args.get("so") or "").strip().upper()
    kwargs: dict[str, Any] = {"limit": 300}
    if rtype in request_store.REQUEST_TYPES:
        kwargs["request_type"] = rtype
    if so:
        kwargs["cust_order_id"] = so
    if status == "open":
        kwargs["open_only"] = True
    elif status == "overdue":
        kwargs["overdue_only"] = True
    elif status in request_store.STATUSES:
        kwargs["status"] = status
    if scope == "team" and operator and operator.team:
        rows = request_store.list_requests(owner_team=operator.team, **kwargs)
    elif scope == "mine" and operator:
        seen: dict[int, dict] = {}
        for row in request_store.list_requests(created_by=operator.name, **kwargs):
            seen[row["id"]] = row
        for row in request_store.list_requests(assigned_to=operator.name, **kwargs):
            seen.setdefault(row["id"], row)
        rows = sorted(seen.values(), key=lambda r: (not r["is_open"], r.get("sla_due_at") or r["created_at"]))
    else:
        scope = "all"
        rows = request_store.list_requests(**kwargs)
    return rows, {"scope": scope, "status": status, "type": rtype, "so": so}


@app.get("/requests")
@require_trusted_client
def requests_page():
    operator = identity.current_operator()
    rows, filters = _request_list_for_args(operator)
    prefill = {
        "type": (request.args.get("new") or request.args.get("type") or "").strip(),
        "so": (request.args.get("so") or "").strip().upper(),
        "wo": (request.args.get("wo") or "").strip().upper(),
        "part": (request.args.get("part") or "").strip().upper(),
        "serial": (request.args.get("serial") or "").strip().upper(),
    }
    return render_template(
        "requests.html",
        rows=_audit_json_safe(rows),
        filters=filters,
        summary=request_store.queue_summary(),
        options=_request_options(),
        prefill=prefill,
        operator=operator,
        open_form=bool(request.args.get("new")),
        manual_holds=_active_manual_holds(),
    )


@app.post("/requests")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_create():
    form = request.form
    request_type = (form.get("request_type") or "").strip()
    fields = {key: form.get(key) for key in (
        "needed_by", "expedite", "service_level", "ship_complete", "exception_kind", "expires_at",
        "expected_location", "actual_location", "qty", "problem_kind",
    ) if form.get(key) is not None}
    try:
        req = request_service.create(
            request_type=request_type,
            actor=g.operator.name,
            actor_team=g.operator.team,
            title=form.get("title"),
            body=form.get("body") or "",
            cust_order_id=form.get("cust_order_id"),
            work_order_id=form.get("work_order_id"),
            customer_id=form.get("customer_id"),
            part_id=form.get("part_id"),
            serial_no=form.get("serial_no"),
            fields=fields,
            priority=form.get("priority") or "normal",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("requests_page", new=request_type or None, so=form.get("cust_order_id") or None))
    flash(f"Request #{req['id']} sent to {request_service.TEAM_LABELS.get(req['owner_team'], req['owner_team'])}.", "success")
    return redirect(url_for("request_detail_page", request_id=req["id"]))


@app.get("/requests/<int:request_id>")
@require_trusted_client
def request_detail_page(request_id: int):
    req = request_store.get_request(request_id)
    if req is None:
        abort(404)
    hold = request_store.get_manual_hold(req["linked_manual_hold_id"]) if req.get("linked_manual_hold_id") else None
    blockers = _readiness_blocking_holds(req["cust_order_id"]) if req.get("cust_order_id") else []
    return render_template(
        "request_detail.html",
        req=_audit_json_safe(req),
        hold=hold,
        blockers=[b for b in blockers if b.get("reason_code") != "manual_hold"],
        legal=sorted(request_store.TRANSITIONS.get(req["status"], set())),
        options=_request_options(),
        operator=identity.current_operator(),
    )


@app.post("/requests/<int:request_id>/transition")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_transition(request_id: int):
    to_status = (request.form.get("to_status") or "").strip().lower()
    try:
        request_service.transition(
            request_id,
            to_status,
            actor=g.operator.name,
            actor_team=g.operator.team,
            note=request.form.get("note"),
            resolution=request.form.get("resolution"),
            accept_expedite=bool(request.form.get("accept_expedite")),
        )
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Request #{request_id} is now {to_status.replace('_', ' ')}.", "success")
    return redirect(url_for("request_detail_page", request_id=request_id))


@app.post("/requests/<int:request_id>/assign")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_assign(request_id: int):
    try:
        request_service.assign(request_id, request.form.get("assignee") or "", actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("request_detail_page", request_id=request_id))


@app.post("/requests/<int:request_id>/comment")
@require_trusted_client
@require_csrf
@identity.require_operator
def requests_comment(request_id: int):
    try:
        request_service.comment(request_id, request.form.get("note") or "", actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("request_detail_page", request_id=request_id))


@app.get("/api/requests")
@require_trusted_client
def api_requests():
    rows, filters = _request_list_for_args(identity.current_operator())
    return jsonify(_audit_json_safe({"requests": rows, "filters": filters, "summary": request_store.queue_summary()}))


@app.get("/api/requests/<int:request_id>")
@require_trusted_client
def api_request_detail(request_id: int):
    req = request_store.get_request(request_id)
    if req is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_audit_json_safe(req))


@app.post("/api/holds/<int:hold_id>/release")
@require_trusted_client
@require_csrf
@identity.require_operator
def api_hold_release(hold_id: int):
    if g.operator.team not in ("shipping", "management"):
        return jsonify({"ok": False, "message": "Only Shipping or Management can release a hold."}), 403
    try:
        released = request_service.release_hold(hold_id, actor=g.operator.name, actor_team=g.operator.team)
    except LookupError:
        return jsonify({"ok": False, "message": "Hold not found."}), 404
    return jsonify({"ok": True, "released": released})


@app.get("/api/shipping/metrics")
@require_trusted_client
def api_shipping_metrics():
    days = parse_scorecard_days(request.args.get("days"))
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_shipping_scorecard_payload(days, force=force))), 200


@app.get("/api/shipping/release-gate")
@require_trusted_client
def api_shipping_release_gate():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_release_gate_payload(force=force))), 200


@app.post("/shipping/release-exceptions")
@require_trusted_client
@require_csrf
def shipping_release_exception_add():
    order_id = (request.form.get("cust_order_id") or "").strip().upper()
    reason = (request.form.get("reason") or "").strip()
    operator = (request.form.get("operator") or "").strip()
    try:
        hours = int(request.form.get("expires_hours") or "24")
        if hours < 1 or hours > 168:
            raise ValueError
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        shipping_store.add_exception(
            cust_order_id=order_id,
            reason=reason,
            created_by=operator,
            expires_at=expires.isoformat(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )
        flash(f"Release exception added for {order_id}.", "success")
    return redirect(url_for("shipping_page", view="scorecard"))


@app.post("/shipping/release-exceptions/<int:exception_id>/revoke")
@require_trusted_client
@require_csrf
def shipping_release_exception_revoke(exception_id: int):
    operator = (request.form.get("operator") or "").strip()
    try:
        revoked = shipping_store.revoke_exception(exception_id, operator)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        _release_gate_cache.update(
            {"payload": None, "fetched_at": None, "signature": None}
        )
        flash(
            "Release exception revoked." if revoked else "Release exception was already inactive.",
            "success" if revoked else "error",
        )
    return redirect(url_for("shipping_page", view="scorecard"))


@app.get("/api/shipping/shortages")
@require_trusted_client
def api_shipping_shortages():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_shortage_payload(force=force))), 200


@app.get("/api/shipping/excess-packlists")
@require_trusted_client
def api_shipping_excess_packlists():
    force = request.args.get("refresh") == "1"
    return jsonify(_audit_json_safe(build_excess_packlist_payload(force=force))), 200


@app.get("/shipping/shortages/export")
@require_trusted_client
def shipping_shortages_export():
    payload = build_shortage_payload()
    if payload.get("error"):
        flash(f"Shortage data is unavailable: {payload['error']}", "error")
        return redirect(url_for("shipping_page", view="shortages"))

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
    operator = (request.form.get("operator") or "").strip() or None
    selected_orders = request.form.getlist("orders")
    queue = build_pick_order_queue()
    try:
        session_id = pick_store.start_order_session(
            plan_rows=queue["plan_rows"],
            selected_orders=selected_orders,
            source_runs=queue["source_runs"],
            operator=operator,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("shipping_page", view="pick"))
    logger.info(
        "Started order pick session #%s for %s.",
        session_id,
        ", ".join(selected_orders),
    )
    return redirect(url_for("pick_session_page", session_id=session_id))


def _legacy_pick_session_start():
    query_type = get_query_type(request.form.get("query_type"))
    operator = (request.form.get("operator") or "").strip() or None

    run, rows = get_latest_successful_run(query_type=query_type)
    if not run:
        flash(
            f"No successful {query_type} picklist run to pick against. Run the picklist first.",
            "error",
        )
        return redirect(url_for("shipping_page", view="pick"))
    if not rows:
        flash(
            f"The latest {query_type} run has no rows — nothing to pick.",
            "error",
        )
        return redirect(url_for("shipping_page", view="pick"))

    active_session = next(
        (
            session
            for session in pick_store.recent_sessions(limit=100)
            if session.get("status") == "active"
            and session.get("query_type") == query_type
            and int(session.get("run_id") or 0) == int(run["id"])
        ),
        None,
    )
    if active_session:
        flash(
            f"Resuming active {query_type} pick session #{active_session['id']}.",
            "success",
        )
        return redirect(url_for("pick_session_page", session_id=active_session["id"]))

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
        return redirect(url_for("shipping_page", view="pick"))

    lines = pick_store.get_lines(session_id)
    orders = pick_store.get_orders(session_id)
    order_context: dict[str, dict[str, Any]] = {}
    for order in orders:
        order_id = str(order.get("cust_order_id") or "").strip().upper()
        order_context[order_id] = {
            "tote": order.get("tote_barcode") or order.get("tote_code"),
            "locations": sorted(
                {
                    str(line.get("location") or "").strip().upper()
                    for line in lines
                    if str(line.get("cust_order_id") or "").strip().upper() == order_id
                    and str(line.get("location") or "").strip()
                }
            ),
            "status": order.get("status"),
            "assigned_operator": order.get("assigned_operator"),
        }
    scans = pick_store.get_scans(session_id, limit=100)
    order_events = pick_store.get_order_events(session_id, limit=200)
    for scan in scans:
        scan["scanned_display"] = _audit_dt_display(scan.get("scanned_at"))
    for event in order_events:
        event["created_display"] = _audit_dt_display(event.get("created_at"))

    return render_template(
        "pick_session.html",
        session=session_row,
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        lines=lines,
        orders=orders,
        order_context=order_context,
        scans=scans,
        order_events=order_events,
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
    upc_parts = {
        str(line.get("upc") or "").strip().upper(): str(line["part_id"] or "").strip()
        for line in lines
        if str(line.get("upc") or "").strip()
    }
    if scan in upc_parts:
        return None, [{"part_id": upc_parts[scan], "locations": []}], False

    try:
        df = run_erp_query_file(
            PICK_SERIAL_LOOKUP_FILE, {"serial": scan}, "pick serial lookup"
        )
    except Exception:  # noqa: BLE001
        logger.exception("Pick serial lookup failed for %s", scan)
        return scan, [], True
    if df.empty and any(line.get("item_type") == "components" for line in lines):
        try:
            upc_df = run_erp_query_file(
                PICK_UPC_LOOKUP_FILE, {"upc": scan}, "pick UPC lookup"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Pick UPC lookup failed for %s", scan)
            return None, [], True
        if not upc_df.empty:
            upc_candidates = [
                {"part_id": str(row.get("PART_ID") or "").strip(), "locations": []}
                for row in upc_df.to_dict(orient="records")
                if str(row.get("PART_ID") or "").strip()
            ]
            return None, upc_candidates, False
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
    operator = (payload.get("operator") or session_row.get("operator") or "").strip() or None
    request_id = (payload.get("request_id") or "").strip() or None
    scanned_tote = (payload.get("tote") or "").strip().upper() or None
    scanned_location = (payload.get("location") or "").strip().upper() or None
    if not scan:
        return jsonify({"error": "invalid", "message": "A scanned value is required."}), 400
    if len(scan) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Scanned value is too long."}), 400
    if not request_id or len(request_id) > 100:
        return jsonify({"error": "invalid", "message": "A valid scan request ID is required."}), 400

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
            request_id=request_id,
            scanned_tote=scanned_tote,
            scanned_location=scanned_location,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record pick scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    result["scan"] = scan
    return jsonify(result), 200


@app.post("/api/pick/session/<int:session_id>/order/<path:order_id>/action")
@require_trusted_client
@require_csrf
def api_pick_order_action(session_id: int, order_id: str):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404
    payload = request.get_json(silent=True) or {}
    try:
        result = pick_store.order_action(
            session_id,
            order_id,
            str(payload.get("action") or ""),
            operator=str(payload.get("operator") or session_row.get("operator") or ""),
            reason=payload.get("reason"),
            to_operator=payload.get("to_operator"),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_action", "message": str(exc)}), 409
    result["redirect"] = url_for("pick_session_page", session_id=session_id)
    return jsonify(result), 200


@app.post("/api/pick/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_pick_complete(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Pick session not found."}), 404

    payload = request.get_json(silent=True) or {}
    order_id = (payload.get("order") or "").strip().upper()
    try:
        if order_id:
            completed = pick_store.complete_order(session_id, order_id)
            counts = pick_store.compute_counts(session_id)
            logger.info("Order %s is ready for packing from pick session #%s.", order_id, session_id)
        else:
            completed = pick_store.complete_session(session_id)
            counts = completed.get("counts")
    except ValueError as exc:
        return jsonify({"error": "incomplete", "message": str(exc)}), 409
    return jsonify(
        {
            "status": completed.get("status", "completed"),
            "counts": counts,
            "redirect": url_for("pick_session_page", session_id=session_id),
        }
    ), 200


def _abandon_pick_session(session_id: int, operator: str, reason: str) -> dict:
    session_row = pick_store.get_session(session_id)
    if not session_row:
        raise LookupError("Pick session not found.")
    current = identity.current_operator()
    actor = (operator or "").strip() or (current.name if current else "") or (session_row.get("operator") or "")
    result = pick_store.abandon_session(session_id, operator=actor, reason=reason)
    logger.info(
        "Pick session #%s closed short by %s (%s): %d order(s) released, %d picked unit(s) to put back.",
        session_id, actor, reason, len(result["orders"]), result["picked_units"],
    )
    return result


@app.post("/api/pick/session/<int:session_id>/abandon")
@require_trusted_client
@require_csrf
def api_pick_abandon(session_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        result = _abandon_pick_session(
            session_id, str(payload.get("operator") or ""), str(payload.get("reason") or "")
        )
    except LookupError as exc:
        return jsonify({"error": "not_found", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": "invalid_action", "message": str(exc)}), 409
    result["redirect"] = url_for("pick_session_page", session_id=session_id)
    return jsonify(result), 200


@app.post("/pick/session/<int:session_id>/abandon")
@require_trusted_client
@require_csrf
def pick_session_abandon(session_id: int):
    try:
        result = _abandon_pick_session(
            session_id, request.form.get("operator") or "", request.form.get("reason") or ""
        )
    except LookupError as exc:
        flash(str(exc), "error")
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        note = f" {result['picked_units']} picked unit(s) need to go back on the shelf." if result["picked_units"] else ""
        flash(f"Pick session #{session_id} closed; {len(result['orders'])} order(s) released.{note}", "success")
    return redirect(url_for("shipping_page", view="pick"))


@app.get("/pick/session/<int:session_id>/export")
@require_trusted_client
def pick_session_export(session_id: int):
    session_row = pick_store.get_session(session_id)
    if not session_row:
        flash(f"Pick session #{session_id} was not found.", "error")
        return redirect(url_for("shipping_page", view="pick"))

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


# ---------------------------------------------------------------------------
# Packlist verification
# ---------------------------------------------------------------------------
def _normalize_packlist_input(raw: str) -> str:
    """Uppercase/trim; a bare number is assumed to be a PL- packlist."""
    value = (raw or "").strip().upper()
    if value.isdigit():
        return f"PL-{value}"
    return value


def _lookup_serial_shipment(scan: str, current_packlist: str) -> tuple[Optional[dict], bool]:
    """(live shipment row on a different packlist, erp_checked) for a scan
    that matched nothing in the session snapshot."""
    try:
        df = run_erp_query_file(
            SERIAL_HISTORY_SHIPMENTS_FILE, {"serial": scan}, "verify serial lookup"
        )
    except Exception:  # noqa: BLE001 — enrichment only; the scan still records
        logger.exception("Verify serial reverse lookup failed for %s", scan)
        return None, False
    other = None
    for row in df.to_dict(orient="records"):
        status = str(row.get("SHIPPER_STATUS") or "").strip().upper()
        packlist = str(row.get("PACKLIST_ID") or "").strip().upper()
        if status in ("X", "V") or not packlist or packlist == current_packlist:
            continue
        other = row  # keep the last (most recent SHIPPED_DATE) live shipment
    return other, True


@app.post("/verify/session/start")
@require_trusted_client
@require_csrf
def verify_session_start():
    packlist_id = _normalize_packlist_input(request.form.get("packlist_id") or "")
    operator = (request.form.get("operator") or "").strip() or None
    if not packlist_id:
        flash("Scan or type a packlist number to verify.", "error")
        return redirect(url_for("shipping_page", view="verify"))

    try:
        df = run_erp_query_file(
            PACKLIST_SERIALS_FILE, {"packlist": packlist_id}, "packlist serials"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Packlist serials query failed for %s", packlist_id)
        flash(f"Could not load {packlist_id} from the ERP: {exc}", "error")
        return redirect(url_for("shipping_page", view="verify"))

    rows = df.to_dict(orient="records")
    if not rows:
        flash(f"{packlist_id} was not found in the ERP.", "error")
        return redirect(url_for("shipping_page", view="verify"))

    header = rows[0]
    shipper_status = str(header.get("SHIPPER_STATUS") or "").strip().upper()
    if shipper_status in ("X", "V"):
        flash(f"{packlist_id} is voided in the ERP — nothing to verify.", "error")
        return redirect(url_for("shipping_page", view="verify"))
    pick_attached = pick_store.attach_packlist(
        header.get("CUST_ORDER_ID"), packlist_id
    )
    if not any(str(r.get("TRACE_ID") or "").strip() for r in rows):
        if pick_attached:
            flash(
                f"{packlist_id} was attached to the picked order; it has no serialized items to scan-verify.",
                "success",
            )
            return redirect(url_for("shipping_page", view="verify"))
        flash(
            f"{packlist_id} has no serialized items — nothing to scan-verify.",
            "error",
        )
        return redirect(url_for("shipping_page", view="verify"))

    session_id = verify_store.start_session(packlist_id, header, rows, operator=operator)
    if pick_attached:
        logger.info(
            "Attached %s to ready picked order %s.",
            packlist_id,
            header.get("CUST_ORDER_ID"),
        )
    logger.info(
        "Started verify session #%s for %s (%d rows).",
        session_id,
        packlist_id,
        len(rows),
    )
    return redirect(url_for("verify_session_page", session_id=session_id))


@app.get("/verify/session/<int:session_id>")
@require_trusted_client
def verify_session_page(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        flash(f"Verification session #{session_id} was not found.", "error")
        return redirect(url_for("shipping_page", view="verify"))

    expected = verify_store.get_expected(session_id)
    scans = verify_store.get_scans(session_id, limit=100)
    for scan in scans:
        scan["scanned_display"] = _audit_dt_display(scan.get("scanned_at"))

    return render_template(
        "verify_session.html",
        session=session_row,
        started_display=_audit_dt_display(session_row.get("started_at")),
        completed_display=_audit_dt_display(session_row.get("completed_at")),
        expected=[row for row in expected if row["status"] != "info"],
        info_rows=[row for row in expected if row["status"] == "info"],
        scans=scans,
        counts=verify_store.compute_counts(session_id),
    )


@app.post("/api/verify/session/<int:session_id>/scan")
@require_trusted_client
@require_csrf
def api_verify_scan(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Verification session not found."}), 404
    if session_row.get("status") == "completed":
        return jsonify(
            {"error": "completed", "message": "This verification session is already completed."}
        ), 409

    payload = request.get_json(silent=True) or {}
    scan = (payload.get("scan") or "").strip().upper()
    operator = (payload.get("operator") or "").strip() or None
    if not scan:
        return jsonify({"error": "invalid", "message": "A scanned value is required."}), 400
    if len(scan) > SERIAL_MAX_LENGTH:
        return jsonify({"error": "invalid", "message": "Scanned value is too long."}), 400

    # Only a scan that matches nothing in the snapshot needs the ERP, and its
    # failure never blocks the scan from recording (unlike pick confirm).
    other_row = None
    erp_checked = True
    if not any(
        row["status"] != "info" and scan in (row.get("serial"), row.get("serial_alt"))
        for row in verify_store.get_expected(session_id)
    ):
        other_row, erp_checked = _lookup_serial_shipment(scan, session_row["packlist_id"])

    try:
        result = verify_store.record_scan(
            session_id,
            scan,
            other_packlist_id=(other_row or {}).get("PACKLIST_ID"),
            other_customer=(other_row or {}).get("CUSTOMER_NAME")
            or (other_row or {}).get("CUSTOMER_ID"),
            erp_checked=erp_checked,
            operator=operator,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record verify scan: %s", exc)
        return jsonify({"error": "scan_failed", "message": str(exc)}), 500

    result["scan"] = scan
    return jsonify(result), 200


@app.post("/api/verify/session/<int:session_id>/complete")
@require_trusted_client
@require_csrf
def api_verify_complete(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        return jsonify({"error": "not_found", "message": "Verification session not found."}), 404

    completed = verify_store.complete_session(session_id)
    logger.info(
        "Completed verify session #%s for %s (%s).",
        session_id,
        session_row.get("packlist_id"),
        completed.get("outcome"),
    )
    return jsonify(
        {
            "status": "completed",
            "outcome": completed.get("outcome"),
            "counts": completed.get("counts"),
            "redirect": url_for("verify_session_page", session_id=session_id),
        }
    ), 200


@app.get("/api/verify/daily")
@require_trusted_client
def api_verify_daily():
    force = request.args.get("refresh") == "1"
    payload = build_verify_daily_payload(request.args.get("date"), force=force)
    return jsonify(_audit_json_safe(payload)), 200


@app.get("/verify/session/<int:session_id>/export")
@require_trusted_client
def verify_session_export(session_id: int):
    session_row = verify_store.get_session(session_id)
    if not session_row:
        flash(f"Verification session #{session_id} was not found.", "error")
        return redirect(url_for("shipping_page", view="verify"))

    expected_df = pd.DataFrame(verify_store.get_expected(session_id))
    scans_df = pd.DataFrame(verify_store.get_scans(session_id, limit=10000))
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        (expected_df if not expected_df.empty else pd.DataFrame(columns=["serial"])).to_excel(
            writer, index=False, sheet_name="Expected"
        )
        (scans_df if not scans_df.empty else pd.DataFrame(columns=["scan_value"])).to_excel(
            writer, index=False, sheet_name="Scans"
        )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"verify_session{session_id}_{session_row['packlist_id']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def parse_recipient_addresses(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def recipients_are_valid(recipients: list[str]) -> bool:
    return all(email_address_is_valid(address) for address in recipients)


@app.get("/api/me")
@require_trusted_client
def api_me():
    operator = identity.current_operator()
    return jsonify(
        {
            "operator": operator.as_dict() if operator else None,
            "roster_size": len(get_operator_roster()),
            "teams": [{"key": team, "label": identity.TEAM_LABELS[team]} for team in identity.TEAMS],
        }
    )


@app.post("/api/settings/test-teams")
@require_trusted_client
@require_csrf
def api_test_teams_settings():
    if not settings_access_granted():
        return jsonify({"ok": False, "message": "Unlock settings before testing."}), 403
    payload = request.get_json(silent=True) or {}
    webhook = (payload.get("webhook_url") or "").strip()
    ok, message = notifier.send_test(webhook)
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400


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
