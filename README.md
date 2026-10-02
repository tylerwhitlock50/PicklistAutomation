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
   python -m picklist.app            # Linux / macOS
   python scripts/run_dev_windows.py # Windows
   ```
   Production runs under gunicorn: `gunicorn -b 0.0.0.0:5000 picklist.app:app`.
6. Open: `http://localhost:5000`

## Project layout

```
picklist/              application package (gunicorn target: picklist.app:app)
  app.py               Flask app, request hooks, context processors, service wiring
  config.py            env-derived settings, paths, logging
  db.py                SQLite connection, schema bootstrap, encrypted settings table
  security.py          trusted-client gate and CSRF
  features.py          feature rollout flags
  erp.py               SQL Server engines and query-file execution
  scheduler.py         APScheduler jobs and the single-instance lock
  routes/              one Flask blueprint per area (runs, settings, audit, serial,
                       allocation, shipping, orders, lookup, request_queue, pick, verify)
  services/            app-level logic the routes call (run execution, release gate,
                       shipping reports, readiness data access, notifications, ...)
  domain/              pure business logic (readiness rules, release gate, allocation, ...)
  stores/              SQLite / Postgres persistence modules
sql/                   ERP queries loaded by name from config
templates/, static/    server-rendered UI
tests/                 unittest suites; run with `python -m pytest`
scripts/               dev runner, setup, live validation
docs/                  design notes and plans
data/, exports/, logs/ runtime state (gitignored, mounted as volumes in Docker)
```

Endpoints are namespaced by blueprint, so templates use `url_for('orders.orders_page')`
rather than `url_for('orders_page')`. URL paths did not change.

## Navigation

The app is organised by intent, four tabs across the top with a second row for the pages in the active group:

| Tab | Question | Pages |
|---|---|---|
| **Work** | I need to do something | Today (`/work`), Run picklist (`/`), Pick orders, Verify boxes, Audit |
| **Reports** | Is it working? | Scorecard, Holds, Shortages, Reconciliation, Excess packlists, Staged shipments, Audit analytics, Run history (`/runs`) |
| **Lookup** | Answer a question | Search (`/lookup`), Orders, Shipments, Stock, Serial history, Allocation |
| **Requests** | Ask another team for something | Requests, with a badge showing the open count (red when any is past its SLA) |

The Lookup search box routes by the shape of what you paste: a sales order opens order detail, a part number opens stock, anything else opens serial history. Feature flags hide the pages they cover; a tab with nothing left in it hides itself. The design notes are in [`docs/app-reorg-proposal.md`](docs/app-reorg-proposal.md).

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

The scorecard is the landing page of the Reports tab. JSON consumers can use `GET /api/shipping/metrics?days=30` and `GET /api/shipping/release-gate`.

The scorecard uses VISUAL packlists as shipment grain and serialized trace units as the gun measure. Metric contracts, rollout steps, and reconciliation checks are documented in [`docs/shipping-kpi-release-gates-plan.md`](docs/shipping-kpi-release-gates-plan.md).

## Order release gate

Configure the gate from Settings using the account-policy editor. New installs start in `advisory`, so Shipping can validate the queue without changing picklists. After sign-off, `enforced` rechecks live ERP serial availability, reconciles sticky assignments, and injects only released order IDs into both picklist queries before supply allocation. If the release or serial validation fails while enforced, the run stops instead of bypassing the policy.

Rule precedence is: hard blocks; approved temporary exceptions and ordinary commitment protection; existing sticky serial reservations; configured protected accumulation; complete-order release; account batch policies; then wait for completion. `HOLD` does not reserve inventory. `ACCUMULATING` does: serials remain in their ERP shelf locations but stay tied to the same order locally and are subtracted before later orders are evaluated. Every evaluation revalidates those assignments against current read-only ERP inventory, and every policy version, decision, serial assignment, and temporary exception is retained in SQLite.

The Settings screen writes the policy JSON. `DEFAULT` controls stores not explicitly listed; major-account entries override it:

