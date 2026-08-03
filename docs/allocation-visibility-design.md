# Allocation Visibility & Prioritization Tool — Analysis and Design

Status: **DESIGN — no code changes yet.** All findings below were verified against the live
VECA database (SQL Server 2016, site `TDJ`) and this repository on 2026-07-29, using
`801-06531-00` as the worked example.

---

## 1. Summary of the current database and application logic

### The application
- Flask app ([app.py](../app.py), ~4,450 lines), server-rendered templates + JSON APIs.
- **Read-only against VECA today.** All writes go to local SQLite (`data/picklist_history.db`
  — run history, pick sessions) and Postgres (`audit_store.py` — serialized-inventory audit).
  The tool proposed here would be the **first feature that writes to the ERP**.
- Scheduled daily picklist runs (APScheduler), Excel export, Telegram/SMTP alerts.
- **No per-user identity.** The only auth is a shared settings password gating `/settings`
  (`SETTINGS_SESSION_KEY`). This matters for the audit-trail and permissions requirements.

### The picklist allocation engine
Two production queries, same algorithm ([sql/query_guns.sql](../sql/query_guns.sql),
[sql/query_components.sql](../sql/query_components.sql)):

1. **Supply** = physical bins in `PART_LOCATION`:
   - Guns: warehouse `SHIPPING`, racks `R01`–`R09`, excluding `%STAGE%` / `%INTERNATIONAL%`.
   - Components: `DISTRIBUTION` (excluding `%STOCK%` bins) + `SHIPPING` rack `R11*`.
2. **Demand** = open CO lines passing eligibility filters (§3).
3. **Allocation** = cumulative-range interval join: running supply totals vs running demand
   totals per part, FIFO. Deterministic and proven — the proposed tool reuses this approach.

[shortage.py](../shortage.py) + [sql/shortage_components.sql](../sql/shortage_components.sql)
report demand the picklist cannot fill, with the same demand ordering.

### VISUAL's native allocation machinery (checked, mostly unused)
- `DEMAND_SUPPLY_LINK` (CO→WO: 136k rows) is used **only for RMA-repair / make-to-order
  linking** (`DEMAND_PART_ID = 'RMA REPAIR'`). Zero rows exist for stock SKUs like
  801-06531-00.
- `CUST_ORDER_LINE.ALLOCATED_QTY` / `FULFILLED_QTY` are 0.0 on every open line of the
  sample SKU. VISUAL auto-allocation is **not** in use for stock product.
- Conclusion: there is no ERP-resident allocation to respect or conflict with for stock
  SKUs; our allocation model is a *projection* we compute ourselves. We must not write to
  these tables.

---

## 2. Actual table/column mappings for the four date fields (verified)

| Business field | Table.Column | Verified notes |
|---|---|---|
| 1. CO Desired Ship Date (header, Inside Sales) | `CUSTOMER_ORDER.DESIRED_SHIP_DATE` | datetime, exists |
| 2. CO **Line** Desired Ship Date — shown as **"Prod Date"** (Operations) | `CUST_ORDER_LINE.DESIRED_SHIP_DATE` | datetime, exists. The "Prod Date" label is a VISUAL client screen customization; the DB column name is unchanged |
| 3. Promise Ship Date (Inside Sales, OTD baseline) | `CUSTOMER_ORDER.PROMISE_DATE`, line override `CUST_ORDER_LINE.PROMISE_DATE` | Both exist; existing queries use `COALESCE(line, header)` |
| 4. Promise Del Date (Inside Sales, allocation priority) | `CUSTOMER_ORDER.PROMISE_DEL_DATE`, line override `CUST_ORDER_LINE.PROMISE_DEL_DATE` | Both exist; existing queries use `COALESCE(line, header)` |

Also relevant: `CUST_LINE_DEL` (delivery-schedule children with their own
`DESIRED_SHIP_DATE`) exists but is ignored by every current query — see open question Q5.

**Live-data reality check (801-06531-00, 84 open lines / 112 open units):**
- Header `DESIRED_SHIP_DATE` = **2026-07-05 on every open line's order** and line
  `DESIRED_SHIP_DATE` ("Prod Date") = **2026-07-27 on every open line**. Both are being
  mass-maintained by scheduling as the estimate slips. The header date **no longer carries
  sales intent** — it moves with operations.
- `PROMISE_DATE` (ship promise) varies per order (2025-02-28 … 2026-06-01) — consistent
  with "original commitment, not auto-moved."
- `PROMISE_DEL_DATE` is **populated at header level on most orders, and only occasionally
  at line level**. Any tie-breaking and edit design must handle the header/line coalesce
  explicitly.

