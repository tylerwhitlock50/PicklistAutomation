"""Environment-derived settings, paths and logging setup."""
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


# Project root (the folder that holds sql/, templates/, static/, data/).
BASE_DIR = Path(__file__).resolve().parents[1]


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
READINESS_SQL_DIR = BASE_DIR / "sql"


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


SCHEDULER_LOCK_PATH = DB_PATH.parent / ".scheduler.lock"


ENCRYPTED_SETTING_PREFIX = "enc:v1:"


SENSITIVE_SETTING_KEYS = {
    "mssql_connection_string",
    "mssql_write_connection_string",
    "telegram_bot_token",
    "smtp_password",
    "teams_webhook_url",
}


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
