/*
===============================================================================
  COMPONENT SHORTAGES — DEMAND THE PICKLIST CANNOT SEE
===============================================================================
  Every open component order line inside the ship horizon, with the part's
  on-hand supply split into three classes:

    PICKLIST_QTY — the bins the components picklist actually picks from
                   (DISTRIBUTION excluding *STOCK* locations, SHIPPING R11*).
                   Must stay in lockstep with sql/query_components.sql.
    MAIN_QTY     — stock sitting in the MAIN (manufacturing) warehouse,
                   fillable after a transfer to DISTRIBUTION.
    OTHER_QTY    — everything else (excluded DISTRIBUTION bins, other
                   SHIPPING racks, any other warehouse).

  The app allocates PICKLIST_QTY across the lines FIFO (same ordering as the
  picklist) and reports the remainder as shortages, classified by why:
  missing ship-to, needs a transfer, or a true stockout.

  SHIPTO_ID rides along so the app can hint "no ship-to on this order —
  fix it before it can actually ship" (the picklist prints those lines too).
  Credit holds, RMA, International, and Employee orders stay excluded:
  those are intentional business filters.

  STOCK_LOCATIONS is a display string of the top non-picklist bins holding
  the part ("MAIN/P-ASSY (120), MAIN/MRB AREA (5)"), FOR XML PATH because
  SQL Server 2016 has no STRING_AGG.

  Tokens (replaced by the app before execution — cannot be bind parameters):
    __SHORTAGE_PRODUCT_CODES__ — UNION ALL rows of product codes in scope.

  Bind parameters:
    :lookahead_days — demand horizon in days (picklist uses 10).

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

WITH
ProductCodes AS (
    __SHORTAGE_PRODUCT_CODES__
),

Params AS (
    SELECT
        CAST(GETDATE() AS date) AS TODAY,
        DATEADD(day, :lookahead_days, CAST(GETDATE() AS date)) AS THROUGH_DATE
),

SupplyByClass AS (
    SELECT
        pl.PART_ID,
        SUM(CASE WHEN (pl.WAREHOUSE_ID = 'DISTRIBUTION'
                       AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STOCK%')
                   OR (pl.WAREHOUSE_ID = 'SHIPPING'
                       AND COALESCE(pl.LOCATION_ID, '') LIKE 'R11%')
                 THEN CAST(pl.QTY AS int) ELSE 0 END) AS PICKLIST_QTY,
        SUM(CASE WHEN pl.WAREHOUSE_ID = 'MAIN'
                 THEN CAST(pl.QTY AS int) ELSE 0 END) AS MAIN_QTY,
        SUM(CASE WHEN pl.WAREHOUSE_ID <> 'MAIN'
                  AND NOT (pl.WAREHOUSE_ID = 'DISTRIBUTION'
                           AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STOCK%')
                  AND NOT (pl.WAREHOUSE_ID = 'SHIPPING'
                           AND COALESCE(pl.LOCATION_ID, '') LIKE 'R11%')
                 THEN CAST(pl.QTY AS int) ELSE 0 END) AS OTHER_QTY
    FROM dbo.PART_LOCATION pl
    WHERE pl.QTY > 0
    GROUP BY pl.PART_ID
)

SELECT
    prm.TODAY,
    co.ORDER_DATE,
    co.ID                       AS CUST_ORDER_ID,
    col.LINE_NO,
    c.ID                        AS CUSTOMER_ID,
    c.NAME                      AS CUSTOMER_NAME,
    co.SHIPTO_ID,
    col.PART_ID,
    p.PRODUCT_CODE,
    p.DESCRIPTION               AS PART_DESCRIPTION,
    CAST(col.ORDER_QTY - col.TOTAL_SHIPPED_QTY AS int) AS OPEN_QTY,
    CAST(co.DESIRED_SHIP_DATE AS date)                 AS DESIRED_SHIP_DATE,
    COALESCE(s.PICKLIST_QTY, 0) AS PICKLIST_QTY,
    COALESCE(s.MAIN_QTY, 0)     AS MAIN_QTY,
    COALESCE(s.OTHER_QTY, 0)    AS OTHER_QTY,
    stock.STOCK_LOCATIONS
FROM dbo.CUST_ORDER_LINE col
JOIN dbo.CUSTOMER_ORDER co
    ON col.CUST_ORDER_ID = co.ID
JOIN dbo.CUSTOMER c
    ON c.ID = co.CUSTOMER_ID
JOIN dbo.CUSTOMER_ENTITY ce
    ON ce.CUSTOMER_ID = c.ID
   AND ce.CREDIT_STATUS = 'A'
JOIN dbo.PART p
    ON p.ID = col.PART_ID
JOIN ProductCodes pc
    ON pc.PRODUCT_CODE = p.PRODUCT_CODE
LEFT JOIN SupplyByClass s
    ON s.PART_ID = col.PART_ID
OUTER APPLY (
    -- Where the part's non-picklist stock sits, biggest piles first.
    SELECT STUFF((
        SELECT TOP (5)
            ', ' + pl.WAREHOUSE_ID + '/' + COALESCE(pl.LOCATION_ID, '?')
            + ' (' + CAST(CAST(pl.QTY AS int) AS varchar(12)) + ')'
        FROM dbo.PART_LOCATION pl
        WHERE pl.PART_ID = col.PART_ID
          AND pl.QTY > 0
          AND NOT (pl.WAREHOUSE_ID = 'DISTRIBUTION'
                   AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STOCK%')
          AND NOT (pl.WAREHOUSE_ID = 'SHIPPING'
                   AND COALESCE(pl.LOCATION_ID, '') LIKE 'R11%')
        ORDER BY pl.QTY DESC
        FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS STOCK_LOCATIONS
) stock
CROSS JOIN Params prm
WHERE co.STATUS = 'R'
  AND col.LINE_STATUS = 'A'
  AND (co.SALESREP_ID IS NULL OR co.SALESREP_ID <> 'RMA')
  AND (co.CUSTOMER_PO_REF IS NULL OR co.CUSTOMER_PO_REF NOT LIKE '%RMA%')
  AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%International%')
  AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%Employee%')
  AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
  AND COALESCE(CAST(co.DESIRED_SHIP_DATE AS date), prm.TODAY) <= prm.THROUGH_DATE
ORDER BY
    col.PART_ID,
    CASE WHEN COALESCE(CAST(co.DESIRED_SHIP_DATE AS date), prm.TODAY) < prm.TODAY THEN 0 ELSE 1 END,
    COALESCE(CAST(co.DESIRED_SHIP_DATE AS date), prm.TODAY),
    co.ORDER_DATE,
    co.ID,
    col.LINE_NO;
