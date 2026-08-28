# Shipping KPI Dashboard and Release Gate Plan

Status: **engineering implementation complete; advisory rollout ready**
Decision source: JP / Jayelynne / Tyler email thread, August 26-27, 2026
Prepared: August 27, 2026

## Implementation and verification record — August 27, 2026

The engineering scope in this plan is now implemented:

- `shipping_metrics.py` calculates all six KPIs, equal-period comparisons, daily
  trends, late-order and single-gun action queues, and source-coverage diagnostics.
- `release_gate.py` produces one deterministic `RELEASE`, `ACCUMULATING`, `HOLD`, or `BLOCKED`
  decision per candidate order. The existing guns and components queries consume the
  same released order set before supply allocation when enforcement is enabled.
- Configured accumulating accounts receive serial-level, auditable protection for shelf
  inventory. Protected serials remain in their ERP shelf locations, stay attached to the
  same order across evaluations, cannot be consumed by later decisions, and are rechecked
  against live ERP immediately before picklist allocation.
- Major accounts can prohibit sales-order mixing, use a configurable 42-gun-style target,
  release a complete remainder below target, and release aged reservations after seven
  days. Standard accounts can use one daily customer/ship-to batch while retaining
  separate sales orders and packlists. A per-policy ship-to cooldown prevents another
  picklist for a recently shipped destination until its next eligible calendar date.
- Versioned policies, evaluations, decisions, sticky serial assignments, expiring exceptions, and metric
  snapshots are persisted in SQLite. The default remains `advisory` and enforced-mode
  evaluation failures stop the picklist run rather than bypassing the policy.
- The Shipping Scorecard displays JP's six measures, comparisons, service and volume
  trends, action queues, release status, freshness, and coverage. A compact summary is
  also prominent on the main Operations Dashboard.
- Live read-only VISUAL validation for the 30-day window reconciled exactly to an
  independent aggregate: **3,491 serialized guns** and **1,699 firearm-bearing
  packlists** in both paths. The optimized source queries completed in 1.64 seconds
  for metrics and 0.30 seconds for release candidates during that validation.
- Desktop and 375px responsive browser QA passed with no console errors or page-level
  horizontal overflow. The first 100 prioritized release decisions are rendered while
  summary counts continue to cover every candidate.
- Automated coverage includes KPI, major-account isolation, standard daily batching,
  seven-day aging, sticky serial persistence, SQL filtering, API, and route/render checks.

The remaining items are governance gates: Shipping must review advisory decisions and JP
must approve the final account list, cutoff time, per-account targets, and enforcement date.

## Goal

Give JP, SLT, and the Shipping Manager one trusted view of whether the end-to-end
Production -> Final Assembly -> Shipping process is working, while allowing Shipping
to pull throughout the day without creating unnecessary single or fragmented shipments.

The goal is complete when:

- JP's six KPIs are calculated from documented, reconciled sources.
- The scorecard is prominent, current, understandable at a glance, and useful for both
  management review and operational follow-up.
- A deterministic order-release gate controls what reaches the pick queue, with simple
  hold/release reasons and auditable exceptions.
- Existing behavior remains covered and all old and new automated tests pass.
- Shipping has reviewed the rules in advisory mode, the Shipping Manager owns daily
  execution, and JP approves enforcement.

## Confirmed direction from the discussion

- Manage the combined process outcome, not whether one department looks busy.
- Show these six KPIs: Ship on time, Ship complete, average guns per shipment,
  single-gun shipments, total guns shipped, and total shipments.
- Reduce single and small fragmented shipments without damaging on-time or complete
  shipment performance.
- Allow Shipping to pull whenever useful inventory becomes available. Consolidation
  should be controlled upstream by order eligibility, not by limiting picklist timing.
- Keep one-order flow where possible and leave guns in their located shelves until the
  order is ready to pull and ship. Do not add staging merely to create work.
- Give Shipping four plain-language answers: what ships, what is held, when an exception
  is allowed, and who decides when the rule is unclear.
- Involve Shipping before enforcing the process. The future Shipping Manager owns the
  result; the dashboard supplies shared facts and SLT visibility.

## Current system baseline

The repository already provides several useful foundations:

- The guns picklist prioritizes Promise Delivery -> Promise Ship -> Desired Ship and
  allocates available shelf inventory deterministically.
- The Shipping area already has order-level pick flow, packlist verification,
  end-of-day plan reconciliation, shortage analysis, stage aging, and excess-packlist
  cost reporting.