### Supply-side mappings (verified)

| Supply class | Source | Date | Quantity |
|---|---|---|---|
| On-hand, allocatable | `PART_LOCATION` per picklist bin rules above | today | `QTY` |
| Released WO | `WORK_ORDER` `TYPE='W' AND STATUS='R'` | `COALESCE(SCHED_FINISH_DATE, DESIRED_WANT_DATE)` — sched dates are **NULL** on the sample SKU's WOs, so `DESIRED_WANT_DATE` is the operative date | `DESIRED_QTY − RECEIVED_QTY` |
| Firmed WO | `WORK_ORDER` `TYPE='W' AND STATUS='F'` (30 exist site-wide) | same | same |
| Master schedule | `MASTER_SCHEDULE` (`MASTER_SCHEDULE_ID='STANDARD'`, weekly buckets, `FIRMED` flag) | `WANT_DATE` | `ORDER_QTY` |
| MRP planned orders | `PLANNED_ORDER` (`WANT_DATE`, `ORDER_QTY`) | `WANT_DATE` | `ORDER_QTY` |

Caveats:
- `WORK_ORDER.TYPE='M'` is the **engineering master** (template, BASE_ID = part id), *not*
  the master schedule. Do not treat it as supply.
- `STATUS='X'` (cancelled, 58k rows) and `'C'` (closed) must be excluded.
- WOs for this SKU are qty-1 each (serialized firearms) — released in same-day batches with
  a shared `DESIRED_WANT_DATE`, e.g. 20 × qty 1 wanted 2026-08-05.
- `MASTER_SCHEDULE` and `PLANNED_ORDER` **overlap** each other and overlap released WOs
  inside the fence (MPS week of 2026-07-27 = 3 units, while 20 qty-1 WOs were just released
  for 2026-08-05). Netting rules are open question Q2.

---

## 3. Existing picklist eligibility and sort order (the "current priority logic")

Eligibility (both queries):
- `CUSTOMER_ORDER.STATUS = 'R'` (released) and `CUST_ORDER_LINE.LINE_STATUS = 'A'` (active)
- `CUSTOMER_ENTITY.CREDIT_STATUS = 'A'` (credit hold excluded)
- Not RMA: `SALESREP_ID <> 'RMA'`, `CUSTOMER_PO_REF NOT LIKE '%RMA%'`
- Customer `DISCOUNT_CODE` not like `%International%` / `%Employee%`
- Guns only: configurable excluded-customer name terms
- Open qty `ORDER_QTY − TOTAL_SHIPPED_QTY > 0`
- Horizon: **header** `DESIRED_SHIP_DATE ≤ today + lookahead` (NULL → treated as today)

Demand sort (identical in both queries and in shortage):

```sql
ORDER BY
    CASE WHEN header_DESIRED_SHIP_DATE < today THEN 0 ELSE 1 END,  -- past due first
    header_DESIRED_SHIP_DATE,
    ORDER_DATE,
    CUST_ORDER_ID,
    LINE_NO
```

`PROMISE_DATE` and `PROMISE_DEL_DATE` are **selected and displayed but never used** for
ordering or filtering.

> **Update 2026-07-29:** the **guns** query was changed to the proposed ordering —
> `PROMISE_DEL_DATE` (NULLS LAST) → `PROMISE_SHIP_DATE` (NULLS LAST) →
> `DESIRED_SHIP_DATE_NORM` → `ORDER_DATE` → SO → line, and its demand **horizon** now uses
> the same fallback chain — `COALESCE(PROMISE_DEL, PROMISE_SHIP, DESIRED, today) ≤
> today + lookahead` — so C1 and C2 are resolved for guns. Measured impact on switch day:
> +316 lines / +688 units entered scope (slipped desired dates no longer hide them),
> −24 lines / −85 units left scope (explicit future Promise Del — customer won't accept
> yet). The sort and horizon described above still apply to `query_components.sql` and
> `shortage_components.sql`, which are unchanged.

---

## 4. Conflicts between the existing system and the intended rules

