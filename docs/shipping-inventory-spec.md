# Dispatch-owned shipping inventory and allocation

Specification date: **2026-10-21**. Product: the independent Dispatch app in
`fulfillment/` and `operator-web/`. Scope: one configured VISUAL site and one
shipping warehouse/location, with Dispatch managing inventory availability,
customer commitments, future supply and shipping execution.

The inventory and order-to-close operating workflows are implemented in schema
**v5**. Production deployment and cutover have not been performed. The accepted
warehouse is **SHIPPING**; the actual VISUAL site and single location remain
explicit configuration. An expired Sales hold stays reserved and is flagged for
review until an authorized person changes it.

| Area | Implemented now | Production setup or optional work remaining |
|---|---|---|
| Inventory ownership | Schema **v5**, local stock ledger, one configured boundary, disabled-by-default activation; additive migration from versions 1–4 | Confirm site and location within SHIPPING; reconcile and import opening stock |
| Receiving | Supervisor/admin receipt form, authenticated POST and read-only VISUAL connector with durable retries | Validate the configured source boundary and classifications in a test company; deploy one connector |
| Future supply | Full work-order identities, source revisions, priority-based partial promotion and shortage visibility | Verify the site's production and transfer procedures, including receipts without work-order lineage |
| Customer commitments | Persistent customer/order/line promises, holds, review dates, priority, transfers and protected accumulation; separate Sales submission and independent approval | Assign human reviewers and customer-reference ownership; expiry never frees a hold |
| Sales orders | Sales drafts awaiting a VISUAL order, exact binding, durable order mirror, idempotent order-upsert POST and complete periodic capture | Configure a separate ORDER_TOKEN for callbacks; complete the first full mirror capture before live local-stock checkout; commercial order creation remains in VISUAL |
| Planning runs | Durable triggers and attempts, retained inputs/decisions/changes/errors, Operations order board and run history | Measure complete-capture cost and choose worker cadence; callbacks queue work, while ordinary ready suggestions remain tentative until checkout |
| Fulfillment | Local stock prescriptions and locks, existing pick/pack checks, one stock debit on verified shipment release | Rehearse against actual order, packlist, label and scanner behavior |
| Reconciliation | Complete source captures, quantity/serial comparison, retained comparison reports, source health and stock quarantine | Rehearse capture load, opening import, outbox backup and discrepancy recovery before activation |
| Daily close | Retained movement versions/tombstones, two-way coverage, live shipment-document rechecks, frozen packets, exception owners, correction correspondences and independent sign-off | Rehearse a coordinated warehouse pause and modern opening capture; incomplete legacy opening evidence cannot be signed as balanced |
| Approval | Draft/submitted/approved/rejected/cancelled requests; separate active human reviewer; atomic allocation/transfer/hold/adjustment execution and admin-reviewed early release | Assign reviewers; choose document-review policy, which remains optional by default; staff notifications are not automatic |
| ERP posting | Existing read-only ERP validation and manual/supported packlist process retained | No automatic ERP writer or production connector has been deployed by this change |

Local inventory views and customer-only commitment changes do not require a
live ERP stock query. Manual-source receiving is also independent; a connected
VISUAL source exclusively owns receipts and requires successful source captures.
**Order-demand planning, order checks and final shipment
release still require VISUAL.** Local inventory ownership does not enable the
order queue or shipping to operate through an ERP outage.

