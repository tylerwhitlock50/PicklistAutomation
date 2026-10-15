/*
===============================================================================
  RELEASE GATE — RECENT SHIP-TO HISTORY
===============================================================================
  One row per VISUAL customer + ship-to ID, containing the most recent actual
  shipment date. The release gate compares this date with each account's
  configurable ship_to_cooldown_days before allowing another picklist.

  Thirty-one days covers the application's supported 0-30 day cooldown range.
  Read-only; SQL Server / Infor VISUAL (VECA).
===============================================================================
*/

WITH RecentShippedOrders AS (
    SELECT DISTINCT
        COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) AS CUST_ORDER_ID,
        CAST(s.SHIPPED_DATE AS date) AS SHIPPED_DATE
    FROM dbo.SHIPPER s WITH (NOLOCK)
    INNER JOIN dbo.SHIPPER_LINE sl WITH (NOLOCK)
        ON sl.PACKLIST_ID = s.PACKLIST_ID
    WHERE s.SHIPPED_DATE >= DATEADD(day, -31, CAST(GETDATE() AS date))
      AND s.SHIPPED_DATE < DATEADD(day, 1, CAST(GETDATE() AS date))
      AND COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) IS NOT NULL
      AND COALESCE(s.STATUS, '') NOT IN ('X', 'V')
)
SELECT
    UPPER(LTRIM(RTRIM(co.CUSTOMER_ID))) AS CUSTOMER_ID,
    UPPER(COALESCE(NULLIF(LTRIM(RTRIM(co.SHIPTO_ID)), ''), 'DEFAULT')) AS SHIP_TO_ID,
    MAX(rso.SHIPPED_DATE) AS LAST_SHIPPED_DATE
FROM RecentShippedOrders rso
INNER JOIN dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    ON co.ID = rso.CUST_ORDER_ID
WHERE NULLIF(LTRIM(RTRIM(COALESCE(co.CUSTOMER_ID, ''))), '') IS NOT NULL
GROUP BY
    UPPER(LTRIM(RTRIM(co.CUSTOMER_ID))),
    UPPER(COALESCE(NULLIF(LTRIM(RTRIM(co.SHIPTO_ID)), ''), 'DEFAULT'))
ORDER BY CUSTOMER_ID, SHIP_TO_ID;
