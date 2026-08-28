/*
===============================================================================
  ORDER RELEASE GATE — OPEN PHYSICAL LINE CANDIDATES + ELIGIBLE SUPPLY
===============================================================================
  One row per open physical customer-order line. Existing eligibility rules are
  returned as explicit flags instead of being filtered away, so the Python gate
  can give Shipping a plain-language BLOCKED / HOLD / SHIP NOW reason.

  AVAILABLE_QTY is repeated at part grain; release_gate.py takes the maximum
  once per part and performs deterministic, complete-order allocation. This
  query never writes to VISUAL.
===============================================================================
*/

WITH Params AS (
    SELECT
        CAST(GETDATE() AS date) AS TODAY,
        DATEADD(day, __RELEASE_LOOKAHEAD_DAYS__, CAST(GETDATE() AS date)) AS THROUGH_DATE
),
ComponentProductCodes AS (
    __COMPONENT_PRODUCT_CODES__
),
ExcludedCustomers AS (
    __RELEASE_EXCLUDED_CUSTOMERS__
),
EligibleSupply AS (
    SELECT
        pl.PART_ID,
        SUM(CAST(pl.QTY AS decimal(18, 4))) AS AVAILABLE_QTY
    FROM dbo.PART_LOCATION pl WITH (NOLOCK)
    LEFT JOIN dbo.PART p WITH (NOLOCK)
        ON p.ID = pl.PART_ID
    WHERE pl.QTY > 0
      AND NULLIF(LTRIM(RTRIM(pl.LOCATION_ID)), '') IS NOT NULL
      AND (
            (
                EXISTS (
                    SELECT 1 FROM ComponentProductCodes cpc
                    WHERE cpc.PRODUCT_CODE = UPPER(COALESCE(p.PRODUCT_CODE, ''))
                )
                AND (
                       (pl.WAREHOUSE_ID = 'DISTRIBUTION'
                        AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STOCK%')
                    OR (pl.WAREHOUSE_ID = 'SHIPPING'
                        AND LEFT(UPPER(COALESCE(pl.LOCATION_ID, '')), 3) = 'R11')
                )
            )
         OR (
                NOT EXISTS (
                    SELECT 1 FROM ComponentProductCodes cpc
                    WHERE cpc.PRODUCT_CODE = UPPER(COALESCE(p.PRODUCT_CODE, ''))
                )
                AND pl.WAREHOUSE_ID = 'SHIPPING'
                AND LEFT(UPPER(COALESCE(pl.LOCATION_ID, '')), 3) BETWEEN 'R01' AND 'R09'
                AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
                AND UPPER(COALESCE(pl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
            )
          )
    GROUP BY pl.PART_ID
)
SELECT
    co.ID AS CUST_ORDER_ID,
    col.LINE_NO,
    co.CUSTOMER_ID,
    c.NAME AS CUSTOMER_NAME,
    co.ORDER_DATE,
    col.PART_ID,
    p.PRODUCT_CODE,
    CASE WHEN EXISTS (
        SELECT 1 FROM ComponentProductCodes cpc
        WHERE cpc.PRODUCT_CODE = UPPER(COALESCE(p.PRODUCT_CODE, ''))
    ) THEN 'components' ELSE 'guns' END AS ITEM_TYPE,
    CAST(col.ORDER_QTY - col.TOTAL_SHIPPED_QTY AS decimal(18, 4)) AS OPEN_QTY,
    CAST(ISNULL(es.AVAILABLE_QTY, 0) AS decimal(18, 4)) AS AVAILABLE_QTY,
    CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date) AS PROMISE_SHIP_DATE,
    CAST(COALESCE(col.PROMISE_DEL_DATE, co.PROMISE_DEL_DATE) AS date) AS PROMISE_DEL_DATE,
    CAST(co.DESIRED_SHIP_DATE AS date) AS DESIRED_SHIP_DATE,
    CASE WHEN co.STATUS = 'R' THEN 1 ELSE 0 END AS ORDER_RELEASED,
    CASE WHEN col.LINE_STATUS = 'A' THEN 1 ELSE 0 END AS LINE_AVAILABLE,
    CASE WHEN EXISTS (
        SELECT 1 FROM dbo.CUSTOMER_ENTITY ce WITH (NOLOCK)
        WHERE ce.CUSTOMER_ID = co.CUSTOMER_ID AND ce.CREDIT_STATUS = 'A'
    ) THEN 1 ELSE 0 END AS CREDIT_APPROVED,
    CASE WHEN NULLIF(LTRIM(RTRIM(COALESCE(co.SHIPTO_ID, ''))), '') IS NOT NULL
         THEN 1 ELSE 0 END AS SHIP_TO_PRESENT,
    CASE WHEN co.SALESREP_ID = 'RMA' OR COALESCE(co.CUSTOMER_PO_REF, '') LIKE '%RMA%'
         THEN 1 ELSE 0 END AS IS_RMA,
    CASE WHEN COALESCE(c.DISCOUNT_CODE, '') LIKE '%International%'
         THEN 1 ELSE 0 END AS IS_INTERNATIONAL,
    CASE WHEN COALESCE(c.DISCOUNT_CODE, '') LIKE '%Employee%'
         THEN 1 ELSE 0 END AS IS_EMPLOYEE,
    CASE WHEN EXISTS (
        SELECT 1 FROM ExcludedCustomers ec
        WHERE UPPER(COALESCE(c.ID, '')) LIKE '%' + ec.CUSTOMER_TERM + '%'
    ) THEN 1 ELSE 0 END AS EXCLUDED_CUSTOMER
FROM dbo.CUSTOMER_ORDER co WITH (NOLOCK)
INNER JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON col.CUST_ORDER_ID = co.ID
LEFT JOIN dbo.CUSTOMER c WITH (NOLOCK)
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = col.PART_ID
LEFT JOIN EligibleSupply es
    ON es.PART_ID = col.PART_ID
CROSS JOIN Params prm
WHERE col.PART_ID IS NOT NULL
  AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
  AND ISNULL(co.STATUS, '') NOT IN ('C', 'X')
  AND ISNULL(col.LINE_STATUS, '') <> 'X'
  AND COALESCE(
        CAST(COALESCE(col.PROMISE_DEL_DATE, co.PROMISE_DEL_DATE) AS date),
        CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date),
        CAST(co.DESIRED_SHIP_DATE AS date),
        prm.TODAY
      ) <= prm.THROUGH_DATE
ORDER BY
    CASE WHEN COALESCE(col.PROMISE_DEL_DATE, co.PROMISE_DEL_DATE) IS NULL THEN 1 ELSE 0 END,
    COALESCE(col.PROMISE_DEL_DATE, co.PROMISE_DEL_DATE),
    CASE WHEN COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) IS NULL THEN 1 ELSE 0 END,
    COALESCE(col.PROMISE_DATE, co.PROMISE_DATE),
    co.ORDER_DATE,
    co.ID,
    col.LINE_NO
OPTION (RECOMPILE);