```json
{
  "DEFAULT": {
    "account_type": "standard",
    "accumulate": true,
    "target_guns": 0,
    "mix_orders": true,
    "release_cadence": "daily",
    "daily_release_time": "14:00",
    "max_hold_days": 7,
    "ship_to_cooldown_days": 1
  },
  "LIPSEYS": {
    "account_type": "major",
    "accumulate": true,
    "target_guns": 42,
    "mix_orders": false,
    "release_cadence": "threshold",
    "daily_release_time": "14:00",
    "max_hold_days": 7,
    "ship_to_cooldown_days": 0
  }
}
```

Major accounts with `mix_orders: false` expose one active sales order at a time and release it at `target_guns`, full completion below the target, `max_hold_days`, or an approved exception. Standard `daily` policies combine only the same customer and ship-to, then release once the configured cutoff arrives or the oldest sticky assignment reaches the maximum age. `ship_to_cooldown_days: 1` prevents a destination shipped today from receiving another picklist until tomorrow; `3` makes the destination next eligible three calendar days after its last shipment. Optional legacy sweep weekdays remain supported.

Two guarantees back the "one shipment per destination per day" goal regardless of per-customer policy. First, a global cooldown floor (`RELEASE_GATE_MIN_SHIP_TO_COOLDOWN_DAYS`, default 1) applies on top of every policy. Second, cooldowns are driven by both actual ERP shipment dates and a local release ledger: every generated picklist logs the customer/ship-to pairs it released, so a destination released this morning is held this afternoon even if VISUAL has not yet recorded the shipment. Audit rows (evaluations and decisions) are written only when a picklist is generated — dashboard and API reads are side-effect free — and the history is capped automatically.

Repository defaults remain advisory. Customer policies can be reviewed and changed in Settings before any enforcement date is approved.

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

To also mount the VISUAL document share for FFL document checks, run
`./scripts/compose_up.sh` (it layers `docker-compose.documents.yml` on top).

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


## Orders, readiness holds, and Teams notifications

The **Orders** tab (and **Shipping > Holds**) replaces the "can this ship?" / "FFL doesn't match" /
"is this released?" traffic that used to live in the Sales-Shipping Teams chat.

- `sql/readiness_candidates.sql` pulls every open physical line for orders in R/F/H status inside
  `READINESS_LOOKAHEAD_DAYS`, with the compliance, credit, status and supply facts as columns.
- `picklist/domain/readiness.py` turns those facts into holds. Every hold has a reason code from `HOLD_REASONS`, an
  owning team (Inside Sales, Finance, Shipping, Production, Compliance) and a blocking flag. Orders
  are **BLOCKED** (someone must act), **ATTENTION** (can ship, worth a look) or **READY**.
- `picklist/stores/readiness_store.py` keeps the punch list in SQLite so hold age is measurable and so a new hold is
  announced exactly once; `picklist/services/readiness_service.py` runs the refresh every `READINESS_REFRESH_MINUTES`
  and after every picklist run.
- `/orders/<SO>` answers the questions the chat used to ask: holds, ship-to and both FFL records,
  credit exposure, where each part physically is, pick status, release-gate decision, hold history.

FFL data is read from `CUST_ADDRESS.USER_4/USER_5` (ship-to, authoritative). The ship-to FFL is
the record the rules run against: expired or unreadable there means "update the ship-to", and the
hold detail points at the customer-master FFL when that one is current. Only when the ship-to has
no FFL number at all does `CUSTOMER.USER_4/USER_5` stand in. A master FFL that differs from the
ship-to FFL is shown on the order page but is not a hold. A firearm order with no FFL on either
record raises `ffl_missing`, the one *critical* hold (dark red pill). The expiry string is parsed
defensively in Python.

**Teams**: paste a Teams Workflows incoming-webhook URL into Settings (or `TEAMS_WEBHOOK_URL`) and
choose which events post. Cards link back to the app via `APP_PUBLIC_URL`. Delivery failures fall
back to email; every attempt is logged in the `notification_log` table.

**Who did it**: Settings > Operator roster lists everyone who can pick their name in the top bar.
The browser remembers the choice and sends it as `X-Operator` / `operator` on every request, so
acknowledgements and (later) requests carry a real name and team.

### Shipments, tracking and stock (self-serve)

- **Shipments** (`/shipments`): packlists with live UPS tracking (Z_UPS_SHIPMENTS, UDF-0000028
  fallback), serials in each box, voided flags; by order, customer or date range. Each
  `/orders/<SO>` page carries the same block.