| # | Conflict | Impact |
|---|---|---|
| C1 | **Picklist priority key is the header `DESIRED_SHIP_DATE`, not `PROMISE_DEL_DATE`.** | The intended Inside-Sales priority lever does nothing today. Worse: since scheduling mass-updates desired dates (all identical on the sample SKU), the primary sort key is a constant within a SKU and priority collapses to `ORDER_DATE` — effectively first-come-first-served, controlled by nobody. |
| C2 | **Horizon filter also uses header `DESIRED_SHIP_DATE`.** | An order Inside Sales wants shipped now is invisible to the picklist until operations' date enters the window; conversely orders the customer can't accept yet still consume supply. |
| C3 | The picklist comment says "DESIRED_SHIP_DATE drives MRP" but it reads the **header** date, while the business says the **line** date (Prod Date) is the material-planning date. | The stated rationale for the current sort key doesn't match the stated ownership model. Needs business confirmation of which date MRP actually consumes. |
| C4 | `PROMISE_DEL_DATE` lives mostly at **header** level; the intended workflow ("reprioritize this SKU's line") needs a **line-level** value. Coalesce order matters and a header edit would move every line on the order. | Edit UX must be explicit about scope (see Q1). |
| C5 | Blank `PROMISE_DEL_DATE` has no defined meaning today. | Proposed logic must define NULL ordering deterministically (recommendation below). |
| C6 | Changing the picklist's ORDER BY to Promise-Del-first is a **behavior change to a production process** (picking, shortage reconciliation both share the ordering). | Must be phased and signed off; the new screen should first *model* the new priority alongside the current one. |

No conflict found on: promise dates being auto-moved (nothing in this repo writes them);
double-allocation (single deterministic pass); VISUAL native allocation (unused for stock).

---

## 5. Proposed allocation algorithm

Compute per SKU, in the app (Python), read-only, on demand. Reuse the cumulative-range
technique already proven in SQL, but in code so previews can run against hypothetical dates.

### Supply events (chronological, tagged by certainty)

```
1. ON_HAND    qty from picklist-eligible bins (guns rules for gun SKUs), date = today, certainty = AVAILABLE
2. WO_RELEASED per open released WO: remaining qty, date = COALESCE(SCHED_FINISH, DESIRED_WANT), certainty = RELEASED
3. WO_FIRMED   same for STATUS='F', certainty = FIRMED
4. MPS        MASTER_SCHEDULE buckets with WANT_DATE > (netting fence), qty netted per Q2, certainty = PLANNED
```

Sort by date, then certainty rank (AVAILABLE < RELEASED < FIRMED < PLANNED on ties), then
supply id. Never merge classes: every allocated unit remembers which event feeds it.

**Netting guard (until Q2 is answered):** exclude any MPS bucket whose `WANT_DATE` is on or
before the latest open-WO want date for the SKU, and show a "netting fence" marker in the
UI. This prevents the worst error — counting the same production twice — at the cost of
possibly understating near-term planned supply, which is the safe direction.

### Demand priority (proposed, per SKU)

```
eligible lines only (same filters as picklist §3, minus the desired-date horizon)
ORDER BY
    EFFECTIVE_PROMISE_DEL  NULLS LAST,        -- COALESCE(line.PROMISE_DEL_DATE, header.PROMISE_DEL_DATE)
    EFFECTIVE_PROMISE_SHIP NULLS LAST,        -- COALESCE(line.PROMISE_DATE, header.PROMISE_DATE)
    header.ORDER_DATE,
    CUST_ORDER_ID,
    LINE_NO
```

- NULLS LAST: a blank Promise Del means "no Inside-Sales priority claim" and queues behind
  every dated line (deterministically, via the remaining keys). Alternative (treat NULL as
  order date) rejected: it would let unmanaged orders jump managed ones.
- Ineligible lines (credit hold, order not released, line closed, RMA, excluded class) are
  **displayed with explicit reason badges but excluded from allocation**.
- Open units = `ORDER_QTY − TOTAL_SHIPPED_QTY` (handles partial shipments); a multi-unit
  line may span supply events and shows one row per contributing event.
- Make-to-order pegging (mirror of supply case 16): qty pegged to this line via
  `DEMAND_SUPPLY_LINK` (open WOs only) is netted out of the line's pool demand — the
  pegged WO is already excluded from the shared pool, so without the net-out a released
  linked line would double-dip and steal a shared unit. Fully pegged lines get
  `supply_status = LINKED`, est. availability from the WO finish date, and a
  `LINKED WO <base/lot> (<status>)` badge; the badge also shows on ineligible lines
  (e.g. firmed SO with a released linked WO) so the "not released" question answers itself.

### Allocation
Single pass, cumulative ranges: demand line occupies units `[cumStart, cumEnd)` of the
priority sequence; supply event covers units `[supStart, supEnd)`; overlap = allocation.
Output per line: position (1..n by line order), fulfilling event(s), estimated availability
date = date of the **last** unit's supply event, certainty = **lowest** certainty among its
events, or **NO SUPPLY** if demand extends past total supply.

Label everywhere: **"Estimated availability — based on <source>"**, never "ship date."

