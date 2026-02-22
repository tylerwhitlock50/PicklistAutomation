# Picklist Automation (Flask)

Simple Python app that:
- Reads a SQL query from a file (`query.sql` by default)
- Runs it against Microsoft SQL Server
- Stores each run in SQLite (keeps last 10 runs)
- Shows latest picklist in a web UI
- Exports latest picklist to Excel
- Sends Telegram + SMTP notifications for success/failure
- Uses `python-dotenv` for secrets/config
- Runs automatically on a daily scheduler (default 05:00 UTC)

## Setup

1. Create virtual environment and install requirements:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy env file and configure:
   ```bash
   cp .env.example .env
   ```
3. Update `query.sql` with your actual picklist query.
4. Start app:
   ```bash
   python app.py
   ```
5. Open: `http://localhost:5000`

## Environment Variables

See `.env.example` for all settings.

## Notes

- `MSSQL_CONNECTION_STRING` must be a valid SQLAlchemy URL for SQL Server + `pyodbc` (the password is masked in app logs).
- Run history is stored in `picklist_history.db`.
- Logs are written to `logs/app.log` with run duration and failure tracebacks for diagnostics.
- Excel exports are written to `exports/` and also available via browser download.


## Scheduler

- By default, the app schedules a daily picklist run at `05:00` (`SCHEDULE_TIME=05:00`) in `UTC`.
- Configure the timezone with `SCHEDULE_TIMEZONE` (example: `America/Chicago`).
- Disable scheduling with `ENABLE_SCHEDULER=false`.
- Time format must be `HH:MM` in 24-hour format. Invalid values are logged and scheduler startup is safely skipped instead of crashing the app.


## Docker

Build and run with Docker:

```bash
docker build -t picklist-automation .
docker run --rm -p 5000:5000 --env-file .env \
  -v $(pwd)/exports:/app/exports \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/picklist_history.db:/app/picklist_history.db \
  -v $(pwd)/query.sql:/app/query.sql:ro \
  picklist-automation
```

Or use Docker Compose:

```bash
docker compose up --build -d
```

This image includes the Microsoft ODBC Driver 18 and `pyodbc`, so SQL Server connectivity works inside the container when `MSSQL_CONNECTION_STRING` is set correctly.


### SMTP Notes

- `SMTP_RECIPIENT` supports comma-separated addresses.
- Set `SMTP_USE_TLS=false` only when your SMTP server expects plaintext or already-terminated TLS.
