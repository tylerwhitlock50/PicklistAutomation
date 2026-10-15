# Dispatch: pick, pack and ship

Dispatch is a separate React/TypeScript PWA and Flask API. It preserves exact
sales-order-line and trace identities through checkout, picking, package scans,
VISUAL reconciliation and UPS label verification. The old application's production
configuration has not been switched over. Dispatch owns its rules, settings,
accounts, holds, exceptions, reservations and audit history. It has no runtime
dependency on the old app, its database, its scheduler or its Python package.

## Run the training workspace

From the repository root, install only Dispatch's Python dependencies:

```powershell
python -m pip install -r requirements.fulfillment.txt
cd operator-web
npm ci
npm run build
cd ..
python scripts/run_fulfillment_demo.py
```

Open `http://127.0.0.1:5090`. Training-only login: administrator **DEMO**, PIN **123456**.
This uses `data/fulfillment-demo.db` and synthetic records. It never connects to VISUAL.
The sample order has two serials (`DEMO-001`, `DEMO-002`) at `R09S02` and six
`ACCESSORY-B` units (UPC `100002`) at `BIN-04`. Scan any cart barcode to check out.
After picking, accept the cart, add/print package barcodes, and rescan contents.
The training-only button prepares a sample ERP packlist and labels; the operator
must still verify the packlist, scan each tracking number and release the shipment.
Sample ERP documents are in process memory; prepare them again after restarting.
For another rehearsal, use `python scripts/run_fulfillment_demo.py --fresh --port 5091`.
This creates a separate training database and preserves the previous run.
Use a fresh session to get the administrator account when upgrading an older demo.
The demo now evaluates saved Dispatch rules, so holds and early-release actions
can be rehearsed against the sample order. Its customer ID is `DEMO-SUPPLY`.

For UI development run the API on 5090 and `npm run dev` in `operator-web`.
Vite proxies `/api` to the backend. Production serves the compiled files directly
from Flask behind HTTPS; there is no production development server.

## Production structure

- `fulfillment/`: independent API, state machine, database schema and read-only ERP adapter.
- `fulfillment/rules/` and `fulfillment/sql/`: Dispatch-owned rule code and SQL assets.
- `fulfillment/worker.py`: independent queued-reservation refresh every 60 seconds.
- `operator-web/`: typed React client, scanner workflow, package barcode printing,
  manifest and asset-only service worker.
- `scripts/worldship_uploader.py`: authenticated Windows-side shipment evidence ingestion.
- `Dockerfile.fulfillment` / `docker-compose.fulfillment.yml`: dedicated API, allocation worker, PostgreSQL and HTTPS proxy.

Importing the new application does not initialize or migrate databases, start
the legacy scheduler, or write ERP records. Database setup is an explicit CLI command.
Schema version 3 uses app-owned relational tables for users, sessions, claims,
requests, events, labels and settings, plus versioned fulfillment JSON documents.
Claims have database-enforced unique resource keys. All mutations share a
transactional mutex row, with savepoints for failed actions; this deliberately
serializes warehouse commands across workers. Rejections and successful commands
are audited. Higher-volume operation can later partition locking by site without
changing the public contract.

There are three modes:

| Mode | ERP source | Operator changes |
|---|---|---|
| `demo` | Synthetic fixtures | Training database only |
| `shadow` (default) | Read-only live VISUAL | Disabled |
| `live` | Read-only live VISUAL | Requires recorded cutover activation |

PostgreSQL is required for shadow/live. SQLite is supported only for tests and
explicit training. Each service instance is configured for exactly one VISUAL site.
Use a dedicated SQL Server login with SELECT permissions only.

## Deployment and first accounts

1. Copy `.env.fulfillment.example` to `.env.fulfillment`. Supply a dedicated
   PostgreSQL password/URL, VISUAL read connection, site, HTTPS origin,
   and two different randomly generated integration tokens of at least 32 characters.
   Keep `FULFILLMENT_MODE=shadow` initially. SQL Server reads are SERIALIZABLE and
   never use `NOLOCK`; assess query latency and locking in the test company first.
2. No legacy database mount or legacy service is needed. Configure release rules,
   timezone and document-review requirements in Dispatch Admin. An optional import
   is described below; the running API never opens the legacy database.
3. Start only the database, then initialize the app schema:

```powershell
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml up -d dispatch-db
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml run --rm dispatch-api python -m fulfillment.cli init
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml run --rm dispatch-api python -m fulfillment.cli user --operator ADMIN --name "Warehouse administrator" --role admin
```

