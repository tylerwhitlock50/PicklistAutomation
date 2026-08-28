# Picklist Automation (Flask)

Simple Python app that:
- Reads SQL queries from files (`sql/query_guns.sql` and `sql/query_components.sql`)
- Runs against Microsoft SQL Server
- Stores each run in SQLite (keeps last `MAX_RUNS_TO_KEEP` runs)
- Shows latest picklist in a web UI
- Shows JP's six-metric Shipping management scorecard with daily trends and action queues
- Evaluates auditable order-release decisions before allocation (advisory by default)
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

## Shipping management scorecard

Open `http://localhost:5000/shipping` for the default 30-day management view. It includes:

- Ship on time and ship complete, with explicit cohorts and denominators
- Average guns per shipment, single-gun shipments, total guns, and total shipments
- Prior-period comparisons, daily service/volume trends, late-order and single-gun queues
- Promise-date/source coverage so missing data is visible rather than scored silently
- A release queue showing `SHIP NOW`, `ACCUMULATING`, `HOLD`, and `BLOCKED` with reasons and next dates

The home page also displays a compact four-metric strip linked to the full six-metric scorecard. JSON consumers can use `GET /api/shipping/metrics?days=30` and `GET /api/shipping/release-gate`.

The scorecard uses VISUAL packlists as shipment grain and serialized trace units as the gun measure. Metric contracts, rollout steps, and reconciliation checks are documented in [`docs/shipping-kpi-release-gates-plan.md`](docs/shipping-kpi-release-gates-plan.md).

## Order release gate

Configure the gate from Settings. New installs start in `advisory`, so Shipping can validate the queue without changing picklists. After sign-off, `enforced` injects only released order IDs into both picklist queries before supply allocation. If the release evaluation fails while enforced, the run stops instead of bypassing the policy.

Rule precedence is: hard blocks; approved temporary exceptions and ordinary commitment protection; configured protected accumulation; complete-order release; customer batch/sweep policies; then wait for completion. `HOLD` does not reserve inventory. `ACCUMULATING` does: allocatable units remain in their shelf locations but are subtracted before later normal orders are evaluated. Every evaluation recomputes that logical protection from current supply, and every policy version, decision, allocation, and temporary exception is retained in SQLite.

Example customer-policy JSON (Thursday is weekday `3`):

```json
{
  "LIPSEYS": {
    "accumulate": true,
    "min_guns": 100,
    "sweep_weekday": 3
  }
}
```

With `accumulate: true`, the gate protects available guns for that customer. With no target or sweep, a protected order releases when it becomes complete. When `min_guns` and/or `sweep_weekday` is configured, the protected customer batch releases at the target or sweep; an approved temporary exception can release it sooner. An accumulating order inside the commitment-protection window remains protected and is flagged for management review instead of being fragmented automatically. Customers without `accumulate: true` retain the prior non-reserving behavior.

Customer-specific accumulation, thresholds, and sweep days are intentionally unset until JP and Shipping approve them; the email's Lipsey's cadence was an example, not an approved production rule.

To reconcile the queries against the configured live VISUAL source without exposing order/customer detail:

```bash
python scripts/validate_shipping_management.py --days 30
```

This command is read-only and prints aggregate KPI, coverage, and release-reason QA.

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
- For fixed MST year-round (UTC-7), use either:
  `SCHEDULE_TIMEZONE=UTC-7`
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
