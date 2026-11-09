# Dispatch: pick, pack and ship

## App-owned inventory and operating workflows (schema 5)

The **Inventory** workspace adds a local inventory ledger, manual/API receipts,
work-order supply, customer/order commitments, holds, priority and need-by dates,
promise risks, atomic binding/reassignment, count adjustments and reconciliation.
A read-only VISUAL connector is implemented with durable delivery and source
health checks. The full contract and cutover procedure are in
[Shipping inventory specification](shipping-inventory-spec.md). Production has
not been migrated or activated by installing this code.

The **Operations** workspace adds Sales intake, independent approval, a shared
order-decision board, retained planning history and signed daily close. VISUAL
remains the commercial order authority; Dispatch drafts await an exact existing
order binding. The durable mirror imports complete order facts independently of
shipping lookahead, and a separate order callback can enqueue updates.

Explicit promises and worker-created accumulation commitments persist in the
database. Ordinary ready prescriptions stay tentative until checkout. The worker
defaults to 60 seconds and now retains each attempt's inputs, decisions, changes
and errors. Callbacks queue durable triggers; receipt-linked future promises
promote immediately, while unpromised arrivals enter the shared pool for planning.

Daily close checks quantities, serial identities, two-way movement coverage and
released shipment documents, then retains an independently reviewed packet.
Missing evidence prevents signing; accepted exceptions remain explicitly labelled
exceptions. Supported packlist statuses (`A`, `S`, `P`) establish the saved document
check, not invoicing, financial posting or carrier departure. See the
[implemented operating contract](shipping-inventory-spec.md#17-order-intake-planning-approval-and-daily-close).

To upgrade, stop Dispatch API/worker processes, back up its database and run
`python -m fulfillment.cli init`. The additive version-5 migration supports versions
1–4 and preserves existing work, users and settings. Local inventory starts disabled.
The accepted warehouse is **SHIPPING**; configure its exact site and single location
in **Inventory → Sync & warehouse**, import opening balances
and record a complete matching reconciliation before enabling ownership. Active
work and old queued reservations must be resolved first. The matching comparison
must be at most one hour old and still match the ledger. Test this in training
before any live cutover; no production migration is performed by installing code.

When enabled, receipt balances and customer commitments determine usable stock.
The existing customer release rules still apply; the allocation worker persists
protected accumulation as local commitments. Checkout locks stock locally, pick
scans validate those locks, and final verified release debits stock once. A
cancelled pick stays locked until every picked unit is scanned back. Local serial
substitution currently uses cancellation/putaway and a new checkout.

Future supply never counts as physical inventory. Receiving against a work order
promotes promises by priority, need-by date, creation time and ID, preserving
customer, order, hold and receipt lineage. Priority is 0–100 (lower first), default
50; optional request references connect a promise to an Inside Sales request.
Binding or transferring only unlocked, unconsumed quantity creates an audited
successor without first making the stock public. Exact order bindings validate
current ERP customer/site/line demand. Inventory shows shortages and risks from
overdue reviews, late arrivals, source problems and unavailable production supply.
Customer holds retain their reservation past the review date and show an overdue
flag. Removing a hold permits that customer to pick; freeing the reservation
returns it to the general pool. Supply reductions retain promises and expose the
shortfall for review. Supervisor/admin roles manage inventory; configuration
requires admin. Operators and Sales can read the workspace; Sales submits requests
for independent review. Expired holds remain reserved until explicitly changed.

Set a separate `FULFILLMENT_INVENTORY_TOKEN` (at least 32 random characters).
Machine POSTs require an `Idempotency-Key`, `source: "visual"` and their configured
boundary. The standalone `/api/v1/integrations/inventory/receipts`, `/work-orders`
and `/reconcile` endpoints support a separately governed producer before a stream
is established. Work orders use monotonic `source_revision`; receipt `event_id`
identifies the original source transaction. The built-in connector uses `/state`,
atomic `/batches` and sequence-fenced `/health` instead. Once connected, it owns
receipts and work-order updates exclusively; manual/standalone competing receipt
and supply writes are rejected. The published OpenAPI describes these contracts.

### Read-only VISUAL connector

`fulfillment/visual_inventory_source.py` and `fulfillment/sql/inventory_sync_*.sql`
read the exact configured site/warehouse/location in one `SERIALIZABLE` transaction.
They capture complete `PART_LOCATION` balances, signed `TRACE_INV_TRANS` serial
balances joined to `INVENTORY_TRANS`, complete physical movement history, and
site work orders. `PART_LOCATION` obtains site through `WAREHOUSE`; units come
from `PART_SITE_VIEW.STOCK_UM`. The source UTC observation is read in that same
transaction. No `NOLOCK`, numeric ID watermark or timestamp-only history window
is used. Each result is bounded (default 100,000 rows, adjustable through
`--max-source-rows` after reviewing source capacity); excess rows, incomplete
master mappings, inconsistent totals, non-unit traces, negative stock or positive
unavailable/held/locked/in-transit stock fail the complete capture.

Movement identity is `INVENTORY_TRANS.TRANSACTION_ID`; `ROWID` and
`POSTING_CANDIDATE` are not commit/void filters. Positive `I/R` receipts without a
transfer reference require a verified same-site/part five-key manufacturing work
order or purchase-order line (`PURC_ORDER_ID`/`PURC_ORDER_LINE_NO`). Customer
references, missing/mismatched provenance or mixed manufacturing/purchase sources
go to review. Positive `I/A` arrivals with a valid reciprocal `O/A` transfer from
outside the boundary also become receipt events. Customer-order `O/I`
shipments are compared but never debited again. Other corrections, transfers,
returns or reversals require review. Serial identity is `TRACE_ID`; trace quantities
are already signed, and actual per-unit trace evidence is required rather than
relying on the profile's `SERIAL` flag.

Work-order identity retains all five key fields as compact JSON. Remaining
manufacturing quantity is desired minus received, floored at zero. Only open
`F`/`R`, top-level (`SUB_ID=0`) work orders without existing demand-supply ties
offer free future capacity; other/terminal records remain available for promise
history. Manufacturing completion outside Shipping is not a shipping receipt.
Transfers without explicit five-field work-order lineage stay unlinked. A
supervisor's **link receipt** command can attach verified, still-free receipt
stock to a work order and promote promises without adding inventory again.

An otherwise valid source receipt containing a serial with prior inventory
history, or reused across distinct receipt events in one batch, becomes a
`receipt_review` issue. All affected receipts, including mixed
known/new serials, is recorded as a replay-safe review event with no stock or
future-supply credit. Synchronization still completes its comparison so staff
can inspect return/rework evidence and use a count adjustment. Review events
cannot be linked to work orders as received supply; malformed/duplicate serial
lists or mismatched quantities still reject the batch.

`python -m fulfillment.inventory_connector` uses a durable SQLite outbox separate
from the app database. It records the exact pending batch before HTTPS delivery
and retries uncertain outcomes unchanged. Every poll audits all previously seen
transaction fingerprints, including deleted identities. Late/lower IDs are found;
edits/deletions raise issues; restoring an old transaction never receives it twice.
Version-2 movement evidence retains full transaction lineage, corrections and
tombstones on the receiver. The producer limits a batch to 15 MiB; only the
inventory batch endpoint allows 16 MiB JSON (ordinary commands allow 2 MiB).
The receiver atomically applies a contiguous source sequence and rejects local
stock changes during capture or stale source observations before business writes.

Configure `FULFILLMENT_INVENTORY_API_ORIGIN`, the dedicated token, source connection,
`FULFILLMENT_SITE`, `FULFILLMENT_INVENTORY_WAREHOUSE`,
`FULFILLMENT_INVENTORY_LOCATION` and durable `FULFILLMENT_INVENTORY_OUTBOX`.
During a coordinated stop of warehouse movements, bootstrap once into **disabled,
empty local inventory** with no active work or legacy reservations:

```powershell
python -m fulfillment.inventory_connector --bootstrap --once
python -m fulfillment.inventory_connector --status
python -m fulfillment.inventory_connector
```

Bootstrap imports the complete opening balance and stores every source identity
and detailed movement already represented by it. Durable checkpoint delivery is
acknowledged separately; daily close requires that confirmation. Review the resulting matching reconciliation before
enabling local inventory and completing the separate exclusive-operation cutover.
Run one active connector owner with the original outbox. Back up its outbox and
the Dispatch database together; do not reset the stream to bypass a conflict.

Polling defaults to 60 seconds (`--interval` accepts 15–120). A connected source
older than 300 seconds, or an immediate capture/delivery failure/pending report, blocks
source-owned availability and final shipment release. Fresh negative variances
block the affected stock from new planning/picking. Already-picked work may
finish release after its exact live ERP packlist and label proof succeeds; this
closes the expected ERP-posting-before-local-debit gap. Fresh accepted evidence
clears health errors; a reasoned issue acknowledgement does not clear quarantine.
`delivery_confirmed` clears only the pending-delivery condition after checkpoint
persistence; it cannot clear an unrelated capture failure.

Use `--status` to inspect safe failure diagnostics. `capture_stale` batches are
rejected before writes and automatically recaptured. For another recorded HTTP
4xx rejection, fix the cause and use `--recapture-rejected`; it verifies that the
receiver has not advanced, preserves the accepted identity map and records the
discard. Timeout, 5xx or malformed-response deliveries remain pending and must
retry unchanged. Validate SQL load, transaction classifications, full history,
backup/restore and this recovery procedure in a test company before deployment.

Receiving and comparison can be prepared in shadow mode; ordinary picking stays
disabled there. Local views and customer-only commitment changes work without
ERP stock reads. Manual-source receiving remains available; connected receiving
waits for valid source captures. Local-stock planning needs a fresh complete order
mirror; binding/approval and final shipping require current ERP evidence plus healthy
connected inventory evidence. Reconciliation saves differences and never
overwrites stock. Manual receipts and adjustments require the corresponding
supported ERP process to keep accounting in agreement.

A reviewed positive adjustment can introduce previously unseen return/correction
stock only when the connected source is healthy and its latest accepted
reconciliation proves that exact positive difference, the amount does not exceed
it, and local stock is unchanged since comparison. Existing serial returns can
restore a zero-balance identity with a reasoned adjustment. Neither route retries
the original receipt or silently rewrites ERP stock; protected stock cannot be
removed by an adjustment.

The sections below describe the original ERP-stock mode wherever they refer to
live shelf/bin supply. They continue to apply when local inventory is disabled.

Dispatch is a separate React/TypeScript PWA and Flask API. It preserves exact
sales-order-line and trace identities through checkout, picking, package scans,
VISUAL reconciliation and UPS label verification. The old application's production
configuration has not been switched over. Dispatch owns its rules, settings,
accounts, holds, exceptions, reservations and audit history. It has no runtime
dependency on the old app, its database, its scheduler or its Python package.

## Order intake, approvals, planning and daily close

The **Operations** workspace has five views: **Order decisions**, **Sales intake**,
**Approval inbox**, **Planning history** and **Daily close**. Sales can read these
views and manage its own requests. Shipping operators can also submit requests.
Supervisors/admins review requests and run planning or close; early-release review
and policy/account configuration require admin. Use separate named human accounts
for requester/reviewer and close preparer/signer.

### First setup and order capture

1. Back up Dispatch and its connector outbox, stop API/worker processes and run
   `python -m fulfillment.cli init` for schema 5. Restart matching application
   processes. This preserves work and history; it does not activate production.
2. Confirm warehouse **SHIPPING**, the exact VISUAL site and single shipping
   location. Expired customer holds remain reserved and require review. Establish
   the approved posting, receiving, exception-owner and human-review procedures.
3. Add Sales users through Administration → People or
   `python -m fulfillment.cli user --operator SALES --name "Inside Sales" --role sales`.
   PINs are prompted securely. Add at least two appropriate human reviewers for
   independent request/close approval.
4. Configure `FULFILLMENT_ORDER_TOKEN` separately from the INVENTORY, LABEL and
   VALIDATE credentials when using order callbacks. Callback ingestion accepts
   existing source orders only; keep the supported VISUAL commercial order-entry
   process in place.
5. Rehearse modern version-2 opening capture, complete order capture, source
   classifications and close coverage in a test company. Inventory opening and
   ownership activation retain the separate checks described above.
6. After the authorized exclusive-operation cutover, run planning once and inspect
   its saved result before the first live local-stock checkout. The complete mirror
   must be initialized, healthy and fresh; one order callback is insufficient.
   The periodic worker then refreshes it and records each attempt.

The separate order callback is `POST /api/v1/integrations/orders`. Use the ORDER
bearer and `Idempotency-Key`; a browser CSRF token is not used for this machine
endpoint. The body contains `event_id`, nonnegative integer `source_revision`,
timezone-aware `observed_at` and `order`. Order facts include site/order/customer,
destination, exact line identities, units, serialized flags and quantities; retain
verified status, credit, dates, ordered/shipped baselines and PO/reference. The
OpenAPI contract at `/api/v1/openapi.json` defines field shapes. Replaying the same
event/key cannot create duplicate demand; old revisions cannot undo new ones.
A successful complete worker capture repairs missed events and establishes full
book freshness. The callback is not an ERP writer or a completeness certificate.

### Record a sales request and reserve supply

1. In Sales intake, save customer/site, PO/reference, destination and requested
   lines. Submit the draft. It displays **awaiting VISUAL order** and cannot ship.
2. Create the commercial order through the supported VISUAL process. A supervisor
   binds the request to exact ERP lines. Customer/site, PO, address, units and
   remaining quantities must agree. Duplicate customer references need an
   independent review reason. Cumulative shipped evidence prevents old completed
   bindings from blocking legitimate remaining demand without allowing duplicates.
3. To reserve stock beforehand or afterward, create an Approval inbox request for
   stock or a named work-order supply. Record quantity, customer, optional exact
   order/line, priority, need-by date, hold/review date and reason. Link the sales
   request when applicable. Save and submit; neither state reserves stock.
4. A different active supervisor/admin reviews the exact terms and approves or
   rejects them. Approval rechecks current supply, source freshness, order facts,
   revisions and requested quantity under the warehouse lock. A shortage or stale
   input leaves the request unapproved. Revise and submit new terms rather than
   silently reducing an approved quantity.
5. Review outcome/progress in the inbox and commitments in Inventory. Future
   receipts immediately promote exact work-order promises and preserve the hold.
   Fulfilled quantities continue to count toward the original sales-request limit.

Use separate requests to reassign a promise, remove its hold, free its remaining
stock, correct a count or request early release. Removing a hold preserves its
owner; freeing a commitment makes its unlocked balance shared. Expiry only marks
overdue review. Reassignments cannot consume picked stock or certify uncovered
future supply. Adjustments require an unchanged count basis and protect existing
promises. Early release requires a different administrator and the same source
order context; it does not override hard shipping checks. Existing direct
supervisor/admin commands remain available as audited administrative operations.

For an uncertain response, use the displayed retry action with the original body
and idempotency key. Do not create a replacement request to force success. A
source-unavailable approval can retry after recovery; rejected business conditions
need a newly reviewed operation/key when their circumstances change.

### Explain and operate planning

Order decisions shows what blocks each mirrored order, the responsible owner,
the next action, source age and saved decision age. Planning history opens the
inputs, policies, source revisions, decisions and allocation changes for an earlier
attempt, including failures. A changed input invalidates a previously ready label
until replanning. Checkout records its own decision provenance and locks exact stock.

The worker defaults to 60 seconds (`--interval` supports 15–3600). It refreshes
the complete order mirror and applies published release policies. Order callbacks,
receipts and relevant request/policy/inventory changes queue durable triggers;
they do not synchronously run the general planner. Receipt-linked future promises
promote immediately. Remaining free stock is planned on the next worker/manual run.
Protected accumulation becomes durable; ordinary ready suggestions stay tentative.

When a run fails, inspect the retained error and restore the source/configuration.
Existing commitments remain, and triggers stay pending for retry. The run history
identifies abandoned worker attempts after the lease. Do not clear allocations or
reset the connector stream as a recovery shortcut. Local-stock planning requires
healthy order evidence within 300 seconds; shipping additionally retains its live
order/packlist/label and inventory-source checks.

### Close the warehouse day

1. Coordinate a pause of warehouse movements, finish or account for active work,
   and let the connector deliver a fresh complete capture. Its durable checkpoint
   must acknowledge the accepted sequence. A pending or uncertain delivery cannot
   be treated as a complete day.
2. Prepare close and attest to the pause. The packet records the actual source
   capture time and business date in the configured warehouse timezone, previous
   signed cutoff or verified opening, stock equation, exact serials, movement
   coverage, shipment-document rechecks and carry-forward custody. It cannot
   reconstruct arbitrary historical midnight balances from a current snapshot.
3. Review every exception. Correct source/local postings through their authorized
   workflow, then capture and prepare again. A resolved count note does not change
   stock. A missing receipt and missing shipment do not cancel one another merely
   because their quantities net to zero.
4. For a legitimate source correction, approve the actual count adjustment
   separately. In Daily close, propose a correspondence using exact source IDs,
   existing adjustment ledger IDs, reason and supporting reference. Quantities,
   directions, units and serials must match; metadata-only corrections create no
   quantity movement. Shipment gaps must resolve through shipment documents and
   fulfillment release, not a balancing adjustment. Cancel an incorrect proposed
   correspondence rather than editing old stock history.
5. Incomplete source/opening/delivery/document evidence blocks all sign-off.
   Other accepted exceptions require an active named owner, due time, reference
   and explicit acceptance. A different supervisor/admin reviews and signs; that
   signer must not have prepared the packet or proposed its applied correspondences.
6. Sign-off rechecks material evidence. If it changed, prepare and review again.
   An unchanged healthy poll can advance sequence/time without changing the frozen
   boundary. Download the retained JSON packet and follow the company's archival
   procedure. **Signed with exceptions** remains different from **balanced**.

The pause is an operator attestation, not an external VISUAL lock. Signature
certifies the captured boundary and follow-up validation, not perpetual equality
after work resumes. Late/backdated movement or document changes flag affected
earlier closes. Superseding retains the old packet; later dependent closes require
current-period corrections rather than silent rewrites.

A legacy opening that retained only movement fingerprints has insufficient proof
for the new close. Upgrading software or receiving one modern delta does not
reconstruct missing baseline detail. The close stays incomplete until a separately
verified opening/rebaseline procedure establishes it; no endpoint silently clears
the old stream or invents opening correspondence. Production recovery and retention
procedures must be rehearsed before rollout.


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
- `fulfillment/worker.py`: complete order refresh and recorded planning attempts every 60 seconds.
- `fulfillment/order_book.py`, `planning.py`: durable order facts, triggers, run evidence and explanations.
- `fulfillment/workflow_requests.py`: Sales intake, exact binding and independently approved operations.
- `fulfillment/daily_close.py`: frozen evidence packets, two-way coverage, correspondences and sign-off.
- `fulfillment/inventory_connector.py`: separate read-only VISUAL capture and durable inbound inventory delivery.
- `operator-web/`: typed React client, scanner workflow, package barcode printing,
  manifest and asset-only service worker.
- `scripts/worldship_uploader.py`: authenticated Windows-side shipment evidence ingestion.
- `Dockerfile.fulfillment` / `docker-compose.fulfillment.yml`: dedicated API, allocation worker, PostgreSQL and HTTPS proxy.

Importing the new application does not initialize or migrate databases, start
the legacy scheduler, or write ERP records. Database setup is an explicit CLI command.
Schema version 5 uses app-owned relational tables for users, sessions, claims,
requests, events, labels and settings, plus versioned fulfillment JSON documents
and inventory stock, supplies, allocations, ledger, inbox, locks and reconciliations.
It adds order mirror, planning triggers/runs, sales/approval requests, source
movement versions and daily-close records. Explicit initialization migrates versions
1–4 without discarding existing records or enabling live operation.
Claims have database-enforced unique resource keys. All mutations share a
transactional mutex row, with savepoints for failed actions; this deliberately
serializes warehouse commands across workers. Rejections and successful commands
are audited. Higher-volume operation can later partition locking by site without
changing the public contract.

There are three modes:

| Mode | ERP source | Operator changes |
|---|---|---|
| `demo` | Synthetic fixtures | Training database only |
| `shadow` (default) | Read-only live VISUAL | Shipping/planning execution disabled; configuration, intake and evidence preparation remain available |
| `live` | Read-only live VISUAL | Shipping/planning require recorded cutover activation; source health and approvals remain enforced |

PostgreSQL is required for shadow/live. SQLite is supported only for tests and
explicit training. Each service instance is configured for exactly one VISUAL site.
Use a dedicated SQL Server login with SELECT permissions only.

## Deployment and first accounts

1. Copy `.env.fulfillment.example` to `.env.fulfillment`. Supply a dedicated
   PostgreSQL password/URL, VISUAL read connection, site, HTTPS origin,
   and separate random integration tokens of at least 32 characters: LABEL,
   VALIDATE, INVENTORY, and ORDER. `FULFILLMENT_ORDER_TOKEN` is only for existing
   order-upsert callbacks; it cannot receive inventory or post labels.
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

4. Add users with `--role operator`, `--role sales`, `--role supervisor` or
   `--role admin`, or manage
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

For an existing Dispatch version-1 through version-4 database, back it up, stop API/worker processes,
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
- Existing release policy logic runs in enforced mode. With local inventory enabled,
  Dispatch stock and commitments supply prescribed quantities/serials; the fresh
  order mirror supplies demand, and live ERP checks guard checkout and release.
  Active work is subtracted from supply.
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
- Inventory token: inventory receipt, work-order, batch and health feeds only.
- Order token: `POST /api/v1/integrations/orders` only.

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
python -m pytest tests -q
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

Inventory extension validation on **2026-10-20**: the full backend regression
passed 544 tests plus 11 subtests, with 12 skipped tests; PostgreSQL-specific
behavior was exercised separately. A disposable PostgreSQL 17.11 run passed 140
tests. After the final serial-return recovery change, the focused SQLite
source-correction and synchronization suites passed 23 tests, and the final
PostgreSQL recovery/health regression passed 28 tests. The disposable database
container was removed after validation. The read-only
VISUAL source adapter's 43 synthetic contract tests passed. Frontend type checking,
production build and all 14 tests passed. A fresh browser recheck could not reach
the local server because browser network access was blocked; the earlier browser
rehearsals above remain separate evidence, not a new visual check of every change.
The complete source adapter was not executed against live VISUAL, and production
deployment, source-load acceptance and cutover remain outstanding.


Operations extension validation on **2026-10-21**: the final full backend suite
passed **661 tests plus 11 subtests**, with 12 PostgreSQL-dependent tests skipped
in that SQLite run. The final disposable PostgreSQL 17 regression passed **135
tests**, covering inventory, planning, requests, daily close, audit, live order
rechecks and schema-4-to-5 preservation. All **29 frontend tests**, TypeScript
checking and the production Vite build passed. The temporary preview server and
PostgreSQL container were stopped/removed after validation. Existing legacy
`datetime.utcnow()` deprecation warnings remain.

The new workflow forms were exercised through functional component tests, including
independent approval, exact revisions, uncertain-request recovery, full sales
addresses and daily-close controls. The browser could not reach the temporary
loopback preview, so this turn does not claim a new visual/browser rehearsal. The
production Docker image was not rebuilt in this turn. No production database or
ERP record was changed, and production activation remains pending the scope,
modern opening capture, source-load, device and test-company acceptance gates
described above.