4. Add operators with the CLI and `--role operator` / `--role supervisor`, or manage
   people in **Administration → People** after starting the services below.
   PINs are prompted without echo, hashed with Werkzeug scrypt, and never passed on
   the command line. Account and source-address sign-in budgets are persisted.
   Run `disable-user --operator ID` to revoke access, including existing sessions.
5. Start the API, allocation worker and proxy. Configure internal DNS for `FULFILLMENT_HOST`, trust
   the Caddy internal CA on tablets/workstations, and use the configured HTTPS
   port (8443 by default). For an existing company TLS proxy, route it to the API
   instead and set `FULFILLMENT_PUBLIC_ORIGIN` to the actual external origin.
   Do not expose PostgreSQL or the API port publicly.

```powershell
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml up -d --build
```

6. Review settings in **Administration → Release rules**, preview their impact,
   enter a reason and publish. New/upgraded databases intentionally start unconfigured.
   Settings can be managed in shadow mode; picking remains disabled. Activate only
   after acceptance testing and exclusive warehouse operation are confirmed.

For an existing Dispatch version-1 or version-2 database, back it up, stop API/worker processes,
run `python -m fulfillment.cli init`, and bootstrap an admin with the `user` command.
The migration preserves work, claims, labels, accounts and history. It adds independent
administration defaults that must be reviewed or imported; it does not silently read
old settings or enable live operation. Restart all processes on the new version.

## Serialized inventory audits

Open **Inventory audit** from the Dispatch navigation. The original audit workflow
has been cloned into `fulfillment/audit/`, with its own templates, static assets,
SQL, and `dispatch_audit_*` tables in Dispatch's PostgreSQL database. There are no
legacy imports, database connections, shared tables, or legacy service requirements.
The screens deliberately retain the existing audit workflow and reporting layout.

Available workflows include location, warehouse, tied-work-order and full audits;
point-in-time expected-inventory snapshots; location and serial batch scanning;
verified/misplaced/missing/unexpected classification; duplicate and near-match hints;
append-only exception resolutions; ERP rechecks; accuracy, cadence, problem-location,
repeat-serial and staging-dwell analytics; session Excel and daily CSV reports.
These exports report audit findings and do not create downloadable picking workflows.

Audit identity comes from the Dispatch login. Every mutation requires CSRF and an
idempotency key and records its actor in Dispatch history. Scans, resolutions,
completion counts and request receipts commit together under the database lock.
Completing an audit marks remaining in-scope serials missing; later resolutions
annotate the detection without rewriting completed accuracy. Retries cannot create
duplicate scans or sessions. Administrator-only **Audit settings** controls accuracy
and dwell targets, watched staging locations, refresh interval and per-location cadence.

ERP reads are restricted to the configured site. An ambiguous serial snapshot is
rejected. Where a bare bin name exists in more than one warehouse, scan
`WAREHOUSE/LOCATION`. Tied units still use their sales order barcode as the location.
Rechecking unexpected scans only auto-resolves fresh, unambiguous unit evidence in
the scanned warehouse/location, and preserves the original unexpected detection.
Auditing never adjusts ERP inventory, allocates stock, or changes Dispatch pick claims.
It is available in shadow mode because its writes are confined to audit records.

The API retains `/api/audit/...` paths and is documented in `/api/v1/openapi.json`.
HTML screens and exports are under `/audit`. The dashboard, scanning sessions,
analytics and settings share Dispatch’s dark sidebar, green accents, typography
and responsive controls. Audit settings are grouped into targets, cadence and
change reason panels. Audit records and pages are never
cached by the service worker. The cloned screens use local assets and nonce-protected
scripts; no external font or chart service is needed.

PostgreSQL is required for this cloned module, including audit training. The ordinary
SQLite picking demo does not include inventory audits. To run a full synthetic demo,
set `FULFILLMENT_DEMO_DATABASE_URL` to a **dedicated training PostgreSQL database**,
then run `python scripts/run_fulfillment_demo.py --postgres --port 5092`.
The login remains DEMO / 123456; this mode never reads or changes VISUAL. Example
inventory: DEMO-001 and DEMO-002 at SHIPPING/R09S02; DEMO-003 at MAIN/C2;
DEMO-TIED staged against SO-DEMO-100.

Run `python -m fulfillment.cli init` during a stopped-service upgrade to create the
audit tables. Existing Dispatch work and settings are preserved. Historical audit
records in the original application's PostgreSQL database are **not automatically
copied**; retain that database for historical reporting. No production migration or
live audit source validation has been performed by this implementation.