- The current `MAX_RUNS_PER_DAY` setting limits how often a list can be generated. That
  is a timing throttle, not an order-release policy, so it cannot tell Shipping which
  available orders should be held for consolidation.
- Same-order/same-day excess packlists are already measured, but the six management KPIs,
  trends, definitions, targets, and release decision audit do not exist yet.
- Baseline test run before this work on August 27: **60 passed**; final implementation
  run: **104 passed**.

## Measurement contract

These are proposed canonical definitions. They must be reviewed with JP and Shipping
before targets or enforcement are enabled.

| KPI | Proposed headline definition | Grain and denominator | Required diagnostic |
| --- | --- | --- | --- |
| Ship on time | Percent of due customer orders whose physical shippable lines are fully shipped by the effective Promise Ship date | Order-level due-date cohort; include open past-due failures; exclude orders without a usable promise date from the rate | Missing-promise-date count/rate, late orders and days late |
| Ship complete | Percent of shipped orders whose first shipment event clears all physical shippable quantity on the order, with no later split required | Order-level shipped cohort | Remaining units after first shipment and split reason/exception |
| Average guns per shipment | Firearm units shipped divided by distinct live firearm-bearing packlists | Shipment/packlist level | Distribution by customer and 1 / 2-4 / 5+ gun bands |
| Single-gun shipments | Distinct live firearm-bearing packlists containing exactly one firearm | Show count and percent of firearm shipments | True one-gun orders vs avoidable partials vs approved exceptions |
| Total guns shipped | Sum of firearm units on non-voided shipment lines by shipped date | Unit level | Customer/day trend and comparison period |
| Total shipments | Count of distinct non-voided firearm-bearing packlists by shipped date | Shipment/packlist level | Packlists vs carrier packages if those concepts differ |

Metric rules:

- Use `SHIPPER.SHIPPED_DATE` in the configured plant timezone for shipment windows.
- Use `COALESCE(CUST_ORDER_LINE.PROMISE_DATE, CUSTOMER_ORDER.PROMISE_DATE)` as the
  provisional on-time commitment. Do not substitute Promise Delivery without JP's
  explicit agreement.
- Exclude voided/cancelled shippers and deduplicate line/trace joins before calculating
  shipment counts or firearm quantities.
- Define "physical shippable lines" by an explicit product-code/classification map so
  freight, service, tax, and other non-physical lines do not make an order look incomplete.
- Show data freshness and the latest complete date on the scorecard.
- Treat missing promise dates and unclassified lines as visible data-quality guardrails,
  not silent exclusions.
- Backfill a review window (recommended: 8-12 weeks), then preserve daily metric facts or
  snapshots so later date edits cannot silently rewrite history.
- Support target configuration, but do not invent targets. Establish a baseline first;
  JP approves the operating targets and alert thresholds.

## Release gate policy

### Core model

Build one coordinated release decision per customer order before inventory is exposed to
the guns/components pick queues. Both list types must consume the same release decision so
the application does not release the gun half of an order while holding its other physical
lines.

The gate is application-level at first. It controls picklist and pick-queue visibility and
does not change the ERP order status. That keeps the change reversible and avoids a new ERP
write permission. If the business later wants VISUAL status changes, treat that as a
separate permissioned phase.

Evaluate rules in this precedence order:

1. **Hard business block** - hold orders that already fail credit, order/line status,
   ship-to, RMA/international, or other existing eligibility rules. Show the reason.
2. **Approved exception / ordinary commitment protection** - an approved exception may
   release available units; non-accumulating orders may release when the commitment
   horizon is at risk.
3. **Sticky protected accumulation** - for accounts configured with `accumulate: true`,
   assign eligible ERP serials in promise priority, preserve each serial's first-assigned
   timestamp, and remove it from the supply seen by later orders. Revalidate every sticky
   assignment from live ERP immediately before picklist allocation.
4. **Ship-to cooldown** - unless an approved exception applies, a destination with a
   recent shipment cannot release again until `last shipment + ship_to_cooldown_days`.
   Accumulating orders may keep protecting serials during the cooldown; ordinary held
   orders do not consume supply.
5. **Ship complete** - release when all physical shippable lines can be covered from the
   currently allocatable supply and no conflicting pick/packlist already exists.
6. **Major-account consolidation** - when sales-order mixing is disabled, expose only the
   oldest sticky sales order for that customer. Release at its configured gun target, when
   its remainder is complete below target, at the maximum hold age, or by exception.
