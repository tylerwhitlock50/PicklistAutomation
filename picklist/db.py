"""SQLite connection, schema bootstrap and the encrypted settings table."""
import os
import sqlite3
from datetime import datetime
from typing import Optional

from picklist.config import (
    DB_PATH,
    ENCRYPTED_SETTING_PREFIX,
    logger,
    SENSITIVE_SETTING_KEYS,
    SETTINGS_ENCRYPTION_KEY,
)


try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:
    Fernet = None  # type: ignore[assignment]

    class InvalidToken(Exception):
        pass


SETTINGS_CIPHER: Optional[object] = None


SETTINGS_CIPHER_INITIALIZED = False


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


def get_config_source(setting_key: str, env_key: str) -> str:
    setting_value = get_setting(setting_key)
    if setting_value not in {None, ""}:
        return "database"
    env_value = os.getenv(env_key)
    if env_value not in {None, ""}:
        return "environment"
    return "unset"


def _int_setting(setting_key: str, env_key: str, default: int) -> int:
    raw = get_config_value(setting_key, env_key, str(default))
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default