## Administration and rule ownership

- **Release rules:** global urgency/lookahead windows, destination cooldown,
  warehouse timezone, component codes, exclusions, and customer/default policies.
  Customer policies support accumulation, serialized-unit thresholds, daily release
  time, sweep day, maximum hold age and order mixing. Preview lists newly eligible,
  no-longer-eligible and changed queued prescriptions before publishing.
- **Orders & assignments:** current decision reasons, holds, early-release exceptions,
  document reviews, queued serial reservations and active work. Early release needs
  an expiry and is consumed by the next successful checkout. It cannot bypass stock,
  credit/order holds, documents, exact packlist reconciliation or label checks.
- **Clear queued assignments & hold:** frees queued reservations and places the order
  on hold in one transaction. It refuses active picks/carts/putaway. Use the existing
  correction/cancellation workflow for checked-out stock; claims stay until scanned return.
- **People:** add accounts, change roles, reset PINs and disable users. Changes that
  affect authentication revoke sessions. Transfer active work before disabling a user.
  The last enabled administrator cannot be disabled or demoted. PINs and hashes are
  omitted from audit and API responses; idempotent PIN requests use salted verification.
- **Change history:** actor, reason, before/after settings and order controls. Admin
  writes use CSRF, server-side role checks, optimistic revision checks, and the same
  database transaction lock as checkout. A stale edit must be reviewed again.

Publishing rules never rewrites checked-out prescriptions. New holds and document
requirements are enforced on subsequent pick/order checks and final release. Queued
allocations are recalculated; the worker maintains sticky reservations independently
of the original application. Failed source reads roll back and retain reservations.
The worker does not reserve stock in shadow mode or before live activation. Its last
successful reconciliation appears in Administration. It can also run as a scheduled
`python -m fulfillment.worker --once` task without Docker.

Document checks now belong to Dispatch. If required, an administrator inspects the
actual documents, loads the order, and records a matching/blocked review with a
reference and expiry. Approval is tied to the destination and ERP document facts;
changed or expired approval cannot pass. Existing ERP readiness checks still run.
The legacy OCR service is **not** run or depended upon. Imported blocking document
findings remain explicit holds, and importing an OCR-enabled configuration requires
new reviews. This release does not read/hash document-file contents; replacing a file
in place without changing its ERP facts requires explicitly revoking and renewing
the review. Do not treat this manual workflow as automatic OCR verification.

`/health` checks schema availability, not ERP readiness. Fresh queue reads expose
ERP failures instead of presenting stale eligibility. Secure cookies, same-origin
requests, CSRF headers and server-controlled roles protect operator mutations.
The service worker caches only hashed frontend assets, never API responses,
customer information, credentials or pending mutations. A lost confirmation is
retried with the same idempotency key; it cannot count twice.

## Workflow rules

- One checked-out order per picker/cart, one destination per fulfillment, multiple
  packages. Subsequent shipments of the same order are separate fulfillments.
- Existing release policy logic runs in enforced mode. Prescribed quantities and
  serials are resolved from fresh ERP facts. Active work is subtracted from supply.
- Serial identities include site and part; order lines never collapse merely
  because they share a SKU. Every serialized scan is one unit. Components accept
  decimal quantities in the displayed stocking unit; serialized lots of quantity
  greater than one are unsupported and rejected.
- VISUAL comparison always uses `SHIPPED_QTY` and `PART.STOCK_UM`; it never compares
  stocking quantities with `USER_SHIPPED_QTY`. The operator manifest explicitly
  displays units. If entry uses selling units, VISUAL performs its conversion;
  the saved stocking quantities must reconcile before release.
- Scan a location before picking. Source bins and later cart/station/package
  custody are separate records; phase one does not post ERP location transfers.
- Rescan every serial into its package and confirm component quantities per box.
  Package barcodes are printable CODE128 labels. Print/attach before packing.
- Packlist equality is by order line, part, unit and serial multiset. Duplicate,
  extra, missing or moved serials fail even when total quantities match.
- Every package needs a matching nonvoided WorldShip label. Address matching is
  conservative: case/whitespace normalized, all exported address fields compared.
  Abbreviations or alternate country names require correction upstream; there is
  no fuzzy-match override that could accept a wrong destination.
- Release rechecks the current ERP document, current order/hold facts, cumulative
  shipped quantities, package contents and label evidence. A generated label is
  not carrier pickup. The final app state is **Released for shipping**.
- Supervisors can transfer custody, resolve exceptions and replace an unpicked
  serial with available stock in the same bin. They cannot override mismatches.
