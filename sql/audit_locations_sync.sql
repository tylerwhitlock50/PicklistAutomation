/*
===============================================================================
  SERIALIZED AUDIT — LOCATION SYNC
===============================================================================
  Lists every warehouse/location currently holding serialized inventory in the
  audited universe (MAIN cage bins + the whole SHIPPING warehouse), with the
  location description and current serial count. Used to keep the app's
  auditable-location list (Postgres audit_locations) in sync with the ERP.

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

WITH TraceLoc AS (
    SELECT
        tit.PART_ID,
        tit.TRACE_ID,
        it.WAREHOUSE_ID,
        it.LOCATION_ID,
        SUM(tit.QTY) AS NET_QTY
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID        = tit.PART_ID
    GROUP BY tit.PART_ID, tit.TRACE_ID, it.WAREHOUSE_ID, it.LOCATION_ID
    HAVING SUM(tit.QTY) > 0
)
SELECT
    tl.WAREHOUSE_ID,
    tl.LOCATION_ID,
    l.DESCRIPTION,
    COUNT(*) AS SERIAL_COUNT
FROM TraceLoc tl
LEFT JOIN dbo.LOCATION l
    ON  l.WAREHOUSE_ID = tl.WAREHOUSE_ID
    AND l.ID           = tl.LOCATION_ID
WHERE (tl.WAREHOUSE_ID = 'MAIN' AND tl.LOCATION_ID IN ('C2', 'C2-SERIALIZED'))
   OR tl.WAREHOUSE_ID = 'SHIPPING'
GROUP BY tl.WAREHOUSE_ID, tl.LOCATION_ID, l.DESCRIPTION
ORDER BY tl.WAREHOUSE_ID, tl.LOCATION_ID;
