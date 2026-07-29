/*
===============================================================================
  ALLOCATION SCREEN — SUPPLY EVENTS FOR ONE SKU
===============================================================================
  All supply the allocation screen knows about for :part_id, one row per
  event, in a uniform shape. Python (allocation.py) turns these rows into
  dated, certainty-ranked supply events and applies the master-schedule
  netting fence — no allocation math happens here.

  SUPPLY_CLASS values:
    ON_HAND        — bins the guns picklist actually picks from
                     (SHIPPING racks R01-R09, no Stage/International).
                     SUPPLY_DATE is NULL: available today.
    ON_HAND_OTHER  — every other bin holding the part (MAIN, staged, other
                     racks). Informational only; ELIGIBLE = 0, never allocated.
    WO_RELEASED    — open released work orders (TYPE 'W', STATUS 'R').
    WO_FIRMED      — open firmed work orders (TYPE 'W', STATUS 'F').
    MPS            — master-schedule buckets (STANDARD schedule). All rows are
                     returned; allocation.py drops buckets on/before the
                     netting fence (max open-WO want date) so the same
                     production is never counted twice.

  Work orders already tied to a demand via DEMAND_SUPPLY_LINK (RMA repair /
  make-to-order) are reserved elsewhere and excluded from allocatable supply.

  Bind parameters:  :part_id
  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    'ON_HAND'                             AS SUPPLY_CLASS,
    pl.WAREHOUSE_ID + '/' + pl.LOCATION_ID AS SUPPLY_ID,
    CAST(pl.QTY AS int)                   AS QTY,
    CAST(NULL AS date)                    AS SUPPLY_DATE,
    1                                     AS ELIGIBLE,
    pl.LOCATION_ID                        AS DETAIL
FROM dbo.PART_LOCATION pl
WHERE pl.PART_ID = :part_id
  AND pl.WAREHOUSE_ID = 'SHIPPING'
  AND pl.QTY > 0
  AND COALESCE(pl.LOCATION_ID, '') <> ''
  AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
  AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
  AND LEFT(COALESCE(pl.LOCATION_ID, ''), 3) BETWEEN 'R01' AND 'R09'

UNION ALL

SELECT
    'ON_HAND_OTHER',
    pl.WAREHOUSE_ID + '/' + COALESCE(pl.LOCATION_ID, '?'),
    CAST(pl.QTY AS int),
    CAST(NULL AS date),
    0,
    pl.WAREHOUSE_ID
FROM dbo.PART_LOCATION pl
WHERE pl.PART_ID = :part_id
  AND pl.QTY > 0
  AND NOT (
        pl.WAREHOUSE_ID = 'SHIPPING'
    AND COALESCE(pl.LOCATION_ID, '') <> ''
    AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
    AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
    AND LEFT(COALESCE(pl.LOCATION_ID, ''), 3) BETWEEN 'R01' AND 'R09'
  )

UNION ALL

SELECT
    CASE wo.STATUS WHEN 'R' THEN 'WO_RELEASED' ELSE 'WO_FIRMED' END,
    'WO ' + wo.BASE_ID + '/' + wo.LOT_ID + '/' + wo.SPLIT_ID,
    CAST(wo.DESIRED_QTY - wo.RECEIVED_QTY AS int),
    CAST(COALESCE(wo.SCHED_FINISH_DATE, wo.DESIRED_WANT_DATE) AS date),
    1,
    'received ' + CAST(CAST(wo.RECEIVED_QTY AS int) AS varchar(12))
        + ' of ' + CAST(CAST(wo.DESIRED_QTY AS int) AS varchar(12))
FROM dbo.WORK_ORDER wo
WHERE wo.PART_ID = :part_id
  AND wo.TYPE = 'W'
  AND wo.STATUS IN ('R', 'F')
  AND (wo.DESIRED_QTY - wo.RECEIVED_QTY) > 0
  AND NOT EXISTS (
      SELECT 1
      FROM dbo.DEMAND_SUPPLY_LINK dsl
      WHERE dsl.SUPPLY_TYPE = 'WO'
        AND dsl.SUPPLY_BASE_ID = wo.BASE_ID
        AND dsl.SUPPLY_LOT_ID = wo.LOT_ID
        AND dsl.SUPPLY_SPLIT_ID = wo.SPLIT_ID
        AND dsl.SUPPLY_SUB_ID = wo.SUB_ID
  )

UNION ALL

SELECT
    'MPS',
    'MPS ' + CONVERT(varchar(10), ms.WANT_DATE, 120),
    CAST(ms.ORDER_QTY AS int),
    CAST(ms.WANT_DATE AS date),
    1,
    CASE WHEN ms.FIRMED = 'Y' THEN 'firmed bucket' ELSE 'planned bucket' END
FROM dbo.MASTER_SCHEDULE ms
WHERE ms.PART_ID = :part_id
  AND ms.MASTER_SCHEDULE_ID = 'STANDARD'
  AND ms.ORDER_QTY > 0;
