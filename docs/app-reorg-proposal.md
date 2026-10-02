# App reorganization proposal

Status: phases 1 to 3 implemented 2026-10-02. Phase 4 (URL cleanup) not started.

## The problem

Nine top-level tabs, and the Shipping tab hides ten more sub-views. Nothing in
the nav tells an operator which page to open to *do* their job versus which
page just *tells them something*. Three sub-views duplicate top-level tabs.

Current nav and what each thing actually is:

| Nav item | Route | Really is |
|---|---|---|
| Dashboard | `/` | Run picklist + exports, plus banners into scorecard/excess/shortages, plus "advanced" clutter (overrides, time-zone check, recent runs, current picklist table) |
| Shipping > Overview | `/shipping?view=work` | "Start today's work" launcher |
| Shipping > Pick orders | `/shipping?view=pick` | Do-work |
| Shipping > Verify boxes | `/shipping?view=verify` | Do-work |
| Shipping > Scorecard | `/shipping?view=scorecard` | Report |
| Shipping > Holds | `/shipping?view=holds` | Report. **Duplicates Orders tab** |
| Shipping > Requests | `/shipping?view=requests` | Queue. **Duplicates Requests tab** |
| Shipping > Staged / Shortages / Recon / Excess | `/shipping?view=...` | Reports |
| Orders | `/orders` | Lookup ("What is holding orders up?") + hold acks |
| Requests | `/requests` | Cross-team queue (Sales asks Shipping, with a clock) |
| Shipments | `/shipments` | Lookup ("Did it ship? Tracking?") |
| Stock | `/stock` | Lookup ("Do we really have one, and where?") |
| Audit | `/audit` | Do-work (start sessions) + report (recent audits) + `/audit/analytics` |
| Serial Lookup | `/serial-history` | Lookup |
| Allocation | `/allocation` | Lookup + what-if |
| Settings | `/settings` | Config, buried behind an "advanced" toggle on Dashboard |

## Proposed shape: four tabs, by intent

```
 Work            Reports              Lookup                    Requests (3)   [who am I]  [Settings]
 ───────────     ─────────────────    ─────────────────────     ────────────
 Run picklist    Scorecard            Order        (/orders/<id>)
 Pick orders     Holds                Shipment     (/shipments)
 Verify boxes    Shortages            Stock        (/stock)
 Audit           Reconciliation       Serial       (/serial-history)
                 Excess packlists     Allocation   (/allocation)
                 Staged shipments
                 Audit analytics
                 Run history
```

Requests is its own tab because Inside Sales lands there as often as Shipping
does. It carries a badge with the count of open requests (status open,
acknowledged, or in_progress). The badge turns red when any open request is
past its SLA. The count comes from one cheap query against the request store
on every page render, so it stays current without JavaScript polling.

### Work: "I need to do something"

Landing page is a single **Today** screen (today's `/shipping?view=work`,
promoted). Four big entry cards, in the order the day actually runs:

1. **Run picklist**: the run buttons and the download links from today's
   Dashboard. Live run status lives here. Nothing else.
2. **Pick orders**: open sessions to resume, plus start a new one.
3. **Verify boxes**: same pattern.
4. **Audit**: due-for-audit banner and start buttons.

Plus a thin strip of open requests addressed to Shipping with the clock
running, linking into the Requests tab.

The "advanced" clutter on today's Dashboard goes away: query overrides and the
time-zone check move to Settings, recent runs and the current picklist table
move to Reports > Run history.

### Reports: "Is it working?"

Everything currently in `/shipping?view=` that only reads. Same sub-nav
toolbar as today, just without the do-work items mixed in. Scorecard is the
default. Audit analytics joins this group so it stops hiding behind a button
on the Audit page.

The Orders page is a report too, but it carries an action (hold ack), so it
is listed in Reports as **Holds** and in Lookup as **Order**. Same route.

### Lookup: "Answer a question"

One page with one search box. Paste whatever is in hand and the page opens
the right lookup with that value already searched. The dispatch is a regex on
the input shape, done in the browser, no server call:

