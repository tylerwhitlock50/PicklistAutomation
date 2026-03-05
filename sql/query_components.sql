WITH
Params AS (
    SELECT
        CAST(GETDATE() AS date) AS TODAY,
        DATEADD(day, 10, CAST(GETDATE() AS date)) AS THROUGH_DATE
),

/* 1) Supply */
Supply AS (
    SELECT
        pl.PART_ID,
        pl.LOCATION_ID,
        pl.WAREHOUSE_ID,
        SUM(CAST(pl.QTY AS int)) AS QTY
    FROM dbo.PART_LOCATION pl
    WHERE pl.QTY > 0
      AND (
            (
                pl.WAREHOUSE_ID = 'DISTRIBUTION'
                -- Exclude overstock (example: R03-OVERSTOCK)
                AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%OVERSTOCK%'
                -- Optional broad exclusion (keep if you truly mean *any* STOCK-labelled locations)
                AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STOCK%'
            )
         OR (
                pl.WAREHOUSE_ID = 'SHIPPING'
                AND COALESCE(pl.LOCATION_ID, '') LIKE 'R11%'
            )
      )
    GROUP BY
        pl.PART_ID,
        pl.LOCATION_ID,
        pl.WAREHOUSE_ID
),

/* 2) Demand base (open order lines) */
DemandBase AS (
    SELECT
        co.ORDER_DATE,
        co.ID AS CUST_ORDER_ID,
        co.SHIPTO_ID,
        co.SHIP_VIA,
        c.ID AS CUSTOMER_ID,
        col.LINE_NO,
        col.PART_ID,
        CAST((col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) AS int) AS OPEN_QTY,
        CAST(co.PROMISE_DEL_DATE AS date) AS PROMISE_DEL_DATE
    FROM dbo.CUST_ORDER_LINE col
    JOIN dbo.CUSTOMER_ORDER co
        ON col.CUST_ORDER_ID = co.ID
    JOIN dbo.CUSTOMER c
        ON c.ID = co.CUSTOMER_ID
    JOIN dbo.CUSTOMER_ENTITY ce
        ON ce.CUSTOMER_ID = c.ID
       AND ce.CREDIT_STATUS = 'A'
    WHERE co.STATUS = 'R'
      AND col.LINE_STATUS = 'A'
      AND (co.SALESREP_ID IS NULL OR co.SALESREP_ID <> 'RMA')
      AND (co.CUSTOMER_PO_REF IS NULL OR co.CUSTOMER_PO_REF NOT LIKE '%RMA%')
      AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%International%')
      AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%Employee%')
      AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
      AND co.SHIPTO_ID IS NOT NULL
),

/* 3) Demand filtered to parts with supply + horizon (<= today+10, includes past due; NULL treated as today) */
Demand AS (
    SELECT
        d.*,
        COALESCE(d.PROMISE_DEL_DATE, p.TODAY) AS PROMISE_DATE_NORM,
        p.TODAY,
        p.THROUGH_DATE
    FROM DemandBase d
    CROSS JOIN Params p
    WHERE EXISTS (SELECT 1 FROM Supply s WHERE s.PART_ID = d.PART_ID)
      AND COALESCE(d.PROMISE_DEL_DATE, p.TODAY) <= p.THROUGH_DATE
),

/* 4) Supply cumulative ranges */
SupplyRanges AS (
    SELECT
        s.PART_ID,
        s.LOCATION_ID,
        s.WAREHOUSE_ID,
        s.QTY,

        SUM(s.QTY) OVER (
            PARTITION BY s.PART_ID
            ORDER BY
                CASE WHEN s.WAREHOUSE_ID = 'DISTRIBUTION' THEN 0 ELSE 1 END,
                s.LOCATION_ID
            ROWS UNBOUNDED PRECEDING
        ) AS SUPPLY_CUM_END,

        SUM(s.QTY) OVER (
            PARTITION BY s.PART_ID
            ORDER BY
                CASE WHEN s.WAREHOUSE_ID = 'DISTRIBUTION' THEN 0 ELSE 1 END,
                s.LOCATION_ID
            ROWS UNBOUNDED PRECEDING
        ) - s.QTY AS SUPPLY_CUM_START
    FROM Supply s
),

