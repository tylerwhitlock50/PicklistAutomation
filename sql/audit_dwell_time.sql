/*
===============================================================================
  SERIALIZED INVENTORY — LOCATION DWELL TIME
===============================================================================
  How long each serialized firearm has been sitting in its current shipping
  bin. One row per (serial, location) currently on hand.

  Current location is INFERRED the same way as the audit expected list
  (sql/audit_serialized_inventory.sql): sum TRACE_INV_TRANS.QTY per
  (part, serial) grouped by the linked INVENTORY_TRANS warehouse/location,
  keep HAVING SUM(qty) > 0.

  ARRIVED_AT is the CREATE_DATE of the serial's most recent INBOUND
  transaction (TRACE_INV_TRANS.QTY > 0) into that same warehouse/location —
  i.e. the start of its current stint there. CREATE_DATE is used instead of
  TRANSACTION_DATE because it carries a real timestamp (TRANSACTION_DATE is
  effectively date-only), and the dwell target is measured in hours.
  If a gun left the bin and came back, only the latest arrival counts.

  Scope: every bin the shipping audit covers —
      MAIN / C2, C2-SERIALIZED, C2-ALLOCATED
      SHIPPING / *  (racks, stage rows, international, ...)
  The app filters this down to the locations it watches for the dwell/aging
  metric (AUDIT_DWELL_LOCATIONS), so the watch list can change without
  touching SQL.

  Output columns (one row per serial per location):
    SERIAL_NO, PART_ID, PART_DESCRIPTION, WAREHOUSE_ID, LOCATION_ID,
    ARRIVED_AT, LAST_TRANSACTION_DATE, ERP_NOW

  ERP_NOW is the database server's local clock, on the same clock as
  CREATE_DATE — dwell = ERP_NOW - ARRIVED_AT regardless of the app host's
  timezone.

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

WITH TraceLoc AS (
    -- Current inferred on-hand balance per (part, serial, bin), audit bins only.
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
    WHERE (it.WAREHOUSE_ID = 'MAIN'
           AND it.LOCATION_ID IN ('C2', 'C2-SERIALIZED', 'C2-ALLOCATED'))
       OR it.WAREHOUSE_ID = 'SHIPPING'
    GROUP BY tit.PART_ID, tit.TRACE_ID, it.WAREHOUSE_ID, it.LOCATION_ID
    HAVING SUM(tit.QTY) > 0
)

SELECT
    tl.TRACE_ID            AS SERIAL_NO,
    tl.PART_ID,
    p.DESCRIPTION          AS PART_DESCRIPTION,
    tl.WAREHOUSE_ID,
    tl.LOCATION_ID,
    arr.ARRIVED_AT,
    arr.LAST_TRANSACTION_DATE,
    SYSDATETIME()          AS ERP_NOW
FROM TraceLoc tl
CROSS APPLY (
    -- Most recent inbound txn into this bin = start of the current stint.
    SELECT
        MAX(it.CREATE_DATE)      AS ARRIVED_AT,
        MAX(it.TRANSACTION_DATE) AS LAST_TRANSACTION_DATE
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID        = tit.PART_ID
    WHERE tit.PART_ID      = tl.PART_ID
      AND tit.TRACE_ID     = tl.TRACE_ID
      AND it.WAREHOUSE_ID  = tl.WAREHOUSE_ID
      AND it.LOCATION_ID   = tl.LOCATION_ID
      AND tit.QTY > 0
) arr
LEFT JOIN dbo.PART p
    ON p.ID = tl.PART_ID
ORDER BY arr.ARRIVED_AT, tl.WAREHOUSE_ID, tl.LOCATION_ID, tl.TRACE_ID;
