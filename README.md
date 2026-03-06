# Picklist Automation (Flask)

Simple Python app that:
- Reads SQL queries from files (`sql/query_guns.sql` and `sql/query_components.sql`)
- Runs against Microsoft SQL Server
- Stores each run in SQLite (keeps last `MAX_RUNS_TO_KEEP` runs)
- Shows latest picklist in a web UI
- Exports latest picklist to Excel
- Exports prior successful runs from the Recent Runs table
- Sends Telegram + SMTP notifications for success/failure
- Uses cross-channel fallback alerts if Telegram/SMTP delivery fails
- Supports UI-managed runtime settings (stored in SQLite, override `.env`)
- Runs automatically on a daily scheduler

## Setup

1. Create virtual environment and install requirements:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy env file and configure:
   ```bash
   cp .env.example .env
   ```
3. Run setup script (creates required runtime directories):
   ```bash
   ./scripts/setup.sh
   ```
4. Update SQL files in `sql/` with your real queries.
5. Start app:
   ```bash
   python3 app.py
   ```
6. Open: `http://localhost:5000`

## Runtime Settings UI

Open `http://localhost:5000/settings` to set:
- `MSSQL_CONNECTION_STRING`
- Telegram token/chat ID
- SMTP settings

The settings page is password-gated:
- Env var: `SETTINGS_PASSWORD`
- Example in `.env.example`: `InforSystem` (change this for your environment)

Values saved in Settings are persisted in `picklist_history.db` and take precedence over matching `.env` values.

For sensitive saved values (`MSSQL_CONNECTION_STRING`, Telegram bot token, SMTP password), set
`SETTINGS_ENCRYPTION_KEY` to enable encryption-at-rest in SQLite:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Access Control

Default is lightweight network-based protection (`ACCESS_MODE=private`):
- Allows private and loopback clients
- Blocks public internet clients
- No additional passwords for warehouse users

Options:
- `ACCESS_MODE=private` (default)
- `ACCESS_MODE=cidr` with `ACCESS_ALLOWED_CIDRS=10.0.0.0/8,192.168.1.0/24`
- `ACCESS_MODE=off` (not recommended)

If behind a reverse proxy, set `TRUST_PROXY_HEADERS=true`.

## Scheduler

- Controlled by `ENABLE_SCHEDULER=true|false`
- Daily run time from `SCHEDULE_TIME` (`HH:MM`) and `SCHEDULE_TIMEZONE`
- Uses a file lock so only one process starts the scheduler
- For 5:00 AM Denver local time, set:
  `SCHEDULE_TIME=05:00`
  `SCHEDULE_TIMEZONE=America/Denver`
- For fixed MST year-round (UTC-7), use:
  `SCHEDULE_TIMEZONE=Etc/GMT+7`

## Docker

Build and run:

```bash
docker build -t picklist-automation .
./scripts/setup.sh
docker run --rm -p 5000:5000 --env-file .env \
  -v "$(pwd)/exports:/app/exports" \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/sql:/app/sql:ro" \
  picklist-automation
```

Or with Compose:

```bash
./scripts/setup.sh
docker compose up --build -d
```

## API / curl

If you run via this repository's Compose file (`8081:5000`), use `http://127.0.0.1:8081`.

Health check:

```bash
curl -sS http://127.0.0.1:8081/health
```

Fetch a CSRF token (stores a session cookie in `cookies.txt`):

```bash
curl -sS -c cookies.txt http://127.0.0.1:8081/api/csrf
```

Trigger a run (CSRF required on API endpoint):

```bash
CSRF_TOKEN=$(curl -sS -c cookies.txt http://127.0.0.1:8081/api/csrf | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')
curl -sS -b cookies.txt -X POST http://127.0.0.1:8081/api/run \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: ${CSRF_TOKEN}" \
  -d '{"query_type":"guns"}'
```

## Notes

- `MSSQL_CONNECTION_STRING` must be a SQLAlchemy SQL Server URL (`pyodbc` driver).
- Logs: `logs/app.log`
- Request logs include method/path/status/response time.
- Exports: `exports/`
- Database: `data/picklist_history.db` (default, configurable via `RUN_HISTORY_DB_PATH`)