| You paste | Shape | Opens |
|---|---|---|
| `SO-131844` or `131844` | `SO-?\d{5,6}` | `/orders/SO-131844` |
| UPS tracking | `1Z[0-9A-Z]{16}` | `/shipments?q=…` |
| Packlist id | packlist prefix pattern | `/shipments?q=…` |
| Part id | part-id pattern | `/stock?part=…` with a link to `/allocation?part=…` |
| Anything else serial-shaped | fallback | `/serial-history?serial=…` |
| No match | | Landing page with the text pre-filled in all five cards |

The five existing pages stay as they are, with one small hook each: on load,
if the URL carries the page's parameter, fill the box and fire the search the
page already runs on submit. Serial history already fetches with `?serial=`
and stock already fetches with `?q=`, so those are a few lines. Shipments and
allocation need the same hook added. Order detail already takes the id in
the path.

Allocation lives here. It answers "what supply do I have for this SKU and who
is in line for it", and the what-if tool is part of answering that.

The landing page also shows five cards linking directly to each lookup, so a
person who knows what they want can skip the box.

## What gets deleted or merged

- `/shipping?view=holds` → redirect to `/orders`.
- `/shipping?view=requests` → redirect to `/requests`.
- `/shipping?view=work` → becomes `/work` (the Today page). `/` redirects here.
- Dashboard banners (scorecard / excess / shortages) → gone. Reports owns them.
- Audit page splits: start-session half stays in Work, "recent audits" table
  moves next to Audit analytics in Reports.
- `SHIPPING_VIEWS` dict in `app.py` splits into `WORK_VIEWS` and `REPORT_VIEWS`.
- New context processor supplies `open_request_count` and `overdue_request_count`
  for the Requests badge, using the existing open-status query in
  `request_store.py`.

## What does not change

- No SQL, store, or service code. This is templates, `_topnav.html`, route
  wiring, and redirects.
- Feature flags keep working. `shipping`, `orders`, `audit`, `serial`,
  `allocation` map onto sub-items instead of top tabs. A flag that is off
  hides the item; a tab with no visible items hides itself.
- Operator picker stays in the header.

## Phasing

1. **Nav only** (small). New `_topnav.html` with the four tabs, sub-navs,
   and the Requests badge.
   Existing routes untouched. Dashboard gets its clutter moved to Settings
   and Reports. Shipping sub-nav loses Holds and Requests (redirects).
2. **Today page** (medium). `/work` built from the current `view=work`
   template plus the run-picklist card lifted from `index.html`. `/` redirects.
3. **Lookup router** (small). `/lookup` page with the search box and the
   input-type dispatch, plus the on-load parameter hook in each lookup page.
4. **URL cleanup** (optional, later). `/shipping?view=recon` →
   `/reports/recon` etc. Only worth doing if bookmarks are not a concern.

## Decisions so far

- Requests is its own top-level tab with an open-count badge. Decided 2026-10-02.
- Allocation lives under Lookup. Decided 2026-10-02.
- Lookup search router: proposed as phase 3, pending go/no-go.

## Implemented 2026-10-02

- Four-tab nav in `templates/_topnav.html` with a per-group sub-nav row,
  Settings in the header, and the Requests badge fed by
  `request_store.open_counts()` through the `inject_request_badge` context
  processor in `app.py`.
- `/work` is the Today page (`templates/work.html`). `/runs` is Run history.
  `/lookup` is the search router.
- `/shipping?view=work|holds|requests` redirect to `/work`, `/orders`,
  `/requests`. Unknown views fall back to the scorecard. The in-page view
  toolbar on the Shipping pages is gone; the sub-nav replaces it.
- The Active manual holds table moved from the Shipping Requests view to the
  Requests page.
- The run page (`/`) lost its scorecard, excess, transfer and audit banners
  and the Settings row.

Deviations from the plan above, on purpose:

- `/` still serves Run picklist rather than redirecting to `/work`. Too many
  export links and run redirects point at the `index` endpoint to move it in
  this pass; it is the phase 4 URL cleanup.
- Guns query overrides, the time-zone check, and the current picklist table
  stayed on the run page. Overrides are run parameters, not settings, and the
  other two are run diagnostics. Only the recent-runs table was promoted to
  Reports > Run history.
- The recent audits table stayed on the Audit page, where it doubles as the
  list of sessions to resume.
