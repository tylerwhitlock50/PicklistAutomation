# Barreled actions on the firearms picklist — decision record

Requested by Robert Schwemmer (Shipping and Logistics Manager), 2026-10-02:
barreled actions should ship as regular firearms and appear on the guns
picklist, not the components picklist.

## Decision (Tyler Whitlock, 2026-10-02): no code change — relocate the stock

The picklists split by bin on purpose. The bin encodes which team owns the
pick: SHIPPING racks R01–R09 are the firearms pallet racks, DISTRIBUTION and
SHIPPING R11 are the small-box component area. If barreled actions are to ship
as firearms they belong in the firearms area, so Shipping moves them there and
the system follows with no change.

## Why they land on the components list today

| Picklist | Supply predicate (`sql/query_*.sql` `Supply` CTE) |
|---|---|
| guns | SHIPPING warehouse, racks R01–R09, not STAGE / INTERNATIONAL |
| components | DISTRIBUTION (not OVERSTOCK/STOCK) **or** SHIPPING rack R11 |

All barreled-action stock (product code `FG-BARACT`, 31 units across 9 parts)
sits in SHIPPING R11S03 / R11S04. No product-code rule is involved.

The release gate, readiness and order detail already classify `FG-BARACT` as
guns by product code, but the gate's gun supply branch only looks at R01–R09.
Today it sees zero supply for them, so the move also fixes that mismatch.

## What Shipping must do

1. Move the barreled actions to a firearms pallet rack in SHIPPING, any
   location whose ID starts R01 through R09. Not R10, not R11, not a STAGE or
   INTERNATIONAL bin.
2. Record the move in VISUAL as an inventory transfer to the new location.
   The picklist reads `PART_LOCATION`, so a physical move alone changes
   nothing.
3. Put future barreled actions away in the same racks. Any unit put back in
   R11 reappears on the components list.
4. Optional: do the same for bare actions (`FG-ACTIONS`, 1 unit in R11). They
   are serialized firearms with the same issue.

## What happens after the move

- Guns picklist picks them from the new rack; components list no longer sees
  them.
- Gun rules apply: gun lookahead window, the `CA MARK` customer exclusion,
  serial scanning in the pick session, serial reservation in the release gate.
  All 31 units on hand carry serials, so serial picking works.
- Orders that mix barreled actions with components split across the two
  picklists, as rifle + components orders already do (3 open lines today).
- Stock page classifies the new bins as pickable.

## Verification after the move

```sql
SELECT pl.WAREHOUSE_ID, pl.LOCATION_ID, COUNT(DISTINCT pl.PART_ID) AS PARTS, SUM(CAST(pl.QTY AS int)) AS QTY
FROM dbo.PART_LOCATION pl
JOIN dbo.PART p ON p.ID = pl.PART_ID
WHERE p.PRODUCT_CODE IN ('FG-BARACT', 'FG-ACTIONS') AND pl.QTY > 0
GROUP BY pl.WAREHOUSE_ID, pl.LOCATION_ID
ORDER BY pl.WAREHOUSE_ID, pl.LOCATION_ID;
```

Expect every SHIPPING row to start R01–R09 and no R11 rows. Then run the guns
picklist and confirm the open `FG-BARACT` lines (13 lines, 6 inside the
10-day horizon on 2026-10-02) appear there and not on the components run.

## Rejected alternative

A product-code carve-out (`FIREARM_PRODUCT_CODES` token in the four supply
CTEs plus `classify_bin`) would make the list follow the part regardless of
bin. Rejected because it blurs the ownership boundary the bins represent.