- **Daily digest**: once per day after `teams_digest_time` (Settings) a "Shipped today" card with
  order, customer, packlist, tracking and units posts to Teams, split into 25-row cards. It is
  idempotent per day (`notification_log`), can be previewed at `/api/shipping/digest/preview` and
  forced with `POST /api/shipping/digest/send`.
- **Stock** (`/stock`): every bin holding a SKU with its serials, classified (pickable R01-R09,
  stage, rack 10, international cage, MAIN), plus what is already allocated to orders, protected by
  the release gate, or held. Serial lookups show where one serial currently sits.

### Requests and sanctioned holds (replacing "set it aside" in Teams)

- **Requests** (`/requests`, Shipping hub tab *Requests*): four typed requests, each with an owning
  team, an SLA clock and a full timeline. `ship_request` (needed-by, service level, expedite),
  `hold_exception` (marketing / VIP / international cage / rework / approved special), 
  `inventory_discrepancy` (part, serial, expected vs actual bin) and `order_problem` (wrong
  tracking, duplicate unit, RMA on picklist, status correction). Every `/orders/<SO>` page carries
  prefilled "Ship this now", "Hold / set aside" and "Report a problem" links. The owning team gets a
  Teams card on create, assign and close.
- **Expedites feed the release gate**: when Shipping or Management accepts a `ship_request` with
  *Accept as expedite*, the app writes a `release_gate_exceptions` row (expires at the end of the
  needed-by day, at most `REQUEST_EXPEDITE_MAX_HOURS` ahead), drops the gate cache and refreshes
  readiness, so the next picklist includes the order. Closing or declining the request revokes it.
  Acceptance is refused while a blocking readiness hold (credit, FFL, status) is open; the request
  page names the hold and its owner.
- **Holds are the only sanctioned set-aside**: a `hold_exception` must name a sales order or work
  order and always expires (default 7 days, max 30). Once Shipping acknowledges it, the order is
  removed from every picklist run (the run notification lists the exclusions) and the release gate
  reports `HOLD / manual_hold` regardless of account policy. Holds expire on the readiness sweep or
  can be released from the order page by Shipping / Management (`POST /api/holds/<id>/release`).
  "Pull one and set it aside, SO coming" has no path: no order, no hold.
- SLA hours per type can be overridden with `REQUEST_SLA_HOURS_JSON`
  (for example `{"ship_request": 2, "order_problem": 48}`); `GET /api/requests?scope=mine|team|all`
  and `GET /api/requests/<id>` expose the queue as JSON.

### FFL document checks (readiness tier 2) and friction analytics

- With `READINESS_OCR_ENABLED=true`, each readiness refresh opens the EZ Check / FFL copy attached
  to a firearms order (VISUAL `DOCUMENT_REFERENCE` -> `DOCUMENT`, joined through
  `DOCUMENT_PATH_MAP` onto the `documents` CIFS mount from `docker-compose.documents.yml`,
  started with `docker compose -f docker-compose.yml -f docker-compose.documents.yml up -d`
  once `SMB_USERNAME` / `SMB_PASSWORD` are in `.env`), extracts the text (pypdf, then
  poppler + tesseract for scans), parses the licensee name, trade names and premise, and compares
  them with the ship-to. Mismatches become `ship_to_vs_ffl_name_mismatch` /
  `ship_to_vs_ffl_premise_mismatch` holds owned by Sales with both strings in the detail. The same
  ZIP plus the same street number passes even when the road is spelled two ways.
  A third, advisory finding (`ffl_record_differs_from_doc`) fires when the license number or
  expiration printed on the document disagrees with the ship-to record in VISUAL, which is how a
  dealer whose EZ Check runs to 2029 ends up flagged as "expiring today".
- Only orders with nothing else blocking them are checked, at most `READINESS_OCR_MAX_DOCS_PER_RUN`
  new documents per refresh; results are cached per file (`ffl_doc_cache`) so a document is read
  once until it changes. Missing OCR dependencies or an unmounted share never fail a refresh; the
  cache row records the error and the order page lists the attachments either way.
- The management scorecard gains a *Holds and requests* block: open hold count and age, holds
  cleared and median time to clear by owning team and by reason, open / past-SLA requests and
  median hours to first response and to close.
