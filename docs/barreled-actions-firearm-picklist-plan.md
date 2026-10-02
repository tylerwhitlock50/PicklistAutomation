# Barreled actions on the firearms picklist — plan

Requested by Robert Schwemmer (Shipping and Logistics Manager), 2026-10-02:
barreled actions should ship as regular firearms and appear on the guns
picklist, not the components picklist.

## Root cause

The two picklists are split by **bin location**, not by what the part is.

| Picklist | Supply predicate (`sql/query_*.sql` `Supply` CTE) |
|---|---|
| guns | SHIPPING warehouse, racks R01–R09, not STAGE / INTERNATIONAL |
| components | DISTRIBUTION (not OVERSTOCK/STOCK) **or** SHIPPING rack R11 |

Barreled actions carry product code `FG-BARACT`. Every unit on hand sits in
SHIPPING R11 (R11S03, R11S04), so the location rule drops them on the
components list. Nothing about the part itself is consulted.

The release gate, readiness and order detail already classify lines by
product code (`ITEM_TYPE` = components when the code is in
`SHORTAGE_PRODUCT_CODES`, otherwise guns). `FG-BARACT` is not in that list, so
those screens already call barreled actions guns. But the gate's gun supply
branch (`release_candidates.sql` `EligibleSupply`, `release_serial_supply.sql`)
only looks at R01–R09, so it sees **zero** available supply for them. Today
the gate and the picklist disagree on barreled actions; this change fixes both.

## Live ERP facts (2026-10-02)

| Fact | Value |
|---|---|
| `FG-BARACT` parts in VISUAL | 73 |
| On hand, SHIPPING R11 | 31 units across 9 parts, all 31 serialized |
| On hand, R01–R09 or DISTRIBUTION | 0 |
| Open released SO lines for `FG-BARACT` | 13 lines, 13 orders, 3 customers, 33 units |
| Of those, inside the 10-day picklist horizon | 6 lines |
| Of those, on orders that also carry component lines | 3 lines |
| `FG-ACTIONS` (bare actions, also serialized) on hand in R11 | 1 unit |

Serialization matters because the gun pick session scans serials, while the
components session falls back to UPC. Every barreled action on hand has a
serial, so gun-style picking works without data cleanup.

## Design

Make the split product-code aware with one new setting:

```
FIREARM_PRODUCT_CODES=FG-BARACT        # env var, comma separated, default shown
```

Rule: a part whose product code is in `FIREARM_PRODUCT_CODES` is a firearm
**wherever it sits in SHIPPING**, so its R11 stock is gun supply. Everything
else keeps today's location rule. Rendered into SQL through a new token
`__FIREARM_PRODUCT_CODES__` (same `SELECT ... UNION ALL` row pattern as
`__COMPONENT_PRODUCT_CODES__`).

Why not just move the parts to R01–R09? That works with zero code and Robert
could do it today, but R11 is where Shipping chose to keep them and the
system would break again the moment a unit is put away in R11. The code
change makes the list follow the part. Both can be done; the physical move is
optional.

## Changes

### SQL (keep the four supply CTEs in lockstep; the comments already say so)

1. `sql/query_guns.sql` `Supply`: add
   `OR (WAREHOUSE_ID='SHIPPING' AND LEFT(LOCATION_ID,3)='R11' AND product code IN FirearmProductCodes)`.
   Needs a join to `dbo.PART` and a `FirearmProductCodes` CTE.
2. `sql/query_components.sql` `Supply`: on the R11 branch add
   `AND product code NOT IN FirearmProductCodes` so the same unit cannot appear
   on both lists and be allocated twice.
3. `sql/release_candidates.sql` `EligibleSupply` gun branch: mirror item 1.
4. `sql/release_serial_supply.sql`: widen the location filter to
   `R01–R09 OR (R11 AND part in FirearmProductCodes)` so the gate can reserve
   barreled-action serials. The part filter token already limits this to gun
   parts.

### Python

5. `app.py`: add `FIREARM_PRODUCT_CODES` + `FIREARM_PRODUCT_CODES_TOKEN`
   next to `SHORTAGE_PRODUCT_CODES`; a small `firearm_product_code_rows()`
   helper; render the token in `load_query()` for **both** picklist types (the
   components template is returned raw today), in `render_release_query()`,
   and wherever `release_serial_supply.sql` is rendered.
6. `readiness.py` `classify_bin(warehouse, location)`: add an optional
   `product_code` argument; R11 returns `pickable` when the code is a firearm
   code. Update the two callers in `stock.py` and the one in `readiness.py`
   to pass the product code they already have. This keeps the Stock page and
   order part-location view from labelling barreled-action bins "R11
   components" while the picklist treats them as pickable guns.
7. `scripts/validate_shipping_management.py`: render the new token so the
   validation script still runs.

### Docs and tests

8. README picklist section and the "KEEP IN LOCKSTEP" comments: describe the
   product-code carve-out.
9. Tests: token rendering for both query types (`load_query` must leave no
   `__FIREARM_PRODUCT_CODES__` behind), `classify_bin` R11 + firearm code →
   `pickable`, R11 + other code → `r11_components`, and a release-gate case
   where an R11 barreled action counts as gun supply. Existing tests in
   `tests/test_release_gate.py`, `tests/test_stock.py`,
   `tests/test_readiness.py` are the models.

## Side effects to accept

- Barreled-action lines now follow **gun rules**: gun lookahead window, the
  `CA MARK` (and any added) customer exclusions, serial scanning in the pick
  session, serial reservation in the release gate, and the FFL document checks.
  This is what "regular firearms shipping" means and is the intent.
- The 3 open orders that mix barreled actions with components will split
  across the two picklists, exactly as a rifle + components order does today.
- The components run's shortage note is unaffected (`FG-BARACT` was never in
  `SHORTAGE_PRODUCT_CODES`).
- Audit sessions (`audit_serialized_inventory.sql`) are MAIN-only and unchanged.

## Decisions for Tyler

1. **Include `FG-ACTIONS` (bare actions) in the default?** They are serialized
   firearms with the same R11 problem (1 unit on hand). Robert only named
   barreled actions. Recommendation: include it, since a bare action ships
   as a firearm too; easy to drop if Shipping disagrees.
2. **Physical move as well?** Optional. The code change alone fixes the list.

## Rollout

1. Implement on a branch, run `pytest`.
2. Run both picklists in advisory mode against live ERP; confirm the 6
   in-horizon barreled-action lines move from the components output to the
   guns output and appear nowhere twice.
3. Confirm the release gate shows non-zero available qty and serial
   candidates for those lines.
4. Reply to Robert once deployed.

Note: the working tree has uncommitted readiness/orders work (supply-hold
hiding). This change touches `readiness.py` too, so land or stash that first.
