# Dispatch: pick, pack and ship

Dispatch is a separate React/TypeScript PWA and Flask API. It preserves exact
sales-order-line and trace identities through checkout, picking, package scans,
VISUAL reconciliation and UPS label verification. The old application's production
configuration has not been switched over.

## Run the training workspace

From the repository root, with the existing Python dependencies installed:

```powershell
cd operator-web
npm ci
npm run build
cd ..
python scripts/run_fulfillment_demo.py
```

Open `http://127.0.0.1:5090`. Training-only login: operator **DEMO**, PIN **123456**.
This uses `data/fulfillment-demo.db` and synthetic records. It never connects to VISUAL.
The sample order has two serials (`DEMO-001`, `DEMO-002`) at `R09S02` and six
`ACCESSORY-B` units (UPC `100002`) at `BIN-04`. Scan any cart barcode to check out.
After picking, accept the cart, add/print package barcodes, and rescan contents.
The training-only button prepares a sample ERP packlist and labels; the operator
must still verify the packlist, scan each tracking number and release the shipment.
Sample ERP documents are in process memory; prepare them again after restarting.
For another rehearsal, use `python scripts/run_fulfillment_demo.py --fresh --port 5091`.
This creates a separate training database and preserves the previous run.

For UI development run the API on 5090 and `npm run dev` in `operator-web`.
Vite proxies `/api` to the backend. Production serves the compiled files directly
from Flask behind HTTPS; there is no production development server.

## Production structure

- `fulfillment/`: independent API, state machine, database schema and read-only ERP adapter.
- `operator-web/`: typed React client, scanner workflow, package barcode printing,
  manifest and asset-only service worker.
- `scripts/worldship_uploader.py`: authenticated Windows-side shipment evidence ingestion.
- `Dockerfile.fulfillment` / `docker-compose.fulfillment.yml`: dedicated API, PostgreSQL and HTTPS proxy.

Importing the new application does not initialize or migrate databases, start
the legacy scheduler, or write ERP records. Database setup is an explicit CLI command.
Schema version 1 uses app-owned relational tables for users, sessions, claims,
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
   PostgreSQL password/URL, VISUAL read connection, site, timezone, HTTPS origin,
   and two different randomly generated integration tokens of at least 32 characters.
   Keep `FULFILLMENT_MODE=shadow` initially. SQL Server reads are SERIALIZABLE and
   never use `NOLOCK`; assess query latency and locking in the test company first.
2. Confirm the legacy database is readable at the mounted path. Current policies,
   manual holds, exceptions and document-check holds are read from it in SQLite
   read-only mode. Invalid or missing context fails closed. Document-check freshness
   is required when legacy OCR is enabled. The legacy management/readiness service
   remains responsible for those administrative facts, not fulfillment reservations.
3. Start only the database, then initialize the app schema:

```powershell
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml up -d dispatch-db
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml run --rm dispatch-api python -m fulfillment.cli init
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml run --rm dispatch-api python -m fulfillment.cli user --operator SUPERVISOR --name "Shipping supervisor" --role supervisor
```

4. Add individual operators with the same `user` command and `--role operator`.
   PINs are prompted without echo, hashed with Werkzeug scrypt, and never passed on
   the command line. Account and source-address sign-in budgets are persisted.
   Run `disable-user --operator ID` to revoke access, including existing sessions.
5. Start the API and proxy. Configure internal DNS for `FULFILLMENT_HOST`, trust
   the Caddy internal CA on tablets/workstations, and use the configured HTTPS
   port (8443 by default). For an existing company TLS proxy, route it to the API
   instead and set `FULFILLMENT_PUBLIC_ORIGIN` to the actual external origin.
   Do not expose PostgreSQL or the API port publicly.

```powershell
docker compose --env-file .env.fulfillment -f docker-compose.fulfillment.yml up -d --build
```

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
4. Back up both databases. Import and validate legacy serial reservations and
   activate the new database while it has no fulfillments:

```powershell
python -m fulfillment.cli activate --legacy-db data/picklist_history.db --legacy-stopped
```

Run this with the new database URL, site and SELECT-only VISUAL connection in the
environment. The command refuses active legacy work, missing order-line identity,
missing serial stock and changed line references. `--legacy-stopped` is an explicit
operator attestation, not automatic remote proof of deployment configuration.
5. Set the new service to `FULFILLMENT_MODE=live` and restart. Maintain legacy
   readiness/holds administration and read-only reporting; Dispatch owns new work.

For rollback, pause new checkouts by switching Dispatch to shadow. Reconcile or
complete every in-flight cart/package and ERP document. Restore/transfer current
reservations before re-enabling legacy allocation. Never run both operator systems
against the same stock simultaneously.

Back up PostgreSQL using `pg_dump -Fc` to durable storage; restore into a separate
database with `pg_restore` and validate schema, active claims, work and audit counts
before promoting it. Keep the legacy database/history backup. No audit pruning is
automatic in this initial release. Monitor API 503s, aged `updated_at` values,
open exceptions, uncompleted putaway and WorldShip export freshness.

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
python -m pytest tests/test_fulfillment.py tests/test_fulfillment_integrations.py -q
cd operator-web
npm test
npm run build
```

For database concurrency tests, set `FULFILLMENT_TEST_DATABASE_URL` to an **empty,
disposable PostgreSQL database**. The fixture drops/recreates its app tables before
each test. Never point this variable at production. Then run the same Python suite.
The tests use synthetic ERP fixtures and do not write or call live ERP systems.

Validated on 2026-10-14: 364 Python tests (plus 11 subtests), 37 tests against
PostgreSQL 17, and 3 frontend API tests passed. The production frontend and Docker
image build passed, including container health, frontend hosting, OpenAPI and
ODBC Driver 18 import checks. A browser rehearsal completed checkout, prescribed
serial and quantity picks, handoff, package rescans, synthetic VISUAL reconciliation,
label verification and release. Desktop and tablet-sized layouts were inspected;
the browser reported no console errors during that rehearsal.

Real scanner/printer hardware, installed VISUAL SQL behavior, WorldShip export
mapping, network/certificate deployment and save-hook behavior remain test-company
acceptance items. Production cutover has not been activated.