7. **Default store batch** - optionally combine ready orders only for the same customer and
   ship-to, then release that group at one daily cutoff. Sales orders and packlists remain
   distinct even though the physical outbound shipment may be consolidated.
8. **Manual exception audit** - require a reason,
   actor, timestamp, and expiration. Typical categories: customer expedite, commitment at
   risk, compliance/ATF, carrier cutoff, backorder authorization, or manager approval.
8. **Otherwise hold** - show why it is held, guns ready, total open guns, missing items,
   age, Promise Ship date, and the next expected release condition/date.

The released set is then allocated deterministically using the existing priority order.
A held order must not consume supply ahead of released orders. An accumulating order may
protect supply while physical units remain on the shelf. Valid serial assignments persist
across evaluations; they are removed only when the serial leaves eligible ERP inventory or
the order/policy no longer permits the assignment. Re-running the picklist may add newly
eligible orders, but it must revalidate and cannot bypass the gate.

### Operator experience

Use concise labels on every order:

- `SHIP NOW - complete`
- `SHIP NOW - commitment at risk`
- `SHIP NOW - scheduled customer release`
- `SHIP NOW - approved exception`
- `ACCUMULATING - <protected>/<target> customer guns protected`
- `ACCUMULATING - commitment at risk; review exception`
- `HOLD - waiting for order completion`
- `HOLD - below customer batch threshold`
- `HOLD - scheduled release on <date>`
- `BLOCKED - <existing eligibility reason>`

Shipping sees the reason and escalation owner without needing to understand the scoring
or allocation internals. The existing one-order/tote picking flow remains intact.

## Dashboard brief

Primary audience: JP and SLT for recurring operating review; Shipping Manager for daily
action; Production, Final Assembly, and Shipping for shared accountability.

Default scorecard layout:

1. **Header** - date window (Today, last 7 days, month-to-date, custom), latest complete
   data date, refresh state, and a short statement of the shared outcome.
2. **Six hero KPIs** - exactly JP's requested measures. Each card shows the current value,
   prior-period comparison, approved target when available, and a plain definition link.
3. **Outcome trends** - Ship on time and Ship complete over time, with target lines.
4. **Consolidation trends** - average guns per shipment and single-gun count/rate over
   time; show the relationship so improving consolidation is never read without service
   performance.
5. **Throughput** - guns and shipments by day.
6. **Management action queue** - late/at-risk orders, avoidable one-gun partials, held
   orders nearing their deadline, and active release exceptions.
7. **Driver detail** - customer breakdown, release/hold reasons, and links into existing
   reconciliation and excess-packlist views.
8. **Data-quality footer** - missing promise dates, unclassified lines, source freshness,
   and any unreconciled totals.

Make the scorecard prominent by adding it to the Shipping navigation and a compact KPI
summary/link on the main Operations Dashboard. Load the live/cached metrics asynchronously
so the current Shipping work overview still opens immediately if the ERP is slow.

## Implementation workstreams

### 1. Confirm definitions and policy

- Review the metric table and rule precedence with JP.
- Run the planned Shipping-team session to collect real exceptions and operational traps.
- Decide whether Ship complete covers all physical order lines or firearms only.
- Confirm whether "shipment" means packlist or carrier package/tracking number.
- Approve customer-specific thresholds, sweep days, due-date override horizon, exception
  owners, and initial advisory-mode pilot scope.
- Record decisions in this document before enforcement.

### 2. Build source queries and pure calculation modules

- Add a raw shipping-metric query covering shipment headers/lines, order quantities,
  product classification, promise dates, customer, and void status.
- Add a release-candidate query that returns all open physical lines and the supply facts
  needed to judge order completeness before allocation.
- Implement `shipping_metrics.py` as a pure, JSON-safe calculator for the six KPIs,
  trends, diagnostics, coverage, and drill-down rows.
- Implement `release_gate.py` as a pure deterministic rules engine returning a decision,
  reason code, evidence, and next action for every order.
- Keep date windows, timezone handling, firearm classification, and exclusions centralized
  so cards, charts, exports, and gates reconcile.

### 3. Persist policies, decisions, exceptions, and history

- Add local SQLite tables for versioned release policies, order exceptions, release
  decisions, and daily metric facts/snapshots.
- Record policy version and source data timestamp with every decision.
- Require actor/reason/expiry for exceptions and keep the audit readable from the UI.
- Add settings for advisory/enforced mode, targets, due-date protection horizon, and
  customer policy management. Default new enforcement to off/advisory.