/* 5) Demand FIFO cumulative ranges (Promise date FIFO; overdue first) */
DemandRanges AS (
    SELECT
        d.ORDER_DATE,
        d.CUST_ORDER_ID,
        d.SHIPTO_ID,
        d.SHIP_VIA,
        d.CUSTOMER_ID,
        d.LINE_NO,
        d.PART_ID,
        d.OPEN_QTY,
        d.PROMISE_DATE_NORM AS PROMISE_DEL_DATE,

        SUM(d.OPEN_QTY) OVER (
            PARTITION BY d.PART_ID
            ORDER BY
                CASE WHEN d.PROMISE_DATE_NORM < d.TODAY THEN 0 ELSE 1 END,
                d.PROMISE_DATE_NORM,
                d.ORDER_DATE,
                d.CUST_ORDER_ID,
                d.LINE_NO
            ROWS UNBOUNDED PRECEDING
        ) AS DEMAND_CUM_END,

        SUM(d.OPEN_QTY) OVER (
            PARTITION BY d.PART_ID
            ORDER BY
                CASE WHEN d.PROMISE_DATE_NORM < d.TODAY THEN 0 ELSE 1 END,
                d.PROMISE_DATE_NORM,
                d.ORDER_DATE,
                d.CUST_ORDER_ID,
                d.LINE_NO
            ROWS UNBOUNDED PRECEDING
        ) - d.OPEN_QTY AS DEMAND_CUM_START
    FROM Demand d
),

/* 6) Allocation */
Allocations AS (
    SELECT
        dr.PART_ID,
        sr.LOCATION_ID,
        sr.WAREHOUSE_ID,
        dr.CUST_ORDER_ID,
        dr.SHIPTO_ID,
        dr.SHIP_VIA,
        dr.CUSTOMER_ID,
        dr.LINE_NO,
        dr.PROMISE_DEL_DATE,
        dr.ORDER_DATE,

        (CASE WHEN sr.SUPPLY_CUM_END < dr.DEMAND_CUM_END THEN sr.SUPPLY_CUM_END ELSE dr.DEMAND_CUM_END END)
      - (CASE WHEN sr.SUPPLY_CUM_START > dr.DEMAND_CUM_START THEN sr.SUPPLY_CUM_START ELSE dr.DEMAND_CUM_START END)
        AS ALLOC_QTY
    FROM DemandRanges dr
    JOIN SupplyRanges sr
      ON sr.PART_ID = dr.PART_ID
     AND sr.SUPPLY_CUM_END   > dr.DEMAND_CUM_START
     AND sr.SUPPLY_CUM_START < dr.DEMAND_CUM_END
)

/* 7) Final output */
SELECT
    p.PRODUCT_CODE  AS [Product code],
    a.PART_ID       AS [Part Id],
    a.LOCATION_ID   AS [Location],
    a.WAREHOUSE_ID  AS [Warehouse],
    a.CUST_ORDER_ID AS [Cust Order ID],
    a.CUSTOMER_ID   AS [Customer ID],
    a.SHIPTO_ID     AS [Ship To ID],
    a.SHIP_VIA      AS [Ship Via],
    a.ALLOC_QTY     AS [SO Qty]
FROM Allocations a
JOIN dbo.PART p
  ON p.ID = a.PART_ID
WHERE a.ALLOC_QTY > 0
ORDER BY
    a.PROMISE_DEL_DATE,
    a.ORDER_DATE,
    a.CUST_ORDER_ID,
    a.LINE_NO,
    a.PART_ID,
    a.WAREHOUSE_ID,
    a.LOCATION_ID;
