import atexit
import io
import json
import logging
import os
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, send_file, url_for
from sqlalchemy import create_engine

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "picklist_history.db"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"
QUERY_FILE = Path(os.getenv("QUERY_FILE", BASE_DIR / "query.sql"))
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
                error_message TEXT
            )
            """
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


def load_query() -> str:
    if not QUERY_FILE.exists():
        raise FileNotFoundError(f"Query file not found at: {QUERY_FILE}")
    return QUERY_FILE.read_text(encoding="utf-8")


def fetch_picklist_from_mssql() -> pd.DataFrame:
    query = load_query()
    mssql_conn_string = os.getenv("MSSQL_CONNECTION_STRING")
    if not mssql_conn_string:
        raise ValueError("MSSQL_CONNECTION_STRING is not set. Add it to your .env file.")

    engine = create_engine(mssql_conn_string)
    with engine.connect() as connection:
        return pd.read_sql_query(query, connection)


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


def save_run(df: pd.DataFrame, status: str, error_message: Optional[str] = None) -> int:
    run_timestamp = datetime.utcnow().isoformat()
    with get_sqlite_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (run_timestamp, status, row_count, error_message)
            VALUES (?, ?, ?, ?)
            """,
            (run_timestamp, status, len(df.index), error_message),
        )
        run_id = cursor.lastrowid

        if not df.empty:
            rows = [(run_id, json.dumps(row, default=str)) for row in df.to_dict(orient="records")]
            conn.executemany("INSERT INTO run_rows (run_id, row_json) VALUES (?, ?)", rows)

        prune_old_runs(conn)
        return run_id


def generate_export(df: pd.DataFrame, run_id: int) -> Path:
    export_path = EXPORT_DIR / f"picklist_run_{run_id}.xlsx"
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

    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, smtp_recipient]):
        logger.info("SMTP credentials incomplete; skipping email notification.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = smtp_recipient
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
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send SMTP email: %s", exc)


def get_latest_run():
    with get_sqlite_conn() as conn:
        run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            return None, []

        rows = conn.execute(
            "SELECT row_json FROM run_rows WHERE run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()
        parsed_rows = [json.loads(row["row_json"]) for row in rows]

        return run, parsed_rows


def execute_picklist_run() -> Optional[Path]:
    try:
        df = fetch_picklist_from_mssql()
        run_id = save_run(df=df, status="success")
        export_path = generate_export(df=df, run_id=run_id)

        message = f"✅ Picklist run {run_id} succeeded with {len(df.index)} rows."
        logger.info(message)
        send_telegram_notification(message)
        send_email_notification(
            subject=f"Picklist run {run_id} succeeded",
            body=message,
            attachment=export_path,
        )
        return export_path
    except Exception as exc:  # noqa: BLE001
        logger.exception("Picklist run failed: %s", exc)
        run_id = save_run(pd.DataFrame(), status="failed", error_message=str(exc))
        message = f"❌ Picklist run {run_id} failed: {exc}"
        send_telegram_notification(message)
        send_email_notification(
            subject=f"Picklist run {run_id} failed",
            body=message,
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

    hour, minute = parse_schedule_time(SCHEDULE_TIME)
    scheduler.add_job(
        execute_picklist_run,
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
    latest_run, rows = get_latest_run()
    columns = list(rows[0].keys()) if rows else []
    next_run = get_next_scheduled_run()
    return render_template(
        "index.html", latest_run=latest_run, rows=rows, columns=columns, next_run=next_run
    )


@app.post("/run")
def run_picklist():
    export_path = execute_picklist_run()
    if export_path:
        flash(f"Picklist run complete. Export created at {export_path.name}", "success")
    else:
        flash("Picklist run failed. Check logs for details.", "error")
    return redirect(url_for("index"))


@app.get("/export")
def export_latest():
    latest_run, rows = get_latest_run()
    if not latest_run or not rows:
        flash("No picklist data available to export yet.", "error")
        return redirect(url_for("index"))

    df = pd.DataFrame(rows)
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    filename = f"picklist_latest_{latest_run['id']}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