### 4. Integrate the release gate with picklist generation

- Generate one coordinated release set for guns and components.
- Apply the gate before allocation so held demand does not consume available supply.
- Persist the release set used by each run and include the reason in exports/notifications.
- In advisory mode, leave list output unchanged but display the proposed released/held
  result and estimated impact.
- In enforced mode, exclude held orders from pick/export/claim paths and prevent a stale
  client from claiming an order whose gate decision has changed.
- Retain `MAX_RUNS_PER_DAY` only as an optional load-control setting, not the consolidation
  policy. A refresh should never make an ineligible order shippable by itself.

### 5. Build the scorecard and action views

- Add cached metric and release-status APIs with explicit `as_of`, coverage, and error
  fields.
- Add the six KPI cards, trends, filters, action queue, definitions, and drill-downs to a
  Shipping Scorecard view.
- Add a compact scorecard summary to the existing Operations Dashboard.
- Preserve the existing Reconciliation and Excess Packlists pages as diagnostic links;
  do not duplicate their detailed tables.
- Add a downloadable review extract for KPI numerator/denominator and release decisions.

### 6. Test and reconcile

Add unit tests for:

- every KPI numerator/denominator, zero denominators, inclusivity at the Promise Ship
  cutoff, missing dates, partial shipments, voids, duplicated joins, same-day and
  cross-day splits, legitimate single-unit orders, exception classification, and plant
  timezone boundaries;
- gate precedence, complete/incomplete orders, due overrides, customer thresholds and
  sweep days, expired exceptions, hard blocks, deterministic ordering, and supply that
  cannot be allocated twice;
- coordinated guns/components decisions and the guarantee that held orders do not consume
  released supply.

Add route/integration tests for:

- metric API schema, filters, cache/refresh behavior, degraded ERP response, and access
  control;
- advisory vs enforced output, pick/export/claim enforcement, exception CSRF/audit, and
  stale-decision rejection;
- scorecard render with data, empty data, missing targets, and partial coverage.

Before rollout:

- Reconcile total guns and shipment counts to an independent ERP query for sampled days.
- Reconcile current same-order fragmentation to the existing Excess Packlists view.
- Hand-check at least ten on-time/late and ten complete/incomplete orders against VISUAL.
- Run the full suite; the acceptance gate is all existing 60 tests plus all new tests
  passing in the supported local/container environment.

### 7. Roll out safely

1. **Metrics shadow** - publish definitions and dashboard using backfilled/current data;
   no gating. Resolve discrepancies before targets are used.
2. **Advisory gate** - show Ship/Hold recommendations and collect Shipping feedback without
   changing the picklist.
3. **Limited pilot** - enforce for one approved customer/policy (a likely candidate is
   Lipsey's after its threshold and sweep day are confirmed).
4. **Broader enforcement** - expand only after on-time and complete guardrails remain
   healthy and exception reasons are understood.
5. **Ownership handoff** - Shipping Manager owns daily review and exceptions; Tyler owns
   data/reliability; JP owns KPI targets and policy changes.

## Definition of done

- All six KPI definitions are approved, visible next to the metrics, and produce
  reproducible results for Today, 7-day, month-to-date, and custom windows.
- Dashboard totals reconcile to the ERP; freshness and data-quality gaps are visible.
- The default view answers in seconds: Are we on time? Are we complete? Are we
  consolidating? What needs action today?
- Every candidate order has one deterministic Ship/Hold/Blocked decision and a human
  explanation.
- Repeated list generation does not create fragmented eligibility or bypass a hold.
- Manual exceptions are authorized, time-limited, and auditable.
- Shipping signs off on the operator instructions; JP signs off on KPI definitions,
  targets, and enforcement rules.
- Existing and new automated tests pass, and the advisory/pilot comparisons show no
  unexplained deterioration in Ship on time or Ship complete.

## Decisions still required before enforcement

1. Does Ship complete cover all physical order lines or only firearms?
2. Is the official shipment grain a VISUAL packlist or a carrier package/tracking number?
3. What counts as a legitimate single-gun exception, and who may approve it?
4. Which customers need custom minimum quantities or release weekdays, and what are the
   first approved values?
5. How close to Promise Ship must an order be before service protection overrides
   consolidation?
6. What baseline period and targets will JP use for the six KPIs?
7. Should the main `/shipping` landing page open the management Scorecard or the current
   operational Overview?
