import atexit
import io
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from sqlalchemy import create_engine

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "picklist_history.db"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"
QUERY_FILES = {
    "guns": Path(os.getenv("GUNS_QUERY_FILE", BASE_DIR / "query_guns.sql")),
    "components": Path(os.getenv("COMPONENTS_QUERY_FILE", BASE_DIR / "query_components.sql")),
}
DEFAULT_QUERY_TYPE = os.getenv("DEFAULT_QUERY_TYPE", "guns").lower()
if DEFAULT_QUERY_TYPE not in QUERY_FILES:
    DEFAULT_QUERY_TYPE = "guns"
MAX_RUNS = int(os.getenv("MAX_RUNS_TO_KEEP", "10"))
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "05:00")
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("picklist-app")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
scheduler = BackgroundScheduler(timezone=os.getenv("SCHEDULE_TIMEZONE", "UTC"))


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


def get_query_type(value: Optional[str]) -> str:
    if value in QUERY_FILES:
        return value
    return DEFAULT_QUERY_TYPE


def load_query(query_type: str) -> str:
    query_file = QUERY_FILES[query_type]
    if not query_file.exists():
        raise FileNotFoundError(f"Query file not found at: {query_file}")
    return query_file.read_text(encoding="utf-8")


def fetch_picklist_from_mssql(query_type: str) -> pd.DataFrame:
    query = load_query(query_type)
    mssql_conn_string = os.getenv("MSSQL_CONNECTION_STRING")
    if not mssql_conn_string:
        raise ValueError("MSSQL_CONNECTION_STRING is not set. Add it to your .env file.")

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


def send_telegram_notification(message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

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


def send_email_notification(subject: str, body: str, attachment: Optional[Path] = None) -> None:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_sender = os.getenv("SMTP_SENDER")
    smtp_recipient = os.getenv("SMTP_RECIPIENT")
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    recipients = [item.strip() for item in smtp_recipient.split(",")] if smtp_recipient else []
    recipients = [item for item in recipients if item]

    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, recipients]):
        logger.info("SMTP credentials incomplete; skipping email notification.")
        return

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


def get_recent_runs(query_type: str, limit: int = 10) -> list[sqlite3.Row]:
    with get_sqlite_conn() as conn:
        return conn.execute(
            """
            SELECT id, run_timestamp, status, row_count, query_type, error_message
            FROM runs
            WHERE query_type = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (query_type, limit),
        ).fetchall()


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
        send_telegram_notification(message)
        send_email_notification(
            subject=f"Picklist run {run_id} succeeded",
            body=message,
            attachment=export_path,
        )
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
        send_telegram_notification(message)
        send_email_notification(
            subject=f"Picklist run {run_id} failed",
            body=(
                f"{message}\n\n"
                "Please review logs/app.log for the full traceback and failure context."
            ),
        )
        return None


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


def start_scheduler() -> None:
    if not ENABLE_SCHEDULER:
        logger.info("Daily scheduler disabled via ENABLE_SCHEDULER=false.")
        return

    try:
        hour, minute = parse_schedule_time(SCHEDULE_TIME)
    except ValueError as exc:
        logger.error("Scheduler configuration error: %s", exc)
        logger.warning("Scheduler startup skipped due to invalid SCHEDULE_TIME value.")
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
    atexit.register(lambda: scheduler.shutdown(wait=False))


def get_next_scheduled_run() -> Optional[str]:
    job = scheduler.get_job("daily_picklist_run")
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.isoformat()


initialize_db()
start_scheduler()


@app.route("/")
def index():
    query_type = get_query_type(request.args.get("query_type"))
    latest_run, rows = get_latest_run(query_type=query_type)
    using_dummy_data = False
    if not rows:
        rows = get_dummy_picklist_rows()
        using_dummy_data = True

    recent_runs = get_recent_runs(query_type=query_type)
    columns = list(rows[0].keys()) if rows else []
    next_run = get_next_scheduled_run()
    return render_template(
        "index.html",
        latest_run=latest_run,
        rows=rows,
        columns=columns,
        next_run=next_run,
        recent_runs=recent_runs,
        using_dummy_data=using_dummy_data,
        active_query_type=query_type,
        query_options=QUERY_FILES.keys(),
    )


@app.post("/run")
def run_picklist():
    query_type = get_query_type(request.form.get("query_type"))
    export_path = execute_picklist_run(query_type=query_type)
    if export_path:
        flash(
            f"Picklist run complete for {query_type}. Export created at {export_path.name}",
            "success",
        )
    else:
        flash("Picklist run failed. Check logs for details.", "error")
    return redirect(url_for("index", query_type=query_type))


@app.get("/export")
def export_latest():
    query_type = get_query_type(request.args.get("query_type"))
    latest_run, rows = get_latest_run(query_type=query_type)
    if not latest_run or not rows:
        flash("No picklist data available to export yet.", "error")
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