### Preview & suggest
- Preview: re-run the sort/allocation in memory with the hypothetical Promise Del value.
  No writes.
- "Suggest a date for position N": take the effective Promise Del of the line currently at
  position N; suggested date = that date minus 1 day (or equal, if the user's remaining
  tie-breakers already win). Show the resulting position before saving. No position field
  is ever stored — Promise Del + documented tie-breakers remain the source of truth.

---

## 6. Proposed data model and query structure

### Reads (new files under `sql/`)
- `alloc_supply.sql` — UNION ALL of the four supply classes for `:part_id`.
- `alloc_demand.sql` — all open CO lines for `:part_id` with all four dates (header + line
  raw values *and* coalesced effective values), customer, status, credit status, and each
  eligibility flag as its own column (so the UI can badge reasons, not just yes/no).
- Both parameterized (`:part_id`), read-only, joined/allocated in Python.

### Write path (Phase 2)
One statement per save, in a transaction, via a dedicated SQL login whose only write grant
is column-level: `GRANT UPDATE (PROMISE_DEL_DATE) ON dbo.CUST_ORDER_LINE TO picklist_writer`
(plus, only if Q1 chooses header edits, the same on `CUSTOMER_ORDER`):

```sql
UPDATE dbo.CUST_ORDER_LINE
SET    PROMISE_DEL_DATE = :new_value
WHERE  CUST_ORDER_ID = :so AND LINE_NO = :line
  AND  ((PROMISE_DEL_DATE = :old_value) OR (PROMISE_DEL_DATE IS NULL AND :old_value IS NULL))
```

- **Optimistic concurrency:** 0 rows affected ⇒ someone (this app or a VISUAL user) changed
  it since the screen loaded ⇒ rollback, reload, re-prompt. (`CUST_ORDER_LINE` has no
  rowversion column; old-value compare is the mechanism.)
- Explicitly out of scope for the write path: `PROMISE_DATE`, `DESIRED_SHIP_DATE` (both
  levels), work orders, master schedule, `DEMAND_SUPPLY_LINK`, `ALLOCATED_QTY`. The
  column-level grant makes this a hard guarantee, not a code convention.

### Audit trail (Postgres, following `audit_store.py` patterns)

```sql
CREATE TABLE promise_del_audit (
    id             bigserial PRIMARY KEY,
    changed_at     timestamptz NOT NULL DEFAULT now(),
    changed_by     text NOT NULL,          -- see Q4 (user identity)
    cust_order_id  text NOT NULL,
    line_no        int  NOT NULL,
    part_id        text NOT NULL,
    old_value      date,
    new_value      date,
    reason         text,
    position_before int,                   -- computed snapshot, informational only
    position_after  int
);
```

Write the ERP update and the audit row in that order; if the audit insert fails, roll back
the ERP transaction too (save fails loudly rather than losing the trail).

---

## 7. UI / component plan

New route `GET /allocation` + JSON APIs (`/api/allocation/<part_id>`,
`POST /api/allocation/promise-del`), template `templates/allocation.html` following the
existing server-rendered + fetch style and `_topnav.html`.

Layout, top to bottom:
1. **SKU search** with part autocomplete (id + description + on-hand badge).
2. **Supply rail** — horizontal timeline of supply events, color-coded by certainty:
   - green `ON HAND`, blue `RELEASED WO` (WO id, qty, want date), amber `FIRMED WO`,
     purple striped `MASTER SCHEDULE` (explicitly styled as *less certain*: dashed border +
     "planned" tag), grey `NETTING FENCE` marker.
3. **Demand table** — one row per open line: position #, SO, customer, order date, open/
   shipped qty, all four dates (raw header + line values on hover; effective value shown),
   supply-event chip(s) with certainty color, estimated availability date, eligibility
   badges (`CREDIT HOLD`, `ORDER NOT RELEASED`, `RMA`, `EXCLUDED`, `NO SUPPLY` in red).
4. **Reprioritize drawer** (select a row): current position, Promise Del editor,
   "target position" helper that fills a suggested date, **Preview** button rendering the
   would-be table (moved row highlighted, displaced rows marked ▼), then **Save** (reason
   optional, shown in audit tab). After save: auto-refresh and flash the row's move
   (e.g. 10 → 3).
5. **History tab** — recent `promise_del_audit` rows for the SKU/order.

Everywhere the projected date appears it is labeled "est. availability" with its source —
never presented as a committed ship date.

---

## 8. Assumptions and unresolved business questions

Assumptions made (flag if wrong):
- A1. Single site (`TDJ`); site filtering deferred.
- A2. "Available on-hand" for guns = the guns-picklist bin rules (SHIPPING R01–R09). Stock
  elsewhere (e.g. MAIN) is shown informationally but not allocated.
