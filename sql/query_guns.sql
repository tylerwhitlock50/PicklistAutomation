WITH
Params AS (
    SELECT
        CAST(GETDATE() AS date) AS TODAY,
        DATEADD(day, 30, CAST(GETDATE() AS date)) AS THROUGH_DATE
),

-- DETERMINE SUPPLY: SHIPPING warehouse, Racks R01-R09 only, exclude Stage/International, Rack10 and Rack11
Supply AS (
    SELECT
        pl.PART_ID,
        pl.LOCATION_ID,
        SUM(CAST(pl.QTY AS int)) AS QTY
    FROM dbo.PART_LOCATION pl
    WHERE pl.WAREHOUSE_ID = 'SHIPPING'
      AND pl.QTY > 0
      AND COALESCE(pl.LOCATION_ID, '') <> ''
      AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
      AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
      AND LEFT(COALESCE(pl.LOCATION_ID, ''), 3) BETWEEN 'R01' AND 'R09'
    GROUP BY
        pl.PART_ID,
        pl.LOCATION_ID
),

-- DETERMINE DEMAND: open SO lines (Released Orders, Available Lines, Good credit status, Not RMA, Not International, and Not Employee)
DemandBase AS (
    SELECT
        co.ORDER_DATE,
        co.ID AS CUST_ORDER_ID,
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
      AND (ce.CUSTOMER_ID IS NULL OR ce.CUSTOMER_ID NOT LIKE '%CA MARK%')
      AND (co.SALESREP_ID IS NULL OR co.SALESREP_ID <> 'RMA')
      AND (co.CUSTOMER_PO_REF IS NULL OR co.CUSTOMER_PO_REF NOT LIKE '%RMA%')
      AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%International%')
      AND (c.DISCOUNT_CODE IS NULL OR c.DISCOUNT_CODE NOT LIKE '%Employee%')
      AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
),

-- Determine Demand horizon: customer want date is either past due OR due within next 30 days
-- NULL promise dates are included by treating them as TODAY
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

-- Supply ranges per part (location order is arbitrary but deterministic)
SupplyRanges AS (
    SELECT
        s.PART_ID,
        s.LOCATION_ID,
        s.QTY,
        SUM(s.QTY) OVER (
            PARTITION BY s.PART_ID
            ORDER BY s.LOCATION_ID
            ROWS UNBOUNDED PRECEDING
        ) AS SUPPLY_CUM_END,
        SUM(s.QTY) OVER (
            PARTITION BY s.PART_ID
            ORDER BY s.LOCATION_ID
            ROWS UNBOUNDED PRECEDING
        ) - s.QTY AS SUPPLY_CUM_START
    FROM Supply s
),

-- Demand ranges per part, FIFO, then tie-breakers
DemandRanges AS (
    SELECT
        d.ORDER_DATE,
        d.CUST_ORDER_ID,
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

Allocations AS (
    SELECT
        dr.PART_ID,
        sr.LOCATION_ID,
        dr.CUST_ORDER_ID,
        dr.CUSTOMER_ID,
        dr.LINE_NO,
        dr.PROMISE_DEL_DATE,
        dr.ORDER_DATE,
        (CASE WHEN sr.SUPPLY_CUM_END   < dr.DEMAND_CUM_END   THEN sr.SUPPLY_CUM_END   ELSE dr.DEMAND_CUM_END   END)
      - (CASE WHEN sr.SUPPLY_CUM_START > dr.DEMAND_CUM_START THEN sr.SUPPLY_CUM_START ELSE dr.DEMAND_CUM_START END)
        AS ALLOC_QTY
    FROM DemandRanges dr
    JOIN SupplyRanges sr
      ON sr.PART_ID = dr.PART_ID
     AND sr.SUPPLY_CUM_END   > dr.DEMAND_CUM_START
     AND sr.SUPPLY_CUM_START < dr.DEMAND_CUM_END
)

SELECT
    p.PRODUCT_CODE  AS [Product code],
    a.PART_ID       AS [Part Id],
    a.LOCATION_ID   AS [Location],
    a.CUST_ORDER_ID AS [Cust Order ID],
    a.CUSTOMER_ID   AS [Customer ID],
    a.ALLOC_QTY     AS [SO Qty]
FROM Allocations a
LEFT JOIN dbo.PART p
  ON p.ID = a.PART_ID
WHERE a.ALLOC_QTY > 0
ORDER BY
    a.CUST_ORDER_ID,
    a.LINE_NO,
    a.PART_ID,
    p.PRODUCT_CODE,
    a.LOCATION_ID;
