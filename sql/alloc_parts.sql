/*
===============================================================================
  ALLOCATION SCREEN — PART SEARCH / AUTOCOMPLETE
===============================================================================
  Top 20 parts matching the typed text, restricted to parts that actually
  matter to allocation: open customer demand, eligible on-hand in the guns
  picklist racks, or an open work order.

  Bind parameters:
    :pattern — '%TERM%' (uppercased by the app)
    :prefix  — 'TERM%'  (id-prefix matches rank first)

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT TOP (20)
    p.ID                                    AS PART_ID,
    p.DESCRIPTION,
    COALESCE(oh.QTY, 0)                     AS ON_HAND,
    COALESCE(dem.OPEN_QTY, 0)               AS OPEN_DEMAND
FROM dbo.PART p
OUTER APPLY (
    SELECT SUM(CAST(pl.QTY AS int)) AS QTY
    FROM dbo.PART_LOCATION pl
    WHERE pl.PART_ID = p.ID
      AND pl.WAREHOUSE_ID = 'SHIPPING'
      AND pl.QTY > 0
      AND COALESCE(pl.LOCATION_ID, '') <> ''
      AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
      AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
      AND LEFT(COALESCE(pl.LOCATION_ID, ''), 3) BETWEEN 'R01' AND 'R09'
) oh
OUTER APPLY (
    SELECT SUM(CAST(col.ORDER_QTY - col.TOTAL_SHIPPED_QTY AS int)) AS OPEN_QTY
    FROM dbo.CUST_ORDER_LINE col
    JOIN dbo.CUSTOMER_ORDER co ON co.ID = col.CUST_ORDER_ID
    WHERE col.PART_ID = p.ID
      AND col.LINE_STATUS = 'A'
      AND co.STATUS IN ('R', 'F', 'H')
      AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
) dem
WHERE (UPPER(p.ID) LIKE :pattern OR UPPER(COALESCE(p.DESCRIPTION, '')) LIKE :pattern)
  AND (
        COALESCE(dem.OPEN_QTY, 0) > 0
     OR COALESCE(oh.QTY, 0) > 0
     OR EXISTS (
            SELECT 1 FROM dbo.WORK_ORDER wo
            WHERE wo.PART_ID = p.ID
              AND wo.TYPE = 'W'
              AND wo.STATUS IN ('R', 'F')
              AND (wo.DESIRED_QTY - wo.RECEIVED_QTY) > 0
        )
  )
ORDER BY
    CASE WHEN UPPER(p.ID) LIKE :prefix THEN 0 ELSE 1 END,
    p.ID;