For the implemented operating contract and acceptance scenarios, see
[section 17](#17-order-intake-planning-approval-and-daily-close).
For existing detailed contracts, see sections 3–5 (ownership/data), 6–7 (receiving/API),
8–10 (allocations/fulfillment), 11 (reconciliation), and 14–16
(cutover/acceptance/delivery stages).

## 1. Decision and intended operating model

Dispatch becomes the operational record for the stock Shipping can allocate and
the reason each unit is available, promised, held, picked or shipped. VISUAL
remains the record for manufacturing, customer orders, inventory accounting and
posted shipping documents. Allocation decisions no longer depend on re-reading
VISUAL shelf balances on every queue refresh.

A production or integration process reports a completed movement into Shipping
with an authenticated POST. Dispatch records that receipt once, immediately
updates its own inventory, and applies existing customer commitments. Work orders
can be promised before completion, but their remaining output is displayed as
future supply and never counted as stock on hand.

Inside Sales can request a quantity for a known customer before a sales order
exists. An independently approved request creates the reservation with its reason
and owner; a review date is recommended
for held promises and is optional in the current API. Later it can
be assigned to a specific sales-order line belonging to that customer. Shipping
can hold that reservation, release its hold, or deliberately free the stock.
Those are separate actions with separate audit events.

The physical inventory boundary is explicit: goods enter Dispatch when received
into its configured shipping location and leave when the shipment is released
or an authorized adjustment/return removes them. An ERP production completion
outside that location is future supply evidence, not a Dispatch receipt.

The implemented shipment boundary retains the existing VISUAL packlist reconciliation and
WorldShip checks. Post the packlist in VISUAL through the existing supported
process, verify it in Dispatch, then release the shipment and debit Dispatch
stock in the same local transaction. This supports decoupled allocation without
inventing an unverified ERP write interface.

## 2. Repository baseline and delivery boundaries

### Existing application before this extension

The existing independent Dispatch service already provides:

- PostgreSQL persistence, authenticated roles, versioned commands, idempotency
  receipts, audit events and database-enforced resource claims.
- Customer release policies, hard order/document checks, manual order holds,
  early-release exceptions and a reservation maintenance worker.
- Order checkout, prescribed serial/quantity picking, cart handoff, package
  rescanning, exact VISUAL packlist comparison, label validation and release.
- Scanned putaway before cancellation frees picked inventory.
- Independent serialized inventory audits. Those audits record observations;
  they do not currently constitute an inventory ledger or adjust balances.

The missing separation is in `VisualERP.available()` and `check_pick()`: the
queue and pick validation derive usable supply from current VISUAL stock and
serial queries. Existing queued reservations are stored in settings, and active
fulfillments subtract their prescribed quantities from the source inventory.
The schema-v3 database has no general receiving ledger, future-supply registry
or customer stock commitment engine.

Baseline evidence: [Dispatch implementation](dispatch-implementation.md),
`fulfillment/model.py`, `fulfillment/service.py`, `fulfillment/erp.py`,
`fulfillment/admin.py`, `fulfillment/rules/release_gate.py` and
`operator-web/src/main.tsx`. Existing SQL already illustrates composite work-order
keys in `sql/alloc_supply.sql` and `fulfillment/sql/audit_serialized_inventory.sql`.
This specification does not claim that the installed VISUAL toolkit, receipt
hooks or ERP write operations have been validated.

### Implemented scope

The implementation contract for this change is:

- Explicit additive migration from schema versions 1–4 to **schema v5**, preserving
  existing work, accounts, claims and history.
- App-owned stock, expected supplies, allocations, inventory events, incoming
  event receipts, fulfillment locks and reconciliation reports.
- A disabled-by-default local-inventory mode with one configured
  site/warehouse/location; explicit setup and reconciliation before activation.
- Inventory workspace and authenticated receipt, work-order, allocation, hold,
  adjustment and reconciliation commands.
- Idempotent machine receipt/work-order/reconciliation POST endpoints with a
  dedicated inventory integration credential.
- A read-only VISUAL source adapter and durable connector, atomic opening/delta
  batches, complete source identity auditing, discrepancy review and health fences.
- Priority/need-by/request-reference metadata, promise risk explanations and
  atomic binding or transfer of unconsumed, unlocked customer commitments.
- Local inventory as the allocation and picking source when enabled, while
  retaining ERP order, destination, document, packlist and label checks.
- Local fulfillment locks and inventory consumption at final release; existing
  cancellation/putaway safeguards remain applicable.
- Durable VISUAL order mirror and exact sales-request binding, separate order
  integration token, Sales role, formal approval inbox and linked outcome history.
- Durable planning triggers, inputs, results, failures and allocation changes,
  with a shared order-decision board and checkout decision provenance.
- Modern complete movement evidence, daily-close packets, reviewed adjustment
  correspondence, explicit exception acceptance and independent sign-off.

The foundation also preserves the existing customer accumulation engine: its
worker can turn protected local-stock plans into durable, policy-managed
allocations. This is distinct from a new general-purpose rule optimizer. Future
receipts promote commitments by priority, need-by date, creation time and ID;
existing picked work remains protected.

An allocation attached to a sales order checks the current ERP customer, site,
part, stocking unit and optional order line before saving. Customer-only manual
promises still rely on an accurately entered customer ID. On inventory activation,
a verified opening count can adopt returned serials from terminal historical
fulfillments without retaining their old claims as active stock locks. Shipment
history remains intact. Changing the warehouse boundary and enabling ownership
cannot be combined in a single command; save the disabled boundary and reconcile
that exact boundary first.

The following are required later deployment/integration work, not implied by
adding endpoints or the new screens:

- Deployment of the implemented connector, test-company acceptance of the source
  queries and complete opening import, and measured production reconciliation.
- Automatic writeback of adjustments, receipts or shipping documents to VISUAL.
- A guaranteed pre-save hook preventing all direct VISUAL shipping bypasses.
- Shipping during a VISUAL outage. Existing final-release validation remains
  dependent on live ERP and label evidence.
- Automatic staff notifications, additional granular role policies and a new
  general-purpose allocation optimizer beyond the published release rules.
- Internal multi-bin warehouse management. The first contract uses one shipping
  location; carts, stations and packages remain fulfillment custody.

Sections below define the full target and production acceptance criteria.
Implementation tests and the accompanying change report determine which criteria
have been demonstrated. A schema upgrade or successful demo is not production
cutover approval.

## 3. Ownership and synchronization contract

| Subject | Authoritative system | Dispatch responsibility | Synchronization direction |
|---|---|---|---|
| Part identity, stocking unit, serialized flag | VISUAL master data | Validate references and retain the version used | VISUAL to Dispatch |
| Customer, order, exact line and destination | VISUAL | Mirror demand; retain line identity; recheck before release | VISUAL to Dispatch |
| Work-order existence, product, status and expected completion | VISUAL | Maintain a referenced future-supply record | VISUAL to Dispatch |
| Shipping operational on-hand | Dispatch after cutover | Ledger, balance, custody and availability | Receipt events into Dispatch; compare with VISUAL |
| Production/WIP and other warehouses | VISUAL | Visibility only; cannot become shipping stock by allocation | VISUAL to Dispatch |
| Customer/order commitments and reservation priority | Dispatch | Allocate, hold, release, fulfill and explain | Internal; optional reference export later |
| Financial inventory quantities, cost and valuation | VISUAL | Preserve source/posting references; expose differences | Reconciliation; supported posting process |
| Shipment plan, contents, packages and scans | Dispatch | Control fulfillment execution | Manifest to existing ERP entry process |
| Posted packlist and ERP shipped quantity | VISUAL | Verify exact equality before release | VISUAL to Dispatch |
| Tracking/void evidence | Carrier export plus existing ERP evidence | Verify every package and retain evidence | WorldShip/VISUAL to Dispatch |
| Observed audit discrepancies | Dispatch audit | Create investigation evidence; approved adjustment is separate | Internal; ERP correction through supported process |

The two systems intentionally keep separate records, but cannot both independently
decide who owns the same shipping stock. After cutover, all shipping allocations
and receipt entry must pass through Dispatch's agreed boundary. Any direct
VISUAL movement or shipment affecting that boundary appears as a reconciliation
exception, never as an implicit overwrite of Dispatch balances.

## 4. Vocabulary and invariants

**On hand** is the physically accountable quantity received into Dispatch less
posted local removals. It includes stock in a cart or package until release.
**Available** is the uncommitted, unheld, unlocked portion of on-hand stock.
**Allocation** is a claim for a customer, an order or an exact order line. **Hold** prevents
use or shipment while keeping that claim. **Future allocation** is a promise
against a named work order, not physical inventory.

The target balance presentation uses mutually exclusive buckets:

```text
on_hand = free + reserved + held + in_fulfillment + quarantined
available_to_other_customers = free
customer_usable = free + that_customer's_unheld_reserved_stock
```

Quantity held by an order is classified as held, not counted a second time in
reserved. Stock already claimed by a fulfillment is in_fulfillment, regardless
of the original reservation. The UI may show a combined committed total, but
must label its members and avoid subtracting the same units twice. The foundation
exposes `quantity`, `committed` and `available` on each stock row; held status is
visible on its allocations. Separate held/in-fulfillment/quarantine balance
columns are the target presentation, not additional quantities to add to these
existing totals.

For an expected supply record:

```text
unreceived_supply = max(0, expected_total - accepted_receipts)
usable_future_supply = unreceived_supply if work_order_status == open else 0
future_free = max(0, usable_future_supply - remaining_future_allocations)
promise_shortfall = max(0, remaining_future_allocations - usable_future_supply)
```

The connector must normalize source quantities to this definition. A VISUAL
remaining quantity cannot be treated as an expected total and have receipts
subtracted again. Over-allocation after a work-order reduction is an exception;
it is not resolved by silently changing the customer promise.

Non-negotiable invariants:

1. No command produces negative stock or commits more stock than is physically
   available at that transaction boundary.
2. Future supply never increases on-hand or authorizes a pick.
3. A serial identity includes site and part. At most one active on-hand unit and
   one active allocation/fulfillment lock may own that identity.
4. Serialized quantities are integers matching the count of distinct serials;
   every serialized unit contributes exactly one stocking unit.
5. Nonserialized quantities use exact decimal arithmetic, serialized as strings;
   existing Dispatch supports at most eight decimal places. No binary floating
   arithmetic or implicit selling-unit conversion is allowed.
6. Order-line identities never collapse because two lines have the same SKU.
7. A customer allocation is usable only by that customer. An order allocation is
   usable only by that order, and a line-specific one only by its exact line. Destination restrictions, when present,
   narrow the claim further.
8. A reservation or hold does not alter the financial owner of goods. It records
   a promise, not consigned inventory or a sale.
9. Arrival promotes future commitments once. A promoted quantity cannot remain
   simultaneously available for another future commitment.
10. Read endpoints cannot receive, adjust, allocate, release or reconcile stock
    by side effect. Workers and explicit commands perform mutations.
11. Idempotency response, source-event identity, ledger entries, stock changes,
    allocation changes and fulfillment locks commit atomically.
12. A failed receipt or any failed line of a multi-line event leaves no partial
    balance or allocation update. Large source batches may be split into
    independently identified receipt-line events.
13. ERP reconciliation never overwrites inventory, reservations or fulfilled
    history. A correction is an explicit, reasoned transaction.
14. Once stock has been picked, cancelling or expiring a promise cannot free it
    until the existing scanned putaway workflow completes.
15. Credit, order status, destination, document and shipment-validation blocks
    remain effective. Allocation priority or a sales expedite cannot bypass them.

## 5. Logical data model

These are domain records. The implementation may retain versioned JSON documents
inside relational tables, consistent with existing fulfillments, provided unique
keys, locks and required invariants are enforced transactionally.

| Record | Identity and required facts | Purpose |
|---|---|---|
| Inventory configuration | Revision, enabled flag, site, VISUAL warehouse, VISUAL location | Defines the one operational boundary |
| Stock | Site/part/UOM/serial or nonserialized bucket; quantity; version | Materialized local balance |
| Inventory ledger event | Immutable event ID, type, signed quantity, stock identity, actor/source, event time, recorded time, reason, references | Explain and rebuild balances |
| Incoming event receipt | Source identity, source event ID, canonical content digest, resulting record IDs, response | Deduplicate independent of HTTP retries |
| Expected supply | Stable external work-order key, part, UOM, normalized expected quantity, accepted receipt quantity, due date, status, source revision | Promise unreceived supply |
| Allocation | ID/version, stock or supply reference, customer, optional order/line, quantity, fulfilled/released amounts, priority, hold, reason, review/expiry time | Own a promise across arrivals and shipping |
| Promoted allocation | Child stock commitment, parent future-allocation ID, receipt ID | Preserve lineage for partial arrival |
| Fulfillment stock lock | Fulfillment, exact line, stock key, quantity, originating allocation | Exclude checked-out stock from other work |
| Reconciliation report | ID, source as-of/watermark, app version/watermark, normalized source, comparison, exceptions, actor | Inspect discrepancies without replacing stock |
| Sales request | Requester, customer/site, PO/reference, destination, requested lines, revision, status and exact VISUAL binding | Intake that cannot ship before its commercial order exists |
| Request and approval | Requester, exact terms/revision, status/history, submitted digest, reviewer, evidence and resulting allocation/control | Independent review; pending requests do not reserve stock |
| Order mirror | Site/order identity, full source facts and line quantities, source revision/digest, observation and conflicts | Durable operating copy with explicit freshness |
| Planning trigger and run | Trigger actor/time, input revisions, start/end/status, decisions, allocation changes and errors | Reproduce why orders shipped or waited |
| Source movement | Transaction ID, detailed lineage, versions/hashes/tombstones and effective/recorded times | Prove source changes without replaying old receipts |
| Daily close and correspondence | Frozen source/ledger/work packet, coverage, exceptions, material digest, reviewers and correction links | Review and sign the captured warehouse boundary |

Ledger types include opening balance, production/transfer receipt, accepted
return, positive/negative adjustment, shipment release, and reversal of a prior
adjustment/receipt. Allocation creation, hold, unhold, reassignment and release
are audit events with no on-hand delta. Reversals reference the original event;
deleting history is not an available repair.

Work-order identifiers must retain the configured site and the complete source
key: type, base, lot, split and sub. A display value such as `WO 123/1/0` alone
is not a unique integration key. Where phase one accepts an opaque `supply_id`,
the connector must generate it from the full composite key and retain that
mapping durably. Do not parse customer IDs, part IDs or work-order IDs by splitting
on punctuation; existing Dispatch uses length-delimited identities.

The first shipping location is configured explicitly. Do not default its value
from an existing shelf query or rename live VISUAL bins. The connector must report
exactly that site/warehouse/location. Stock elsewhere is outside the boundary.

## 6. Receipt ingestion and upstream integration

### Normal production-to-Shipping receipt

1. Production completes or moves an item through its supported VISUAL process.
2. The goods physically arrive in the configured shipping location.
3. A connector or receiving station emits a stable, positive receipt event with
   source transaction/line identity and, when applicable, work-order identity.
4. Dispatch validates the inventory boundary, part/UOM, serial cardinality,
   event identity and supplied work-order link.
5. In one transaction, Dispatch records the event, increments stock, records the
   work-order receipt quantity and promotes eligible future commitments.
6. The response returns the accepted event and resulting stock/commitment facts.
   A worker refreshes the eligible shipping queue using the local ledger.
7. Source transaction evidence is subsequently compared in reconciliation.

Expected response behavior: first accepted event succeeds; replay of identical
content succeeds with the original result; reuse of its source-event identity
with different quantity, serial, source or destination returns a conflict. An
HTTP client retry key and a source business event ID solve different problems
and both are required.

For a non-opening connector receipt containing any same-part serial with prior
inventory history, or shared by distinct receipt events in the same batch,
Dispatch accepts the event for review rather than receiving
stock. It validates unique serials and quantity first, stores
`accepted: true, review_required: true, quantity, items: []` with part/UOM/source
references, and emits a `receipt_review` issue. The entire receipt stays in review,
including any new serials mixed with known ones. No stock, work-order received
quantity or promise promotion is created; the batch still records reconciliation
so an inspected return/rework can use the count-adjustment procedure. Replay is
a no-op. All receipt events sharing a serial within one batch stay in review;
numeric transaction IDs are not used to guess receive/ship/return chronology.
Duplicate serials within one event and quantity mismatches still reject the
complete batch.

### Implemented source adapter and source ownership

`fulfillment/visual_inventory_source.py` reads VISUAL; `inventory_connector.py`
owns a durable SQLite checkpoint/outbox; `inventory_sync.py` applies complete
batches to Dispatch. The connector uses a SELECT-only SQL login and HTTPS with
the dedicated inventory token. It never writes ERP inventory or orders.

Three inventory-source states are distinct from the app's demo/shadow/live modes:

| Inventory source state | Receipt and supply owner | Availability |
|---|---|---|
| Local inventory disabled | Existing ERP-stock adapter | Existing VISUAL stock and order checks |
| Local inventory enabled, no connector stream | Authorized staff or a separately governed machine producer | Local ledger; manual receipts/supply permitted |
| Connector stream established | One persisted VISUAL stream and its original outbox | Local ledger, with source health and reconciliation fences |

Once a connector stream is established, standalone manual/machine receipts and
manual/standalone work-order updates are rejected. Correct the source and let
the connector deliver it; do not add a second receipt producer. Reasoned local
adjustments remain explicit corrections subject to stock/lock guards and a
subsequent source comparison. Losing the connector state does not authorize a
new opening import. Back up both the Dispatch database and connector outbox.

Every capture reads the exact site/warehouse/location in one SQLAlchemy
`SERIALIZABLE` transaction, with no `NOLOCK`. Scope, part-location balances,
trace balances, complete movement history, work orders and a source UTC
observation time are read before that transaction ends. Each result is bounded
by `max_rows` (default 100,000, CLI `--max-source-rows`); exceeding a bound rejects the entire capture.
There is no truncation, maximum-transaction-ID cursor or timestamp-only window.
Test the full capture's latency and locking at the actual boundary before live use.

The source contract is grounded in the installed schema and repository queries:

| Source fact | Implemented interpretation |
|---|---|
| `INVENTORY_TRANS.TRANSACTION_ID` | Stable primary identity; receipt event `IT:<id>`. `ROWID` is not the business identity or a commit-order guarantee. |
| `TYPE`, `CLASS`, `QTY` | Header sign is positive for `I`, negative for `O`, preserving any negative source quantity as a reversal. Stocking units come from `PART_SITE_VIEW.STOCK_UM`. |
| Receipt source documents | Full `WORKORDER_*` identity must resolve to a same-site/part manufacturing work order, or `PURC_ORDER_ID`/`PURC_ORDER_LINE_NO` to a same-site/part `PURCHASE_ORDER` + `PURC_ORDER_LINE`. Customer references exclude automatic receipt handling. |
| Posted/void evidence | There is no posted/void flag in this table. `POSTING_CANDIDATE` concerns accounting and is not a filter for committed inventory. Reads use committed transactional source evidence. |
| `PART_LOCATION` | Complete part/warehouse/location quantity; site is resolved through `WAREHOUSE.SITE_ID`. Positive held, locked, in-transit or unavailable source stock rejects capture because automatic quality-hold mapping is not implemented. |
| `TRACE_INV_TRANS` | `(PART_ID, TRACE_ID, TRANSACTION_ID)` key; its `QTY` is already signed. Trace IDs, not optional `TRACE.SERIAL_ID`, identify serialized stock. |
| `TRACE_PROFILE` | Presence identifies a traced part. Its `SERIAL` flag alone does not establish per-unit tracing; actual trace quantities must validate as unit quantities. |
| `WORK_ORDER` | Identity is compact JSON `[TYPE,BASE_ID,LOT_ID,SPLIT_ID,SUB_ID]`, retained in full. Missing/overlong identities fail clearly; there is no truncation or guessed alias. |

Current serial balances are signed trace sums at the boundary. Every positive
serial must have quantity one; serial counts and aggregate movement balances
must match `PART_LOCATION` quantities. Unknown master mappings, negative stock,
fractional/mixed trace quantities or unexplained ledger differences fail the
capture. Component quantities remain decimal strings, never integer casts.

Receipt classification is deliberately narrow. A positive `TYPE=I, CLASS=R`
movement without a transfer reference requires exactly one verified source:
a full five-key `TYPE=W` manufacturing work order matching site and part, or a
purchase-order header/line matching site and part. Any `CUST_ORDER_ID` or
`CUST_ORDER_LINE_NO` excludes automatic receipt handling; missing, unmatched or
mixed manufacturing/purchase provenance goes to review. A positive `I/A` movement is
a receipt only with a reciprocal `O/A` transfer partner of the same part and
quantity, outside the configured boundary, with neither side in transit. A
negative `O/I` movement with a customer order is a shipment; Dispatch does not
debit it again. Other movements, including unmatched adjustments, transfer-outs,
returns, reversals and corrections, become review issues and never automatic
stock adjustments. A complete comparison still exposes their stock effect.

All site production work orders are captured, including terminal records needed
to retain promise history. Only `F`/`R` work orders with `SUB_ID=0` and no existing
`DEMAND_SUPPLY_LINK` tie offer free future capacity. Valid split IDs remain part
of the key. Remaining manufacturing quantity is `max(0, DESIRED_QTY-RECEIVED_QTY)`;
other orders offer zero free capacity, and `X` maps to cancelled. Due date uses
scheduled finish, then desired want date. `RECEIVED_QTY` means manufacturing
completion anywhere; it is not proof of physical arrival into Shipping.

A movement links a work order only when all five explicit source fields exist.
Transfers without those fields enter general stock and can surface an
`unlinked_receipt` issue when matching future promises exist. Never infer a work
order solely from part or quantity. A supervisor can use `link-receipt` after
verifying production evidence; it promotes existing free receipt stock without
receiving it again (section 7).

### Durable batch contract and opening boundary

`POST /api/v1/integrations/inventory/batches` accepts the source boundary,
`source: "visual"`, `event_id`, `stream_id`, monotonically contiguous `sequence`,
`kind: "opening" | "delta"`, `expected_stock_version`, `observed_at`,
`source_manifest`, complete `balances`, changed `work_orders`, new `receipts`,
review `issues`, version-2 `movement_evidence` and detailed `movement_changes`.
Movement evidence includes completeness, manifest/count and the previous source
sequence. Changes retain full transaction lineage, edits and deletions. The
producer stops before delivery above 15 MiB; the batch HTTP route accepts up to
16 MiB, while ordinary commands remain limited to 2 MiB. Work-order rows carry `remaining`; the receiver combines
that with locally received quantities rather than overwriting receipt progress.
Each batch applies receipts, supply revisions, movement evidence, issues and reconciliation in one
local transaction. It permits at most 5,000 receipt and 5,000 work-order events;
larger batches fail instead of applying partially.

Bootstrap requires disabled local inventory, an empty stock ledger, no active
fulfillment and no legacy queued reservations. It imports complete opening
balances once and records every source movement identity already represented by
that opening count. Historical receipts are not replayed into opening stock.
The outbox retains fingerprints for every observed movement, including deleted
identities. Every subsequent capture compares the complete set: late/lower IDs
are discovered, edits/deletions become issues, and a restored historical receipt
cannot silently become a new arrival. This identity set, sequence and stock
version form the cutoff evidence; a numeric high-water mark is insufficient.
The connector acknowledges `delivery_confirmed` after its durable checkpoint
advances. Missing confirmation prevents close sign-off. An uncertain delivery is
reported as `delivery_pending`; its exact body/key remain pending for recovery.

The producer reads Dispatch's stock version before source capture. The receiver
rejects a batch if local stock changed during capture, if its observation is over
120 seconds old (or over 60 seconds in the future), or if observation time does
not advance. The stock version includes boundary/balance identity and ledger
count. This fence avoids comparing different local stock moments without
holding a Dispatch transaction open during SQL Server reads.

### Delivery, corrections and offline behavior

- The source event ID must remain stable across retries, application restarts
  and re-exports. A newly generated UUID for every retry defeats deduplication.
- The connector persists its exact batch before HTTP delivery. Timeouts, 5xx,
  malformed responses and uncertain acknowledgements retain the pending body;
  the next poll retries it unchanged. Default polling is 60 seconds, configurable
  from 15 to 120 seconds. Inspect `--status` for a pending batch and safe error.
- If Dispatch commits but the response is lost, the connector resends the same
  source identity, idempotency key and content. It must not create a replacement
  event to force success.
- A newly issued event ID must not provide a way to receive the same serial twice.
  Nonserialized receipts additionally require durable source transaction/line
  uniqueness; different IDs with the same business transaction require review.
- Corrections and source edits are review issues. A reversal cannot remove
  stock already locked, shipped or committed without explicit resolution.
- A `capture_stale` rejection occurs before business writes, so the connector
  discards only that rejected envelope and recaptures from the accepted checkpoint.
  For another recorded HTTP 4xx rejection, correct the cause and run
  `--recapture-rejected`. This requires the receiver to still have the same
  accepted stream/sequence, retains a discard audit record and preserves the
  accepted identity map. It cannot discard an uncertain timeout/5xx delivery.
- The connector must reject unknown parts and validate stocking units and
  serialization against the approved master-data mapping. The foundation checks
  identifier/quantity shape, its configured boundary, existing local part/unit/
  serialization consistency and any referenced work-order match; it does not
  independently look up a newly typed part in VISUAL before receiving it.
  Wrong sites, negative or zero receipts, inconsistent units, fractional
  serialized quantities and duplicate serials fail atomically. Unknown source
  data belongs in the connector's error queue rather than a guessed stock row.
- Inventory views and customer-only commitment changes remain available while
  VISUAL is unavailable. Manual-source receiving can continue under its receiving
  procedure; connector-owned receiving waits for a valid source capture. Order
  binding and the eligible order queue still need
  live ERP demand and hard-hold facts; it is not an offline order planner.
  Final shipment release fails closed when required ERP evidence is unavailable.
- In manual-source mode, a receipt is physical confirmation, not proof VISUAL posted the
  corresponding inventory. Display and reconcile this gap; require the supported
  ERP posting before its eventual packlist can validate. A future policy can
  quarantine unposted receipts until confirmation, but must not claim this is
  already automatic without implementation.

## 7. Application API surface

The following paths are the implemented application contract. Browser commands
use the existing authenticated session, CSRF header and `Idempotency-Key`.
Inventory machine commands use a dedicated inventory bearer credential; order
upserts use a separate `FULFILLMENT_ORDER_TOKEN`. Both are separate from label
and VISUAL-validation credentials. Mutations carry a revision
where the target aggregate can be edited concurrently.

| Method and path | Operation |
|---|---|
| `GET /api/v1/inventory` | Configuration, balance summary, expected supplies, allocations, ledger and reconciliation history |
| `GET /api/v1/inventory/demand` | Current ERP order manifests for choosing customer/order/line; no ERP inventory read |
| `POST /api/v1/inventory/configure` | Set/review the single boundary; enable local inventory only after activation checks |
| `POST /api/v1/inventory/receive` | Record a receipt from the receiving workspace |
| `POST /api/v1/inventory/supply` | Create/update a referenced future-supply record |
| `POST /api/v1/inventory/allocate` | Reserve available on-hand stock or named future supply |
| `POST /api/v1/inventory/allocation-update` | Hold, unhold or release a commitment using explicit actions |
| `POST /api/v1/inventory/allocation-transfer` | Bind/split/reassign an unlocked commitment while preserving ownership lineage |
| `POST /api/v1/inventory/adjust` | Reasoned supervisor correction subject to availability/lock constraints |
| `POST /api/v1/inventory/reconcile` | Save and compare a source snapshot; do not overwrite balances |
| `POST /api/v1/inventory/link-receipt` | Attach existing free receipt stock to a work order and promote promises without a second receipt |
| `POST /api/v1/inventory/sync-ack` | Record a reasoned resolution of a sync issue; does not clear a stock quarantine |
| `POST /api/v1/integrations/inventory/receipts` | Idempotent upstream receipt ingestion |
| `POST /api/v1/integrations/inventory/work-orders` | Upstream future-supply updates |
| `POST /api/v1/integrations/inventory/reconcile` | Source snapshot delivery for reconciliation |
| `GET /api/v1/integrations/inventory/state` | Current stream/sequence, stock version, scope and health for the connector |
| `POST /api/v1/integrations/inventory/batches` | Atomic opening/delta receipt, supply and complete snapshot delivery |
| `POST /api/v1/integrations/inventory/health` | Sequence-fenced capture/delivery failure report |
| `POST /api/v1/integrations/orders` | Idempotently record an existing VISUAL order and monotonic source revision; never create an ERP order |
| `GET /api/v1/planning/board` | All mirrored orders, current explanation, owner, next action and source/decision age |
| `GET /api/v1/planning/runs` and `/runs/{id}` | Retained planning summaries and complete attempt evidence |
| `POST /api/v1/planning/run` | Supervisor/admin planning attempt; failed runs remain inspectable |
| `GET /api/v1/workflow` | Sales intake, approval requests, histories and resulting commitment progress |
| `POST /api/v1/workflow/sales-{create,update,submit,cancel,bind}` | Draft intake and supervised exact VISUAL order binding |
| `POST /api/v1/workflow/request-{create,update,submit,cancel,approve,reject}` | Revision-bound request lifecycle; approval executes its one reviewed operation atomically |
| `GET /api/v1/daily-close` and `/{id}/packet` | Close history, retained JSON packet and movement correspondences |
| `POST /api/v1/daily-close/{prepare,exception,sign,supersede}` | Capture evidence, assign exceptions, independently sign or replace a retained close |
| `POST /api/v1/daily-close/{correspondence,correspondence-cancel}` | Propose/cancel exact source-revision-to-adjustment evidence; never create stock |

Standalone receipt/work-order examples below apply before a connector stream is
established. Connected mode accepts those changes only through its batch owner.

`GET /api/v1/inventory` has the following top-level shape:

```json
{
  "config": {
    "enabled": false, "site": "TEST", "warehouse": "SHIPPING",
    "location": "DISPATCH", "version": 1
  },
  "balances": [],
  "supplies": [],
  "allocations": [],
  "ledger": [],
  "reconciliations": [],
  "sync": {"connected": false, "sequence": 0, "fresh": true, "issues": []},
  "as_of": "2026-10-20T15:30:00Z"
}
```

### Implemented request examples

Create or revise expected work-order supply through
`POST /api/v1/integrations/inventory/work-orders`:

```json
{
  "source": "visual", "event_id": "WO-DEMO-REV-1", "source_revision": 1,
  "site": "TEST", "warehouse": "SHIPPING", "location": "DISPATCH",
  "work_order": "[\"W\",\"WO-DEMO\",\"1\",\"0\",\"0\"]", "part": "DEMO-PART", "uom": "EA",
  "quantity": "10", "serialized": true, "status": "open",
  "due_date": "2026-10-23", "reason": "Production supply synchronized"
}
```

`quantity` is the expected total including quantity already received.
`source_revision` is a positive, monotonically increasing integer per work order;
the connector must durably preserve this sequence. Replaying the identical
`event_id` returns its recorded result. A different event with an older or
repeated revision conflicts. The response's `id` is the Dispatch supply ID; retain
it for receipt and allocation links. The server derives this canonical ID from
the configured site and `work_order`; a caller cannot provide a second arbitrary
ID for the same work order. The browser `supply` command uses
`id`/`version` on an update rather than the machine source revision protocol.
Manual edits preserve existing source identity/revision metadata, so they cannot
reset the machine update ordering fence.

Receive the first two units through
`POST /api/v1/integrations/inventory/receipts`:

```json
{
  "source": "visual", "event_id": "TEST-INV-78123-LINE-1",
  "site": "TEST", "warehouse": "SHIPPING", "location": "DISPATCH",
  "part": "DEMO-PART", "uom": "EA", "quantity": "2",
  "serials": ["DEMO-101", "DEMO-102"],
  "work_order_id": "SUPPLY-ID-FROM-RESPONSE",
  "reason": "Completed goods accepted into Shipping"
}
```

Replace `SUPPLY-ID-FROM-RESPONSE` with the actual returned `id`. Omit
`work_order_id` for general stock. Nonserialized receipts use an empty `serials`
list. The browser `receive` command accepts the same receipt shape with
`source: "manual"` for an operator-entered receipt. The response contains
`accepted`, `event_id`, `quantity` and `items`, where each item gives `stock_id`
and quantity. A receipt can promote previous promises without becoming free stock.

Allocate six future units through `POST /api/v1/inventory/allocate`:

```json
{
  "source": "work_order", "source_id": "SUPPLY-ID-FROM-RESPONSE",
  "part": "DEMO-PART", "uom": "EA", "quantity": "6",
  "customer": "DEMO-CUSTOMER-A", "order": "", "order_line": "",
  "hold": true, "review_at": "2026-10-27T16:00:00Z",
  "priority": 25, "need_by": "2026-10-26", "request_reference": "Inside Sales request DEMO-42",
  "reason": "Inside Sales requested stock for an upcoming customer order"
}
```

Here `source` selects the allocation pool, not the integration sender. Use
`source: "stock"` and omit `source_id` for an on-hand quantity; the engine selects
eligible stock rows deterministically. `hold` defaults to true. `order` is
optional; `order_line` requires an order. `review_at` is currently optional in
the API, although operations should provide it for every manual held promise.
Customer and order identifiers are checked for eligibility when planning demand;
the current manual allocation form does not independently query a customer
master before saving a typed customer identifier.

`priority` is an integer from 0 (highest) to 100, default 50. `need_by` is optional
`YYYY-MM-DD`; `request_reference` is at most 200 characters without control
characters. Allocate, update and transfer accept these fields.

`allocation-update` accepts `id`, current `version` and `reason`, plus metadata,
`hold` and/or `review_at`; use `status: "released"` to explicitly free the remaining
commitment. It refuses changes while that allocation has fulfillment locks.
`allocation-transfer` accepts `id`, `version`, `quantity`, `customer`, optional
`order`/`order_line`, metadata, hold/review fields and reason. It atomically moves
only unlocked, unconsumed quantity into a successor, returns `{allocation, source}`,
and preserves `transfer_from`, source history and receipt/parent lineage. The old
record becomes `transferred` when its remainder reaches zero. No intermediate
release makes transferred stock available to another customer. Explicit order
bindings validate live ERP customer, site, part/UOM and open line demand.

`link-receipt` takes `event_id`, `work_order_id`, the supply's current `version`
and `reason`. It requires a matching, previously unlinked VISUAL receipt and all
its stock to remain free and unconsumed. It increases the work-order received and
total counters equally, promotes the existing receipt items, retains a link audit
record and resolves its unlinked-receipt issue. It does not alter physical stock.
Review-only arrivals with prior serial history cannot use this command; they
require an inspected count adjustment and do not credit future production promises.
`sync-ack` takes issue `id` and reason; fresh source evidence alone clears stock
quarantines. Acknowledging an alert is not evidence that balances now agree.

`configure` takes current `version`, `enabled`, `site`, `warehouse`, `location`
and `reason`. Activation requires a matching stock fingerprint and source
observation no older than one hour, with no active fulfillment or legacy queued
reservation. Reconciliation evidence is bound to the configured boundary; changing
an empty boundary cannot reuse a match for its former site/location. Initial
setup keeps `enabled: false` while opening receipts are loaded. `adjust` takes
`part`, `uom`, signed nonzero `quantity`, `serials` and
`reason`; it cannot reduce stock below its protected quantity. Ordinarily it
applies to existing stock identities. A positive adjustment may introduce a new
part/serial only when a connected, healthy source's latest accepted reconciliation
shows a positive difference for that exact part/UOM/serial, the adjustment fits
that difference, and the local stock fingerprint has not changed since the
comparison. Otherwise a new identity requires a receipt. This narrow correction
path does not create a receipt or infer production provenance.

Reconciliation input uses `source`, `event_id`, the three boundary fields,
`observed_at` (a timezone-aware ISO timestamp) and a complete `balances` array.
Each balance is `{ "part": "...", "uom": "EA", "quantity": "2",
"serials": ["SERIAL-1", "SERIAL-2"] }`; use `serials: []` for components.
The endpoint compares the snapshot and records `matched` or `variance`, detailed
differences and the local stock fingerprint. It rejects snapshots older than
already accepted reconciliation evidence; it cannot independently prove that
the caller supplied every ERP row.

The initial inventory GET returns the latest 250 ledger entries and 50
reconciliation reports; those display limits do not delete database history.
Pagination/export of the complete inventory history is later UI work.

The running `/api/v1/openapi.json` and endpoint tests are the authoritative field
names for the implemented transport contract. No client should send
an undocumented field and assume the service persisted it.

API error behavior must distinguish:

| Error | Meaning and caller behavior |
|---|---|
| `400` | Invalid shape, identity, quantity or required reason; correct the request |
| `401` / `403` | Missing identity or insufficient scope; do not retry under another user's account |
| `404` | Referenced stock/supply/allocation/order unavailable |
| `409` | Stale version, changed idempotent content, duplicate business event, insufficient free quantity or active lock |
| `503` | Required source unavailable or configuration incomplete; retry when recoverable |

The response should identify conflicting stock or commitment references without
exposing credentials. The UI preserves a pending idempotency key after an
uncertain response and obtains the saved result before accepting a second action.

## 8. Future supply and allocation engine

### Future-supply updates

An expected supply records a specific existing work order, part, stocking unit,
expected total quantity, due date and source status. The integrated connector
validates work-order existence; a user-entered label alone is a planning
record until source validation occurs. Source refreshes use an explicit source
revision or event identity, with stale updates rejected or ignored according
to a documented rule. Receipt progress is app-owned and cannot be erased by
replaying an old work-order snapshot.

Delays update expected arrival dates and flag affected promises. A reduction or
cancellation that invalidates committed quantities retains the promises and
exposes their uncovered quantity. The snapshot provides per-allocation
`risks: [{code,message}]`, `at_risk`, `shortage`, `expected_arrival`,
`locked_quantity` and `transferable_quantity`. Risks include overdue reviews,
passed need-by dates, late/unknown arrivals, unavailable/missing supply, priority
shortfalls, quarantined source stock and stale/failed source evidence. This view uses local facts; it does not
pretend a dated promise is guaranteed delivery.

### Allocation precedence

The target deterministic order is:

1. Existing checkout/pick/package locks: never reassigned by a planning run.
2. Explicit customer/order commitments, including held commitments.
3. Accepted Inside Sales requests and protected account minimums [policy
   automation later].
4. Existing customer accumulation/release rules using only the eligible
   remaining stock.
5. General demand sorted by approved priority, needed-by date, creation time
   and a stable order/line tie-breaker.

An allocation rule determines who may use supply. A release rule determines
when the selected customer's shipment can proceed. A held unit must not appear
available to someone else simply because its owner is not currently releasable.
Policy preview should show the before/after promises and shortages before
publishing; applying a rule must not rewrite checked-out work.

### Allocate current stock

The user chooses a customer, optional exact order/line, part, quantity, priority,
need-by date, request reference, reason and review date. The server selects free
stock rows deterministically and creates the commitment atomically. Serialized
stock is pinned to concrete items immediately. The current API does not offer a
separate manual serial-selection override on an allocation.

An order-line commitment requires both order and line; an order-only commitment
can cover matching part/UOM demand within that order. Before checkout, validate
customer ownership, part, unit and remaining demand against VISUAL. Customer-only
commitments are already eligible for that customer's matching order demand.
The binding/reassignment command retains the original request/customer lineage
and records a reasoned transfer; it cannot change the owner silently.

### Allocate future supply and promote arrivals

Example: work order `WO-DEMO` expects 10 units. Customer A has a 6-unit future
commitment; Customer B has 3. One unit remains unpromised. A receipt of 4 units
creates 4 actual units and promotes 4 of A's commitment into stock commitments.
A retains 2 future units; B retains 3. A second receipt of 3 completes A's
remaining 2 and promotes 1 for B. No receipt creates stock twice, and no promoted
portion remains available in the future pool.

Promotion sorts by explicit priority (lower first), need-by date (undated last),
creation time and stable allocation ID. The example assumes A precedes B under
that ordering. Priority changes never reclaim already promoted or picked stock.
A held commitment remains held after promotion. Promotion order does not
automatically release the customer's shipment. If serialized, the concrete
received serials are recorded on the promoted stock commitments. Each child
references its original future allocation and receipt so the complete promise
can be reconstructed.

In the current record shape, `fulfilled` on a future parent measures quantity
promoted to physical stock; `fulfilled` on a stock child measures quantity
consumed by shipment release. A promoted future promise is not evidence that
its customer has received a shipment. Display those two meanings as promoted
and shipped, respectively. The child stores `parent_id` and the received stock
items plus `receipt_event_id`, linking the promotion to the triggering receipt.
The receipt ledger retains the same source event reference, including for
nonserialized stock whose physical balance is aggregated.

The foundation rejects receipt quantities above an expected work-order total;
review and update the declared supply before accepting verified overproduction.
Receipt of the wrong part or
unit cannot fulfill that work-order promise. Receipt without a work-order link
adds general stock and does not silently steal or satisfy a named future claim.

### Holds, expiry and freeing stock

Provide distinct commands:

- **Hold allocation:** keep ownership, prevent its use and require a reason;
  record a review date when appropriate. Do not change on-hand.
- **Remove hold:** restore use by the same customer/order; do not make it public.
- **Release allocation:** deliberately free the unconsumed, unlocked quantity
  for other demand, with a reason and audit history.
- **Bind/reassign allocation:** atomically move all or part of an unlocked,
  unconsumed promise to a customer/order with authorization and a recorded reason.

The accepted expiry behavior is **needs review**, preserving the promise.
Expiry never releases customer inventory automatically. Every overdue hold remains visible, with
owner, age and affected orders. Review dates are timezone-aware and displayed in
the configured warehouse timezone (initial operating context: America/Denver).

An order-level shipping hold and an inventory allocation hold are different.
The former stops an order from shipping; the latter reserves stock and prevents
its consumption. The order screen must show both and explain their effects.

## 9. Inside Sales workflow and policy ownership

The implemented review lifecycle is:

```text
draft -> submitted -> approved | rejected | cancelled
                    -> linked commitment and fulfillment progress
```

A request identifies customer, exact order/line if known, part/quantity, needed-by
date, optional work order, business reason and requested hold/review period.
An active supervisor/admin other than the requester approves supply ownership;
Inside Sales sees the decision, source,
shortage, expected arrival and subsequent receipts. The request is not itself
a reservation until approval executes successfully. Revising submitted terms
returns the request to draft and requires fresh submission. History retains the
previous terms and every review reason.

**Operations → Sales intake** records drafts that await an existing VISUAL order;
**Approval inbox** handles stock/future reservations, reassignment, hold removal or
commitment release, physical adjustments and early-release requests. The Sales
role can create/revise/submit/cancel its requests and inspect operating evidence;
it cannot mutate shipping, inventory, policy or close records. Early-release
review requires an administrator. Direct supervisor/admin inventory commands
remain an audited administrative path; document review remains its own control.
Automatic notifications are not part of this implementation.

Published release rules continue to govern accumulation, cadence, minimums and
order mixing. Explicit promise metadata governs priority, need-by and hold review.
Rule preview shows changes in eligible orders and prescriptions; saved planning
attempts retain the policy and inputs. Approval does not bypass credit, documents,
stock custody, packing or exact shipment validation.

No policy may release someone else's explicit commitment by silently ranking a
new customer higher. Such a transfer is a reasoned supervisor action.

## 10. Checkout, shipment and cancellation

### Checkout and local picking

With local inventory enabled, obtain order/demand and hard-hold facts from the
ERP adapter, and obtain supply and commitments from Dispatch. Do not allow
VISUAL's current inventory numbers to shrink the local queue indirectly through
legacy readiness stock columns. Stock-only readiness reasons are evaluated
against local supply; non-stock hard blocks remain source-backed.

During checkout, lock the exact local stock and commitment quantities under the
same database transaction as fulfillment creation and the existing order/cart/
operator/trace claims. Re-read versions and available quantities under that lock.
Two concurrent checkouts cannot consume the same final unit. The local warehouse
and location appear in the prescription; source ERP shelf bins no longer control
where the user is told to pick in this mode.

Each pick validates the prescribed local stock/serial and ownership. Existing
order/destination/document checks remain. A new order hold can stop progress,
but cannot free physically picked goods for another fulfillment. Cart, station
and package scans remain separate custody facts from the single ERP location.

### Shipment boundary in the initial implementation

1. Finish picking and package rescans.
2. Create/post the packlist through the existing VISUAL process using the
   Dispatch manifest.
3. Dispatch verifies exact site, order, line, part, stocking quantity, serial
   multiset, destination and cumulative ERP shipped quantities.
4. Verify all current, nonvoided package labels as today.
5. Final release transaction rechecks mandatory evidence, consumes local locks,
   debits local stock once, records fulfilled allocation quantities and retains
   the inventory ledger event with fulfillment/packlist references.

Dispatch's state remains **Released for shipping**. It is not evidence of carrier
pickup. Local consumption on release corresponds to the operational boundary
already used by the app; adding carrier pickup as a separate event is optional
future scope and must not cause a second stock debit.

This sequence deliberately has no initial outbound ERP posting worker: the ERP
packlist already exists before the local debit. If the release response is lost,
retry the same command and obtain its saved result. If ERP posting succeeded but
local release did not, the local lock remains and the reconciliation difference
is explained as a pending fulfillment. Retry validation/release; do not post a
second packlist or adjust stock merely to close the difference.

### Automatic shipment posting [later]

If installed, supported VISUAL APIs are verified in a test company, a later
adapter may create a shipment from the manifest. It requires a durable Dispatch
posting intent, stable external fulfillment reference, lookup-before-retry and
document reconciliation after timeout recovery. Transport retry alone cannot
guarantee exactly-once ERP posting.

Required states are queued, posting, uncertain, confirmed and attention-required.
After a timeout, search for the external reference before another create call.
If the installed API cannot uniquely record and retrieve that reference or
atomically post lines/traces, automatic posting remains disabled. Do not directly
INSERT/UPDATE VISUAL inventory or shipping tables as a shortcut.

### Cancellation, returns and adjustments

Unpicked work may release local locks after existing claim checks. Picked work
enters putaway and retains its locks until every unit is returned and scanned.
Returning a cart does not create a new receipt because its quantity was never
debited from on-hand. A released shipment's physical return requires inspection
and supported ERP processing. An ordinary manual receipt is rejected for a
serial with inventory history; an otherwise valid connected source arrival is
retained as `receipt_review` without stock or supply changes so synchronization
can continue. A supervisor can restore an existing
zero-balance serial with a reviewed positive adjustment and put the prior
shipment/source reference in its reason. A dedicated return event with structured
links and disposition is a later extension. Never retry the original production
receipt to represent a return.

If a verified ERP return/correction introduces a serial or component identity
that Dispatch has never held, it remains a source review issue rather than an
automatic receipt. After physical inspection and a fresh successful connector
comparison, a supervisor may add no more than the exact positive difference by
a reasoned adjustment. The latest connector reconciliation must still match the
unchanged local stock fingerprint and the source must be healthy. Unseen stock
without that evidence is rejected; neither a hand-entered comparison nor an
acknowledged alert supplies the required connector evidence.

Negative adjustments cannot remove allocated or fulfillment-locked stock without
first resolving the affected commitments. Missing serials should be blocked from
new use and investigated. Positive adjustments require evidence and a reason;
they do not silently fix VISUAL. A reversal must retain history and must not
reconstitute already shipped serials without a legitimate return.

## 11. Reconciliation and synchronization health

Receipt ingestion keeps operational stock current; reconciliation proves the
systems agree. Treat them as separate processes.

The implemented connector captures complete boundary evidence and delivers a
reconciliation on every poll, default once per minute. Delivery latency and full
history query cost must be measured in the test company; this is a configured
cadence, not a measured production service guarantee.

A comparison is scoped to the exact configured site/warehouse/location and
stocking unit. It compares both quantity totals and serialized identity sets.
Two balances of 10 with different serials are a mismatch. Future supply is
excluded from on-hand reconciliation.

The complete SQL capture supplies an observation time and source manifest;
the connector retains its full movement identity set. Dispatch's stock-version
fence rejects a capture if local stock changes while it is read. Opening imports
additionally require a coordinated stop of warehouse movements. An arbitrary
user-supplied JSON snapshot is useful evidence but cannot independently prove
fresh, complete ERP synchronization.

Classify differences rather than hiding them:

| Difference | Likely investigation | Correct response |
|---|---|---|
| Source receipt not yet delivered | Durable pending batch or capture error | Deliver the original event through its persisted batch |
| Local receipt absent in ERP | Manual receiving/posting gap | Complete supported ERP posting or reverse with evidence |
| ERP packlist posted, app still locked | Fulfillment awaiting local release | Finish validated release; no balancing adjustment |
| ERP stock removed without Dispatch work | Direct shipment/transfer/writeoff | Stop affected stock, investigate and record authorized correction |
| Equal quantities, different serials | Wrong trace, duplicate or swap | Resolve identity; do not net quantities |
| Wrong unit or location | Mapping/configuration error | Correct the mapping and repeat comparison |
| Work order reduced below promises | Supply plan changed | Re-plan explicitly and notify affected owner |
| Old or partial snapshot | Incomplete source evidence | Mark report incomplete; do not pass activation |

Reconciliation writes a report and exception history, never a replacement stock
snapshot. A confirmed difference requires a separate receiving, adjustment,
fulfillment or source correction. Keep before/after evidence and the reviewer.

Inventory includes `sync`: stream/sequence, observed time, last success, effective
status, fresh flag, source manifest, stock version, latest reconciliation and
issues. `freshness_seconds` is 300. A connected source becomes unavailable after
five minutes without a valid fresh capture. A sequence-fenced `capture_failed`,
`delivery_rejected` or `delivery_pending` health report makes it unavailable immediately. A fresh
accepted batch clears the source error; acknowledging an issue does not.
`delivery_confirmed` acknowledges the durable producer checkpoint and clears only
an outstanding `delivery_pending` condition. Daily close additionally checks this
confirmation against the current accepted sequence.

Negative reconciliation differences quarantine the affected stock from new
planning and picking. Stale/failed captures block all source-owned stock and
final release. A current negative difference caused by an already-posted ERP
packlist does not prevent final release of that already-picked work when the
existing exact live packlist/label checks succeed; this closes the expected gap
between ERP posting and the local debit. It is not permission to pick missing
stock or bypass a stale source. Positive differences remain visible and require
proper receipts/corrections rather than an automatic balance overwrite.

Connector `--status` exposes the local pending state, last success and safe error.
Capture/transport failure retains its accepted checkpoint and cannot falsely
advance freshness. Back up the outbox, monitor freshness/issues and assign a
review owner. Automatic notifications to staff remain deployment/operations work.

## 12. Screens and user decisions

**Inventory:** searchable part/serial rows with on-hand, available, allocated,
held and in-fulfillment quantities. Selecting a row shows owners, allocations,
receipts, current custody and ledger history. Quantities always show stocking unit.

**Receive stock:** part/quantity/UOM, serial scanning when required, source
transaction/event ID, optional work order and reason. Show the configured shipping
boundary rather than asking users to guess it. Confirm which future promises
were fulfilled and what became free. A successful retry must visibly remain one
receipt.

**Future supply:** existing work orders, due date, expected/received/remaining
quantities, promised/free future quantities and exception state. A user can see
who the next arriving units are planned for.

**Allocations:** customer and order ownership, source stock/work order,
remaining quantity, priority, need-by/request reference, hold/review date, risk
explanations, reason and lineage. Separate actions hold, unhold, free a
reservation and bind/transfer its unlocked remainder.

**Operations → Sales intake / Approval inbox:** editable drafts, submitted terms,
independent approve/reject, exact VISUAL binding, duplicate-reference review,
commitment outcomes and request history. Pending requests create no reservations.

**Operations → Order decisions / Planning history:** mirrored open/closed demand,
current blocker and responsible owner, next action, source age and retained
planning inputs/decisions/changes. Read views never commit a plan.

**Operations → Daily close:** prepare a captured boundary, inspect two-way
movement and shipment evidence, assign exceptions, propose correction
correspondences, independently sign and download the retained JSON packet.

**Reconciliation:** source boundary/as-of, compared quantities and serials,
explained in-flight differences, exceptions and correction references. Running
a comparison does not offer a silent "make app match ERP" action.

The existing Picking, Packing, Administration and Inventory audit screens remain
part of the same Dispatch app. A separate internal website or spreadsheet is
not needed to hold allocations. Inventory audit findings should link to the
inventory exception workflow; resolving an audit note alone cannot change stock.

## 13. Security, audit and concurrency

The foundation reuses session authentication, same-origin checks, CSRF protection,
role checks, command idempotency, optimistic revisions and the database mutex.
Inventory machine ingestion uses `FULFILLMENT_INVENTORY_TOKEN`, its own configured
bearer token, and never an
operator PIN or the label integration token. Tokens belong in protected service
configuration, not browser code, ledger events or connector logs.

Implemented role boundaries:

| Capability | Operator | Inside Sales | Supervisor | Administrator | Inventory connector |
|---|---|---|---|---|---|
| Read scoped availability and commitments | Yes | Yes | Yes | Yes | As needed |
| Confirm authorized receipt | No | No | Yes | Yes | Receipt ingestion |
| Request customer/future commitment | Yes | Yes | Yes | Yes | No |
| Accept/reassign/release commitment | No | No | Yes; independent review for inbox | Yes; independent review for inbox | No |
| Adjust physical inventory | No | No | Yes, with reason | Yes, with reason | Only separately authorized correction protocol |
| Change boundary or enable ownership | No | No | No | Yes | No |
| Publish allocation/release policy | No | No | No | Yes | No |
| Approve an early-release request | No | No | No | Yes, other than requester | No |
| Prepare/sign close | No | No | Yes, independent signer | Yes, independent signer | No |

Roles are operator, sales, supervisor and admin. Inventory browser mutations
require supervisor or administrator; configuration requires administrator.
Authenticated Sales and Shipping users can read inventory and operating evidence.
The inventory integration identity can receive, synchronize work orders,
reconcile, read connector state, deliver batches and report source health;
it cannot allocate, adjust stock or change configuration. The separate order
integration credential can upsert order facts only; neither machine identity can
submit or approve human workflow requests. Every reviewer must still have an
enabled human account at command execution.

Successful and rejected service commands retain actor, timestamp, action and
request detail; inventory commands also retain their resulting resource reference.
The stock ledger retains receipts, adjustments and shipment consumption. Identity
comes from authentication, not an actor field supplied in JSON. This audit is
application-managed history; it is not an independently tamper-evident archive.
Every committed planning attempt records inputs, source/policy revisions,
decisions, before/after allocation changes and failure status. Review history
retains exact terms and actor identities; resulting commitments carry approval
references. Source movement versions retain effective and observed history, and
signed close packets retain the original evidence and later-impact references.

Continue using the existing database transaction mutex for initial warehouse
volume. It provides one serial command order across API and worker processes;
no in-memory lock is a correctness mechanism. Every error path must roll back
all business changes while preserving the existing command/audit semantics.
Higher throughput may partition locks by site/stock identity later, with a
documented lock ordering and database concurrency tests.

## 14. Migration, cutover and rollback

### Prepare without changing production

1. Back up Dispatch and preserve its existing fulfillment/claims/events. Apply
   the new explicit schema migration with services stopped, as for existing
   upgrades. Local inventory starts disabled; normal operation retains its
   prior source until activation.
2. Choose and verify the one site/warehouse/location. Agree how nonserialized
   accessories enter this same inventory boundary; existing queries also read
   DISTRIBUTION, so simply selecting SHIPPING does not migrate those quantities.
3. Prove part/UOM/serialization mapping, composite work-order references and the
   receipt transaction source in a test company. Establish receiving ownership.
4. Rehearse receipt, future promise, partial arrival, held stock, checkout,
   packing, ERP packlist, release, cancellation/putaway and discrepancy repair.

### Opening balance and activation

1. Pause new warehouse allocations and movements for a coordinated cutoff.
   Complete or explicitly reconcile every active fulfillment and legacy
   reservation. The first implementation requires no active work or queued
   legacy reservations when switching inventory ownership.
2. Keep local inventory disabled and its stock ledger empty. Configure the exact
   boundary and connector environment. If manual opening receipts already exist,
   do not layer a connector opening import over them; reconcile that rehearsal
   data and establish an approved clean opening before proceeding.
3. Run `python -m fulfillment.inventory_connector --bootstrap --once` with the
   SELECT-only source and HTTPS receiver configured. The complete source capture
   creates identified opening receipts, work-order snapshots and a matching
   reconciliation atomically. Physically verify unresolved differences. Do not
   take stock from other warehouses by assumption.
4. Preserve the connector's SQLite outbox and opening source identity set. Run
   the connector normally; it audits all history on every poll and receives only
   previously unseen supported arrivals. Pre-opening transactions remain
   represented by the opening count, regardless of their numeric IDs. Run only
   one active producer with this stream/outbox; never reset it to force a retry.
5. Save a fresh, complete, matching reconciliation. Activation must require a
   report for the same boundary and unchanged inventory state; reject a stale
   report after any intervening stock movement or configuration edit.
6. Review configuration and enable app-owned inventory explicitly. Retain the
   existing exclusive Dispatch-operation cutover requirement. Restart/verify
   workers on the same inventory mode and observe the first real receipts.
7. Monitor each poll's comparison and rehearse response loss, capture failure,
   stale health, source edits/deletions, unlinked receipts and rejected-batch
   recovery before unattended production operation.

A demo-supplied or manually constructed matching report proves the comparison
engine, not that a production ERP export is complete. Production activation also
requires the connector's source/cutoff evidence and the business operating
procedure above.

### Rollback

Disable new checkouts and receiving changes first. Preserve all local ledger and
commitment history, reconcile every in-flight cart/package and ERP document, and
produce a complete stock/commitment handoff. Do not switch back to live ERP stock
while Dispatch holds or customer promises would disappear from allocation.

Changing the configured boundary after receipts or commitments exist requires
an explicit migration, not editing the warehouse field. Restoring a pre-cutover
database while real receipts/shipments have occurred loses evidence; replay and
reconciliation are required before operation resumes. Never run independent
legacy and Dispatch allocation writers against the same stock.

## 15. Acceptance criteria and test matrix

Acceptance requires behavior, not just tables or a successful HTTP response.
Use synthetic data for automated tests and a disposable PostgreSQL instance for
actual locking behavior. Do not run destructive fixtures against production.

| Scenario | Required result |
|---|---|
| Receive 5 unallocated component units | On-hand/free increase by exactly 5; ledger and source event retained |
| Retry identical event after response loss | Same receipt result; no second stock or ledger delta |
| Same source event ID, changed quantity | Conflict; all prior state preserved |
| New event ID, same existing serial | Manual receipt rejected; connected receipt retained for review, no stock or future-supply growth |
| Connector receipt mixes known and new serials | Whole receipt retained for review; no partial receiving; valid batch still reconciles |
| Distinct receipt events reuse a new serial between polls | Every involved receipt retained for review; net snapshot supplies correction evidence; no chronology inferred from IDs |
| Review-only receipt replay or attempt to link work order | Replay is a no-op; linking rejected with count-adjustment guidance |
| Different event IDs, same component source transaction/line | Detected or quarantined by connector/source uniqueness, never double counted |
| Wrong boundary, inconsistent existing part/UOM or work-order identity, malformed quantity, duplicate serial list | Atomic rejection |
| Newly supplied part/UOM/serialization | Connector validates master mapping before sending; foundation checks local consistency |
| Reserve all 5 units for customer A; customer B checks out | B cannot consume A's stock |
| Hold A, then remove hold | Ownership stays A throughout; availability to B does not increase |
| Release A's remaining reservation | Only unlocked/unconsumed stock becomes generally available |
| Promise 6 of a 10-unit work order, receive 4 then 2 | Exactly 6 stock commitments promoted; remaining future promise reaches zero |
| Two future customers and partial receipt | Priority/need-by/time/ID promotion; preserved parent/receipt lineage |
| Transfer part of a held promise | Successor inherits source/hold/lineage; no intermediate public availability |
| Transfer locked quantity or bind to another customer's order | Rejected atomically |
| Unlinked production arrival matched later | Existing free stock promotes once, with no new receipt/debit |
| Hold a future commitment then receive | Promoted stock remains held |
| Cancel/reduce work order below committed amount | Visible unresolved promise conflict; no silent promise deletion |
| Refresh work order from an older source revision | Does not rewind source state or receipt progress |
| Customer-only promise bound to wrong customer's order | Rejected |
| Duplicate SKU on two order lines | Line ownership and quantity remain distinct |
| Two concurrent allocations/checkouts for the last unit | One succeeds; other conflicts; no negative balance |
| Picked work cancelled | Stock remains locked until scanned putaway completes |
| Partial putaway or disconnect | No premature free inventory |
| ERP order hold arrives after allocation | Shipment blocked; ownership/custody preserved |
| ERP reports less stock than Dispatch | Affected stock quarantined for planning/picking; discrepancy visible; validated already-picked packlist release can close the expected posting gap |
| Packlist posted, local release response lost | Retry releases/debits once; no duplicate packlist |
| Final release fails label/ERP check | No local debit or promise consumption |
| Shipment release succeeds | Matching local debit, fulfilled promise update and lock disposition commit once |
| Adjustment would consume locked stock | Rejected with actionable reference |
| Positive adjustment for a previously unseen return/correction identity | Allowed only within the latest healthy connector's exact positive difference and unchanged local fingerprint; recorded as adjustment, not receipt |
| Same total quantity, different ERP serial set | Reconciliation mismatch |
| Mismatching snapshot submitted | Report saved; local balances unchanged |
| Matching snapshot followed by receipt before activation | Old reconciliation cannot authorize activation |
| Boundary changed after opening data | Rejected or routed through explicit migration |
| Pre-cutoff receipt replayed after opening import | Recognized as already represented; no duplicate opening stock |
| Late/lower transaction ID | Found by complete identity comparison; supported receipt delivered once |
| Historical movement edited, deleted or restored | Review issue; previously observed receipt never replayed as a new arrival |
| Local stock changes during source capture | Batch rejected before writes and recaptured from accepted checkpoint |
| Source capture fails or source age exceeds five minutes | Source-owned availability and final release blocked until a fresh accepted batch |
| Response lost after accepted batch | Exact durable body replay returns accepted result; checkpoint advances once |
| Explicitly rejected batch needs correction | Recapture only after unchanged receiver checkpoint is proven; uncertain deliveries remain pending |
| Expired hold | Review flag; promise retained by default |
| Unauthorized operator/connector uses management endpoint | Rejected and no state change |
| ERP unavailable | Local views/customer-only commitments usable; manual-source receiving usable, connected receiving waits; order planning and final shipping blocked |
| Disabled local mode | Existing legacy-source Dispatch behavior preserved |

Production rehearsal must also verify the real source transaction identities,
actual receipt/reversal event stream, partial work-order completions, stocking
units, serial trace identity, source snapshot completeness, scanner flow,
WorldShip refresh, backup restore and recovery after connector downtime.

Source implementation was checked against connected read-only schema definitions,
existing repository SQL and bounded aggregate validation of recent shipping
transactions/profile behavior. Adapter tests use synthetic rows, including
transfer, trace and quantity failures. The complete adapter has not been run
against the production boundary as part of this change; these checks do not
substitute for the coordinated test-company capture and acceptance procedure.

## 16. Delivery status and production decisions

Schema v5 implements local inventory, durable VISUAL order intake, Sales requests,
independent approval, retained planning history, the operating decision board and
daily close with movement coverage. Production remains on its existing process;
installing this code does not migrate a production database or activate ownership.

| Decision | Accepted behavior | Deployment evidence still needed |
|---|---|---|
| Shipping warehouse | SHIPPING, one configured site/location | Confirm the exact VISUAL site/location and treatment of stock elsewhere |
| Expired Sales hold | Retain ownership and flag overdue review; never auto-free | Assign reviewers and review cadence |
| Sales-order authority | VISUAL owns the commercial order; Dispatch drafts await exact binding | Rehearse order entry and complete initial mirror capture |
| Promise before an SO | Permitted through a separately approved customer commitment | Validate customer IDs and eventual binding responsibilities |
| Approval | Independent active supervisor/admin; admin for early release | Provision named reviewers and train users |
| Physical debit | Final Dispatch release after exact document/label checks | Rehearse the actual warehouse posting procedure |
| ERP outage | Local evidence remains visible; source-dependent operations fail closed | Monitor freshness and assign recovery ownership |
| Daily close | Coordinated pause, captured evidence, explicit exceptions and independent signature | Verify modern opening lineage and transaction coverage |
| Automatic ERP writeback | Not implemented | Validate the installed supported interface before a separate writer project |

Deployment must validate SQL load, transaction classifications, full history,
order/line semantics, scanner/printer behavior and WorldShip mapping. Run one
active inventory connector with its backed-up durable outbox. Coordinate opening
import and the separate exclusive-operation cutover. Automatic staff notifications,
a new allocation optimizer and internal multi-bin management are not supplied.

## 17. Order intake, planning, approval and daily close

This is the implemented schema-v5 operating contract. Production acceptance is
separate and uses the actual VISUAL company, site/location, devices and posting
procedures. The application does not write ERP transactional records.

### Sales-order entry and intake

VISUAL remains authoritative for commercial orders. Dispatch provides two paths:

1. **Record an existing VISUAL order.** A separate order bearer authenticates
   `POST /api/v1/integrations/orders` with stable event ID, source revision,
   observed time and exact order/line facts. Repeated events replay safely; older
   revisions/observations cannot overwrite newer facts. This does not create an
   ERP order, and one event does not certify complete order-book freshness.
2. **Record a sales request.** Operations → Sales intake saves customer/site,
   PO/reference, destination, date and requested lines/units/quantities.
   Submission becomes `awaiting_visual_order`. A supervisor/admin binds an
   existing VISUAL order after checking customer/site, PO/reference, destination,
   exact open lines and remaining quantity. Duplicate references across drafts
   and mirrored orders require an independent review reason. A draft cannot ship;
   a separately approved customer commitment may be linked while it awaits the SO.

The planner performs a complete order capture independently of the shipping
lookahead. Its durable mirror retains status, ordered/shipped/open quantities,
units, dates, destination, blocking facts, revision/digest and observation time.
Failed captures preserve old orders and mark source availability false; only a
successful complete snapshot can mark absent orders missing. Order changes flag
affected promises without freeing stock or removing picked custody.

Live local-inventory planning requires an initial complete capture and fresh,
healthy mirror evidence, bounded at 300 seconds. The synthetic training fallback
before a mirror exists is not a live bypass. Single-order callbacks do not satisfy
that initial complete-capture requirement. Published policies and shipping checks
still decide eligibility.

Bindings retain cumulative-shipment positions so proven shipments free the
corresponding previously bound demand, while an order quantity reduction does
not masquerade as shipment. Duplicate binding cannot exceed remaining demand.
Linked commitment limits include both quantities already fulfilled and remaining
promises; future promotion and same-customer transfers do not count twice or
reset the customer's requested quantity. Revising/cancelling an unbound draft
with active linked promises first requires their explicit review/release.

Direct ERP order creation, receipt/adjustment posting and shipment writing remain
the responsibility of existing supported VISUAL processes. The app never reports
a draft as a successfully created ERP sales order.

### Allocation timing and persistence

- **Commitments** are accepted stock/future promises or protected policy
  accumulations stored in PostgreSQL. They survive retries, runs, restarts and
  rule changes. Explicit guarded actions change their ownership.
- **Ready suggestions** are current free-stock prescriptions. They remain
  tentative until checkout and can change when inputs change.
- **Fulfillment locks** protect exact stock/serial quantities for active work.
  Picked stock remains on hand until verified release or scanned return.

Receipt acceptance, relevant inventory/policy changes, order intake and request
changes enqueue durable triggers. Equivalent triggers coalesce while retaining
actor/time references. The worker defaults to 60 seconds; supervisor/admin can
run planning explicitly. Callbacks queue work; they do not synchronously execute
a general planning run. Queue and board reads do not create commitments.

Receipts with exact work-order lineage immediately promote that work order's
existing promises in priority, need-by, creation-time and ID order. Partial
arrivals create linked stock children and preserve customer/order, holds and
approval references. Unpromised arrivals enter the shared pool; the next planner
applies published rules and persists protected accumulation. Ordinary ready
suggestions remain tentative. Unlinked arrivals cannot silently fulfill a named WO.

Expiry never frees a Sales hold. Removing a hold keeps ownership; freeing a
commitment returns its unlocked remainder to shared availability. Transfers cannot
consume checked-out stock. Formal reassignment/hold-release approval also rejects
quarantined stock or uncovered future supply.

### Planning history and order explanations

Every attempt retains run ID, trigger references, start/finish/status, captured
order/policy/stock/source/work inputs, order decisions and allocation before/after
changes. Failures retain prior promises, record an error and leave triggers pending.
Worker starts commit separately so abandoned attempts can be marked interrupted
after their lease instead of disappearing.

Operations → Order decisions shows all mirrored demand with reason, owner, next
action and source/decision age. States include ready, waiting stock, future supply,
customer hold, accumulation, pending approval, source unavailable, order blocked,
outside horizon, commitment conflict, in progress and released. Changed inputs
invalidate a prior ready suggestion until replanning. Planning history retains
the earlier explanations and inputs; today's board does not replace that history.

Checkout retains its own decision provenance and links a previous run only when
its inputs and decision match. Approval does not guarantee shipment: order,
source, document, packing, packlist and label requirements remain effective at
their guarded operational boundaries.

### Independent approval

The lifecycle is `draft -> submitted -> approved | rejected | cancelled`.
Pending requests do not reserve stock. Terms, requester, versions, submission
digest, reasons and all reviews are retained. Revising a submission returns it
to draft. The requester cannot approve or reject their own request.

The five request kinds are stock/future allocation, transfer, allocation change,
inventory adjustment and early release. Active supervisors/admins review inventory
requests; early release requires admin. Account status and role are rechecked under
the warehouse lock. Machine identities cannot review requests. Direct privileged
inventory commands remain audited; Sales uses the independent inbox.

Approval rechecks live order ownership/status/open demand, units/lines, source
health/capacity, resource revisions and linked sales quantity. Adjustments require
an unchanged count basis and cannot consume promised or checked-out stock.
Shortage, stale revision or outage leaves the request unapproved without partial
allocation. Source failures are audited and retryable. Approval and its resulting
inventory/control change commit together; duplicate approval cannot reserve twice.
Rejection/cancellation have no inventory effect.

Allocation approval does not authorize shipment or satisfy document review.
Early release is tied to the submitted source order context, expires explicitly
and cannot bypass hard shipping checks. Later commitment release/reassignment
uses a separate reviewed request or authorized administrative operation.

### Daily close and correction evidence

Daily close provides Prepare, exception review, correction correspondence,
independent Sign and a retained JSON packet. Evidence status is separately
`incomplete | exceptions | balanced`; approval is `draft | signed | superseded`.
A signed close with accepted exceptions remains an exceptions close.

Preparation requires a warehouse-pause attestation and captures the actual source
observation boundary, warehouse timezone/business date, previous signed cutoff or
verified opening, local stock/ledger, source movement versions, comparison,
commitments, custody and shipment documents. The pause does not lock external
VISUAL writers. A present capture cannot prove arbitrary historical midnight stock.

The evidence checks:

- Opening plus receipts/adjustments minus shipments equals closing stock by
  part/unit/serial; prior retained ledger entries still match their digests.
- Fresh complete source stock agrees in quantities and serial identities; the
  accepted sequence has durable connector delivery confirmation.
- Relevant source and local movements are covered in both directions through
  exact receipt IDs, shipment order/packlist/line/serial identities and reviewed
  correction links. Missing receipt and shipment cannot disappear by netting zero.
- Relevant released packlists are read again. Changed, missing or voided documents
  become exceptions; pending work/custody carry forward. A/S/P document acceptance
  does not prove invoicing, financial posting or carrier departure.

Missing/partial/stale source, missing opening lineage, uncertain delivery or
incomplete document evidence prevents sign-off even with exception override.
Other exceptions require active named owner, due time, supporting reference and
explicit acceptance. A note cannot resolve a quantity difference.

Correspondences match exact retained source revisions to already authorized
adjustment entries. Direction, quantity, unit and serial vectors must agree;
opposite movements cannot hide gaps. Metadata-only corrections have no quantity
effect. Shipment posting gaps resolve through fulfillment/document flow, never a
balancing adjustment. Correspondence creates evidence, not stock. A signer must
be different from both the preparer and any applied correspondence's creator.

Sign-off rechecks material stock, source movement, work, commitment and document
evidence. Changes require a new prepared packet. An unchanged healthy poll may
advance sequence/time without invalidating the captured boundary; both captures
are retained. Concurrent drafts cannot both become the next baseline. Superseding
preserves the old packet and cannot discard later dependent closes.

Late/backdated edits, deletions and document changes flag earlier affected closes.
Current-period corrections retain the prior-period link. Version-2 opening
movement evidence is required: upgrading an old opening with only fingerprints
does not reconstruct its missing details. Such a warehouse remains incomplete
for close until a separately verified opening/rebaseline procedure establishes
the required evidence. No API silently resets an old stream or calls it balanced.

### Acceptance evidence and production gates

Automated tests cover order replay/revisions, source failure retention, requester
separation, revoked accounts, stale/concurrent approvals, shortages, future
promotion with holds, order cancellation/reduction, exact sales binding and
fulfilled quantity limits. Planner, movement coverage, shipment recheck, incomplete
close refusal, independent sign-off and correspondence recovery are also covered.
HTTP checks exercise session/CSRF/origin boundaries, separate machine credentials,
command retry and the larger capture body limit.

See the runbook for validation checkpoints. Synthetic/disposable-database checks
do not establish installed-company SQL performance, source completeness, hardware
integration, independent audit archival or production activation. These remain
explicit deployment acceptance gates.