- Cancelling picked work enters putaway. Scanned returns to the original bins
  are required before claims are released. Void an existing packlist in VISUAL
  before cancelling or replacing it. No automatic reservation expiry occurs.

## WorldShip mapping and uploader

The current `Z_UPS_SHIPMENTS` export contains order, packlist, tracking, void flag,
street and postal code, but its recipient name is only 20 characters and it lacks
full address fields. It is used as additional live tracking/void evidence, not as
the sole destination source.

Configure a separate WorldShip export with a full current snapshot of relevant
packages, including voided/replaced labels. Export these CSV column headers:

```text
tracking,site,order,packlist,package,void,exported_at,name,address1,address2,address3,city,state,postal,country
```

`package` must carry the app package barcode in a WorldShip reference field. Export
both order and packlist references. `exported_at` must be a timezone-aware ISO
timestamp representing the evidence revision; changed evidence requires a newer
timestamp. Unknown void flags, conflicting revisions and incomplete destinations
are rejected. Verify the actual installed export behavior for multi-package
shipments and voids before enabling live operation.

Refresh the snapshot at least every minute and publish it atomically. The uploader
refuses a source file older than two minutes. Schedule this on the Windows station:

```powershell
python scripts/worldship_uploader.py C:\WorldShip\dispatch-current.csv --origin https://dispatch.local:8443
```

Provide `FULFILLMENT_LABEL_TOKEN` in that task's protected environment and trust
the company's CA (`REQUESTS_CA_BUNDLE` if needed); never disable TLS verification.
The API requires evidence received within five minutes at label confirmation and
release, and also rereads `Z_UPS_SHIPMENTS`. An event-only export that omits voids
does **not** satisfy this contract. Missing/stale evidence leaves work pending.
No existing WorldShip configuration has been changed by this implementation.

## API and VISUAL save integration

`GET /api/v1/openapi.json` is the public OpenAPI 3.1 contract. Browser callers use
PIN sessions; machine callers use separate bearer tokens:

- Label token: `POST /api/v1/integrations/worldship/labels` only.
- Validation token: machine manifest GET and VISUAL validation only.

The UI manifest is `GET /api/v1/fulfillments/{id}/manifest`. The machine equivalent
is `GET /api/v1/integrations/visual/fulfillments/{id}/manifest`.

```json
{
  "fulfillment_id": "uuid-from-checkout",
  "version": 12,
  "packlist": "PL-123456"
}
```

POST that body to `/api/v1/integrations/visual/validate` to read and compare the
saved document. Alternatively supply `candidate` using the OpenAPI Packlist schema
for an advisory pre-save check. Responses distinguish `pass`, `mismatch` and
`unavailable`; stale revisions return 409. Neither endpoint grants shipment release.
All operator changes carry `version` and `Idempotency-Key`; uncertain retries use
the identical body/key. Clear 4xx rejections require a new corrected request.

## Cutover and rollback

1. Rehearse mixed orders, partial releases, duplicate SKU lines, multiple boxes,
   packlist changes, label voids, disconnects and stock returns on actual scanners.
2. Finish or reconcile every legacy pick and verification session.
3. Set `FULFILLMENT_OPERATOR_CUTOVER=true` on the legacy app and restart its workers.
   Its operator mutations, picklist exports and scheduled allocation generation
   are then rejected; the reservation writer also refuses to run. Disable its
   picklist scheduler to avoid repeated intentional failure notifications.
4. Back up both databases. Optionally import into a fresh, unconfigured Dispatch
   database. Preview first; apply only after legacy operations are stopped:

```powershell
python -m fulfillment.cli import-legacy --legacy-db data/picklist_history.db
python -m fulfillment.cli import-legacy --legacy-db data/picklist_history.db --apply --legacy-stopped
```

Run this with the new database URL, site and SELECT-only VISUAL connection in the
environment. Import includes saved release rules, unexpired exceptions, active holds,
blocking document findings and line-specific reservations. It refuses active legacy
work, missing order-line identity, duplicate/missing serial stock, changed line references
or overwriting existing Dispatch configuration. It never writes the source database.
Review settings formerly supplied only by the legacy environment; they cannot be
recovered from the database. In Docker, mount the backup read-only for this one CLI
invocation; do not add it to the production API/worker mounts.
5. Preview and publish the imported/new rules in Dispatch Admin. Set the required
   document reviews, then activate with:

```powershell
python -m fulfillment.cli activate --exclusive-operation
```