- A3. Purchased supply (POs) is out of scope for these SKUs (fabricated firearms).
- A4. `MASTER_SCHEDULE_ID = 'STANDARD'` is the active schedule.

Open questions (need answers before Phase 2):
- **Q1 — Edit scope:** write `PROMISE_DEL_DATE` at line level (recommended: per-SKU
  precision; header value continues to serve as default via coalesce) or header level
  (matches current data-entry practice but moves every line on the order)?
- **Q2 — MPS netting:** exact rule for when a master-schedule bucket is "consumed" by
  released WOs. Interim guard in §5 understates planned supply on purpose.
- **Q3 — Should the picklist itself adopt the Promise-Del-first ordering** (and should its
  horizon switch from header desired date to Promise Del)? This is the real behavior change
  (C1/C2) and needs Ops + Inside Sales sign-off.
- **Q4 — User identity:** the app has no per-user login. Options: require a username +
  per-user PIN for this screen (minimum), or Windows/Entra SSO. `changed_by` must be a real
  person for the audit trail to satisfy the requirement.
- **Q5 — `CUST_LINE_DEL`:** are delivery schedules used at all? Every current query ignores
  them. If some orders carry multi-delivery schedules, per-delivery dates would refine the
  model (VISUAL's own `CUST_ORDER_ALLOC` table is keyed to delivery lines).
- **Q6 — Does MRP consume header or line desired date** (C3)? Determines what warning to
  show users about the relationship between Prod Date and the allocation view.
- **Q7 — Should credit-hold demand reserve supply?** Current picklist excludes it entirely;
  proposed default keeps it excluded but visible. Inside Sales may prefer "reserve but
  don't release."

---

## 9. Phased implementation plan

- **Phase 0 — Read-only visibility screen** (no ERP writes, no behavior change):
  SKU search, supply rail, demand table with computed positions under the *proposed*
  ordering, plus a toggle to view the *current picklist* ordering so the two can be
  compared side by side. Ship quickly; this alone answers most of Inside Sales' questions.
- **Phase 1 — Preview & suggest:** hypothetical Promise Del editor with preview and
  suggested-date helper. Still no writes.
- **Phase 2 — Save with audit:** column-level-granted SQL login, optimistic concurrency,
  Postgres audit trail, user identity (Q4 resolved), permission gate.
- **Phase 3 — Picklist alignment (optional, gated on Q3):** change picklist/shortage
  ORDER BY (and possibly horizon) to the Promise-Del-first rule, in lockstep across
  `query_guns.sql`, `query_components.sql`, `shortage_components.sql`, with a parallel-run
  comparison period before cutover.

## 10. Test cases (pre-production)

Fixture-driven tests of the allocation engine (pure function: supply events + demand lines
→ allocation), mirroring `tests/`:

1. One order, one on-hand unit → position 1, AVAILABLE, date = today.
2. Multiple orders, distinct Promise Del dates → strict date order.
3. Tied Promise Del dates → deterministic by Promise Ship, then order date, SO, line — and
   stable across repeated runs.
4. Blank Promise Del → sorts after all dated lines; never before.
5. Held order (credit status ≠ 'A') → displayed, badged, allocated nothing, and its supply
   goes to the next eligible line.
6. Partially shipped line (e.g. SO-118293: 5 ordered / 3 shipped) → open qty 2 only.
7. Multi-unit line spanning supply events → split allocation; est. date = last unit's
   event; certainty = weakest contributing source.
8. Released-WO supply (remaining = desired − received; received-partial WO reduces supply).
9. Firmed-WO supply ranks behind released on same-date ties.
10. Master-schedule supply appears only beyond the netting fence and is labeled PLANNED.
11. WO date slips after a save → positions unchanged, estimated dates move (recompute).
12. Concurrent edit: stale old-value → save rejected, screen reloads, no partial write.
13. Position 10 → 3 workflow: suggested date lands the order at position 3 after save.
14. Earlier Promise Ship but later Promise Del → Promise Del wins (position follows Del).
15. Supply insufficient → tail lines flagged NO SUPPLY, never a fabricated date.
16. Inventory reserved elsewhere (excluded bins / MAIN warehouse / `DEMAND_SUPPLY_LINK`
    rows, e.g. RMA-linked WOs) → not counted as allocatable supply.
17. Audit row written atomically with the update; rollback on audit failure.
18. Write-permission test: the app login cannot update any column other than
    `PROMISE_DEL_DATE` (verifies the column-level grant).
