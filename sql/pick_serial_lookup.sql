/*
===============================================================================
  PICK CONFIRM — SERIAL RESOLUTION
===============================================================================
  Resolve one scanned serial number to its part(s) and current inferred
  on-hand location(s). Used by the pick-confirm scan endpoint to check that
  the firearm a picker just pulled is actually on the day's picklist.

  Current location is inferred the same way as the serialized audit
  (sql/audit_serialized_inventory.sql): sum TRACE_INV_TRANS.QTY per
  (part, serial) grouped by the linked INVENTORY_TRANS warehouse/location,
  HAVING SUM(qty) > 0. A serial that has fully shipped or is in WIP returns
  its trace row(s) with NULL location — the app reports those as
  "not on hand".

  One row per (part, on-hand location); a serial with no on-hand balance
  still returns one row per matching trace record (location columns NULL).

  Bind parameters:
    :serial — the serial number to look up (exact match, uppercased by app).

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    t.PART_ID,
    t.ID                   AS TRACE_ID,
    p.DESCRIPTION          AS PART_DESCRIPTION,
    p.PRODUCT_CODE,
    loc.WAREHOUSE_ID,
    loc.LOCATION_ID,
    loc.NET_QTY
FROM dbo.TRACE t
LEFT JOIN dbo.PART p
    ON p.ID = t.PART_ID
OUTER APPLY (
    SELECT
        it.WAREHOUSE_ID,
        it.LOCATION_ID,
        SUM(tit.QTY) AS NET_QTY
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON  it.TRANSACTION_ID = tit.TRANSACTION_ID
        AND it.PART_ID        = tit.PART_ID
    WHERE tit.PART_ID  = t.PART_ID
      AND tit.TRACE_ID = t.ID
    GROUP BY it.WAREHOUSE_ID, it.LOCATION_ID
    HAVING SUM(tit.QTY) > 0
) loc
WHERE t.ID = :serial
   OR t.SERIAL_ID = :serial
ORDER BY t.PART_ID, loc.WAREHOUSE_ID, loc.LOCATION_ID;
