-- dispatch.inventory-balances: complete per-bin stock, including zero/held rows.
-- Availability filters are deliberately absent: hiding held/negative stock
-- would produce a misleading opening/reconciliation snapshot.
SELECT pl.PART_ID, psv.STOCK_UM, pl.QTY, pl.STATUS, pl.LOCKED, pl.TRANSIT,
       pl.HOLD_REASON_ID, tp.PART_ID AS TRACE_PROFILE_PART_ID
FROM dbo.PART_LOCATION pl
JOIN dbo.WAREHOUSE w ON w.ID = pl.WAREHOUSE_ID
LEFT JOIN dbo.PART_SITE_VIEW psv ON psv.SITE_ID = w.SITE_ID AND psv.PART_ID = pl.PART_ID
LEFT JOIN dbo.TRACE_PROFILE tp ON tp.SITE_ID = w.SITE_ID AND tp.PART_ID = pl.PART_ID
WHERE w.SITE_ID = :site AND pl.WAREHOUSE_ID = :warehouse AND pl.LOCATION_ID = :location
ORDER BY pl.PART_ID;