This attests that no other system allocates this site's stock. A legacy installation
is not required for a clean deployment. Set `FULFILLMENT_MODE=live` and restart API
and worker. The old application may remain available for historical reporting;
all current fulfillment administration is now in Dispatch.

For rollback, pause new checkouts by switching Dispatch to shadow. Reconcile or
complete every in-flight cart/package and ERP document. Restore/transfer current
reservations before re-enabling legacy allocation. Never run both operator systems
against the same stock simultaneously.

Back up PostgreSQL using `pg_dump -Fc` to durable storage; restore into a separate
database with `pg_restore` and validate schema, active claims, work and audit counts
before promoting it. Keep the legacy database/history backup. No audit pruning is
automatic in this initial release. Monitor API 503s, aged `updated_at` values,
open exceptions, uncompleted putaway, the allocation worker timestamp and WorldShip
export freshness. Returning to version 1 requires the pre-upgrade backup and an
explicit reconciliation of work since the upgrade; do not run the old API against
the new database.

## Phase-two research result and boundary

Infor documents shipment logic in the API Toolkit, and VISUAL's Sales guide
documents packlist creation before/during shipment:

- [Official toolkit release notes](https://docs.infor.com/visual/9.x/en-us/useradminlist/InforVISUAL901ReleaseNotes_53007.pdf)
- [VISUAL Sales guide](https://docs.infor.com/visual/11.x/en-us/useradminlist/VISUALSale.pdf)
- [WorldShip export mapping](https://www.ups.com/worldshiphelp/WSA/ENU/AppHelp/mergedProjects/CORE/CONNECT/Create_or_Edit_an_Export_Map_from_WorldShip_to_an_ODBC_Database.htm)

The installed toolkit binaries, licensed version and Shipping Entry save-hook
capabilities were not available in this workspace. No undocumented toolkit call
or UI keystroke automation has been invented. `scripts/inspect_visual_toolkit.ps1`
provides a read-only version/document inventory on the VISUAL workstation.

Before implementing automatic creation, obtain the installed Sales Class Library
Reference and development guide, then prove exact line/trace/location assignment,
transaction rollback, and timeout recovery in a test company. Record a unique
fulfillment reference in a supported VISUAL field and search for that reference
before retrying creation. Reuse the same saved-document reconciliation afterward.
Determine whether the actual save hook runs before or after commit and whether
it can block; do not claim that an after-save warning prevents posting. Phase one
cannot prevent someone bypassing Dispatch and shipping directly in VISUAL.

## Validation commands

```powershell
python -m pytest tests/test_fulfillment.py tests/test_fulfillment_integrations.py tests/test_fulfillment_admin.py tests/test_dispatch_rules.py tests/test_dispatch_audit.py -q
cd operator-web
npm test
npm run build
```

For database concurrency tests, set `FULFILLMENT_TEST_DATABASE_URL` to an **empty,
disposable PostgreSQL database**. The fixture drops/recreates its app tables before
each test. Never point this variable at production. Then run the same Python suite.
The tests use synthetic ERP fixtures and do not write or call live ERP systems.

Validated on 2026-10-14: 432 Python tests (plus 11 subtests), 64 Dispatch tests against
PostgreSQL 17, and 5 frontend tests passed. The production frontend and standalone
Docker image build passed, including schema initialization, admin preview/publish,
allocation worker, container health, frontend hosting, OpenAPI and ODBC Driver 18
checks with no legacy app files or mounts. The admin browser rehearsal covered
publishing settings, clearing assignments with a hold, revoking it, and history.
The earlier operator browser rehearsal completed checkout, prescribed
serial and quantity picks, handoff, package rescans, synthetic VISUAL reconciliation,
label verification and release. Desktop and tablet-sized layouts were inspected;
the browser reported no console errors during that rehearsal.

Real scanner/printer hardware, installed VISUAL SQL behavior, WorldShip export
mapping, network/certificate deployment and save-hook behavior remain test-company
acceptance items. Production cutover has not been activated.

Audit extension validated on 2026-10-15: the full regression run passed 434 tests
(11 subtests); PostgreSQL-only audit cases were checked separately. The 64 Dispatch
workflow/admin checks and 14 audit checks passed against PostgreSQL 17. All 5
frontend tests passed. The standalone image passed a PostgreSQL smoke test for
schema setup, audit pages, scans, completion, analytics, settings and Excel export
with no legacy app present. Browser testing verified snapshot creation, batch
classification and follow-up recording; the dashboard and analytics layouts were
inspected. The browser's native confirmation stalled during rehearsal, so completion
now uses an in-app dialog; completion was verified through API and container tests.
