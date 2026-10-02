/*
===============================================================================
  ORDER DETAIL — EVERY LINE OF ONE SALES ORDER WITH READINESS FACTS
===============================================================================
  Same column contract as sql/readiness_candidates.sql but for a single order
  (:so) with no status / horizon / open-quantity filter, so the detail page
  works for firmed, held, closed and fully shipped orders alike. readiness.py
  ignores closed lines when assigning holds but the page still lists them.

  Tokens: __COMPONENT_PRODUCT_CODES__ and __RELEASE_EXCLUDED_CUSTOMERS__
  (rendered by app.render_release_candidates_query); __RELEASE_LOOKAHEAD_DAYS__
  is accepted for renderer compatibility but unused.
  Bind parameter: :so (CUSTOMER_ORDER.ID, e.g. 'SO-131844').
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
      AND pl.PART_ID IN (
            SELECT col0.PART_ID FROM dbo.CUST_ORDER_LINE col0 WITH (NOLOCK)
            WHERE col0.CUST_ORDER_ID = :so
      )
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
    co.ID                                                   AS CUST_ORDER_ID,
    col.LINE_NO,
    co.CUSTOMER_ID,
    c.NAME                                                  AS CUSTOMER_NAME,
    co.STATUS                                               AS ORDER_STATUS,
    col.LINE_STATUS,
    CAST(co.ORDER_DATE AS date)                             AS ORDER_DATE,
    co.SHIPTO_ID                                            AS SHIP_TO_ID,
    co.SHIP_TO_ADDR_NO,
    col.PART_ID,
    p.PRODUCT_CODE,
    p.DESCRIPTION                                           AS PART_DESCRIPTION,
    CASE WHEN EXISTS (
        SELECT 1 FROM ComponentProductCodes cpc
        WHERE cpc.PRODUCT_CODE = UPPER(COALESCE(p.PRODUCT_CODE, ''))
    ) THEN 'components' ELSE 'guns' END                     AS ITEM_TYPE,
    CAST(col.ORDER_QTY AS decimal(18, 4))                   AS ORDER_QTY,
    CAST(col.TOTAL_SHIPPED_QTY AS decimal(18, 4))           AS SHIPPED_QTY,
    CAST(col.ORDER_QTY - col.TOTAL_SHIPPED_QTY AS decimal(18, 4)) AS OPEN_QTY,
    CAST((col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) * ISNULL(col.UNIT_PRICE, 0)
         AS decimal(18, 2))                                 AS OPEN_VALUE,
    CAST(ISNULL(es.AVAILABLE_QTY, 0) AS decimal(18, 4))     AS AVAILABLE_QTY,
    CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date)         AS PROMISE_SHIP_DATE,
    CAST(COALESCE(col.PROMISE_DEL_DATE, co.PROMISE_DEL_DATE) AS date) AS PROMISE_DEL_DATE,
    CAST(co.DESIRED_SHIP_DATE AS date)                      AS DESIRED_SHIP_DATE,
    co.SHIP_VIA,
    c.SHIP_VIA                                              AS MASTER_SHIP_VIA,
    co.FREE_ON_BOARD,
    co.SALESREP_ID,
    co.CUSTOMER_PO_REF,
    c.DISCOUNT_CODE,
    ca.NAME                                                 AS SHIPTO_NAME,
    ca.ADDR_1                                               AS SHIPTO_ADDR_1,
    ca.ADDR_2                                               AS SHIPTO_ADDR_2,
    ca.ADDR_3                                               AS SHIPTO_ADDR_3,
    ca.CITY                                                 AS SHIPTO_CITY,
    ca.STATE                                                AS SHIPTO_STATE,
    ca.ZIPCODE                                              AS SHIPTO_ZIP,
    ca.COUNTRY                                              AS SHIPTO_COUNTRY,
    ca.ACTIVE_FLAG                                          AS SHIPTO_ACTIVE,
    ca.USER_4                                               AS SHIPTO_FFL_NUMBER,
    ca.USER_5                                               AS SHIPTO_FFL_EXPIRY_RAW,
    c.USER_4                                                AS MASTER_FFL_NUMBER,
    c.USER_5                                                AS MASTER_FFL_EXPIRY_RAW,
    ce.CREDIT_STATUS,
    CAST(ce.CREDIT_LIMIT AS decimal(18, 2))                 AS CREDIT_LIMIT,
    ce.CREDIT_LIMIT_CTL,
    ce.SHIP_CREDIT_LIMIT_CTL,
    CAST(ce.TOTAL_OPEN_RECV AS decimal(18, 2))              AS TOTAL_OPEN_RECV,
    CAST(ce.TOTAL_OPEN_SHIPPED AS decimal(18, 2))           AS TOTAL_OPEN_SHIPPED,
    CAST(ce.TOTAL_OPEN_ORDERS AS decimal(18, 2))            AS TOTAL_OPEN_ORDERS,
    ISNULL(docs.ATTACHMENT_COUNT, 0)                        AS ATTACHMENT_COUNT,
    ISNULL(docs.FFL_EZ_CHECK_COUNT, 0)                      AS FFL_EZ_CHECK_COUNT,
    ISNULL(docs.FFL_MASTER_COUNT, 0)                        AS FFL_MASTER_COUNT,
    CASE WHEN co.SALESREP_ID = 'RMA' OR COALESCE(co.CUSTOMER_PO_REF, '') LIKE '%RMA%'
         THEN 1 ELSE 0 END                                  AS IS_RMA,
    CASE WHEN COALESCE(c.DISCOUNT_CODE, '') LIKE '%International%'
         THEN 1 ELSE 0 END                                  AS IS_INTERNATIONAL,
    CASE WHEN COALESCE(c.DISCOUNT_CODE, '') LIKE '%Employee%'
         THEN 1 ELSE 0 END                                  AS IS_EMPLOYEE,
    CASE WHEN EXISTS (
        SELECT 1 FROM ExcludedCustomers ec
        WHERE UPPER(COALESCE(c.ID, '')) LIKE '%' + ec.CUSTOMER_TERM + '%'
    ) THEN 1 ELSE 0 END                                     AS EXCLUDED_CUSTOMER
FROM dbo.CUSTOMER_ORDER co WITH (NOLOCK)
INNER JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON col.CUST_ORDER_ID = co.ID
LEFT JOIN dbo.CUSTOMER c WITH (NOLOCK)
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = col.PART_ID
LEFT JOIN EligibleSupply es
    ON es.PART_ID = col.PART_ID
LEFT JOIN dbo.CUST_ADDRESS ca WITH (NOLOCK)
    ON ca.CUSTOMER_ID = co.CUSTOMER_ID
   AND ca.ADDR_NO = co.SHIP_TO_ADDR_NO
OUTER APPLY (
    SELECT TOP (1)
        ce2.CREDIT_STATUS, ce2.CREDIT_LIMIT, ce2.CREDIT_LIMIT_CTL, ce2.SHIP_CREDIT_LIMIT_CTL,
        ce2.TOTAL_OPEN_RECV, ce2.TOTAL_OPEN_SHIPPED, ce2.TOTAL_OPEN_ORDERS
    FROM dbo.CUSTOMER_ENTITY ce2 WITH (NOLOCK)
    WHERE ce2.CUSTOMER_ID = co.CUSTOMER_ID
    ORDER BY CASE WHEN ce2.CREDIT_STATUS = 'A' THEN 1 ELSE 0 END, ce2.ENTITY_ID
) ce
OUTER APPLY (
    SELECT
        COUNT(*) AS ATTACHMENT_COUNT,
        SUM(CASE
                WHEN UPPER(d.ID) LIKE '%FFL EZ CHECK%'
                  OR UPPER(d.ID) LIKE '%FFL EZCHECK%'
                  OR UPPER(d.ID) LIKE '%EZ CHECK%'
                  OR UPPER(d.ID) LIKE '%EZCHECK%'
                  OR UPPER(d.ID) LIKE '%EZ-CHECK%' THEN 1 ELSE 0
            END) AS FFL_EZ_CHECK_COUNT,
        SUM(CASE
                WHEN (LOWER(d.ID) LIKE '%.pdf' AND UPPER(d.DOC_FILE_PATH) LIKE '%\FFLS%')
                  OR (LOWER(d.ID) LIKE '%.pdf' AND UPPER(d.ID) LIKE '%FFL%') THEN 1 ELSE 0
            END) AS FFL_MASTER_COUNT
    FROM dbo.DOCUMENT_REFERENCE dr WITH (NOLOCK)
    LEFT JOIN dbo.DOCUMENT d WITH (NOLOCK)
        ON d.ID = dr.DOCUMENT_ID
    WHERE dr.SOURCE_TYPE = 'T'
      AND dr.ID = co.ID
) docs
CROSS JOIN Params prm
WHERE co.ID = :so
ORDER BY col.LINE_NO
OPTION (RECOMPILE);
