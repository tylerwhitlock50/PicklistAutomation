/*
===============================================================================
  SHIPPING MANAGEMENT SCORECARD — RAW ORDER + SHIPMENT FACTS
===============================================================================
  Returns a compact UNION with two grains:

    ORDER_LINE    one physical customer-order line for relevant orders
    SHIPMENT_LINE one historical shipper line for those same orders

  Relevant order IDs are materialized first. This avoids repeatedly expanding a
  broad CTE across the large order/shipper history and gives SQL Server a small,
  indexed driving set for the rest of the read-only report.

  Relevant orders are those due or shipped in [:query_start, :end_date). The
  shipment facts intentionally include each relevant order's history before
  :end_date so first-shipment completeness and by-promise performance can be
  reconstructed without survivorship bias.

  FIREARM_QTY is the number of distinct serialized TRACE units attached to the
  shipper-line inventory transaction. Temporary trace aggregation keeps the
  final result at one row per SHIPPER_LINE and prevents join multiplication.

  NOLOCK is deliberate for this management view: it must not contend with live
  Shipping transactions. Coverage diagnostics and periodic snapshots make any
  transient source inconsistency visible and recoverable.

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SET NOCOUNT ON;

DECLARE @QUERY_START date = CAST(:query_start AS date);
DECLARE @END_DATE date = CAST(:end_date AS date);

CREATE TABLE #RelevantOrders (
    CUST_ORDER_ID varchar(50) NOT NULL PRIMARY KEY
);

INSERT INTO #RelevantOrders (CUST_ORDER_ID)
SELECT candidate.CUST_ORDER_ID
FROM (
    SELECT co.ID AS CUST_ORDER_ID
    FROM dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    INNER JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
        ON col.CUST_ORDER_ID = co.ID
    WHERE col.PART_ID IS NOT NULL
      AND col.ORDER_QTY > 0
      AND ISNULL(co.STATUS, '') <> 'X'
      AND ISNULL(col.LINE_STATUS, '') <> 'X'
      AND CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date) >= @QUERY_START
      AND CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date) <  @END_DATE

    UNION

    SELECT COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
    FROM dbo.SHIPPER s WITH (NOLOCK)
    INNER JOIN dbo.SHIPPER_LINE sl WITH (NOLOCK)
        ON sl.PACKLIST_ID = s.PACKLIST_ID
    WHERE s.SHIPPED_DATE >= @QUERY_START
      AND s.SHIPPED_DATE <  @END_DATE
      AND COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) IS NOT NULL
) candidate;

SELECT
    co.ID AS CUST_ORDER_ID,
    MAX(CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date)) AS PROMISE_SHIP_DATE
INTO #OrderCommitments
FROM #RelevantOrders ro
INNER JOIN dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    ON co.ID = ro.CUST_ORDER_ID
INNER JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON col.CUST_ORDER_ID = co.ID
WHERE col.PART_ID IS NOT NULL
  AND col.ORDER_QTY > 0
  AND ISNULL(co.STATUS, '') <> 'X'
  AND ISNULL(col.LINE_STATUS, '') <> 'X'
GROUP BY co.ID;

CREATE UNIQUE CLUSTERED INDEX IX_ShippingMetrics_Commitment
    ON #OrderCommitments (CUST_ORDER_ID);

SELECT
    COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) AS CUST_ORDER_ID,
    sl.CUST_ORDER_LINE_NO,
    s.PACKLIST_ID,
    sl.LINE_NO AS SHIPPER_LINE_NO,
    s.SHIPPED_DATE,
    s.STATUS AS SHIPPER_STATUS,
    CAST(COALESCE(sl.USER_SHIPPED_QTY, sl.SHIPPED_QTY, 0) AS decimal(18, 4)) AS SHIPPED_QTY,
    sl.TRANSACTION_ID
INTO #OrderShipmentLines
FROM dbo.SHIPPER s WITH (NOLOCK)
INNER JOIN dbo.SHIPPER_LINE sl WITH (NOLOCK)
    ON sl.PACKLIST_ID = s.PACKLIST_ID
INNER JOIN #RelevantOrders ro
    ON ro.CUST_ORDER_ID = COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
WHERE s.SHIPPED_DATE IS NOT NULL
  AND s.SHIPPED_DATE < @END_DATE
  AND ISNULL(s.STATUS, '') NOT IN ('X', 'V')
OPTION (RECOMPILE);

CREATE CLUSTERED INDEX IX_ShippingMetrics_OrderHistory
    ON #OrderShipmentLines (CUST_ORDER_ID, SHIPPED_DATE);

SELECT
    osl.CUST_ORDER_ID,
    MIN(CAST(osl.SHIPPED_DATE AS date)) AS FIRST_SHIP_DATE
INTO #FirstShip
FROM #OrderShipmentLines osl
GROUP BY osl.CUST_ORDER_ID;

CREATE UNIQUE CLUSTERED INDEX IX_ShippingMetrics_FirstShip
    ON #FirstShip (CUST_ORDER_ID);

SELECT
    oc.CUST_ORDER_ID,
    fs.FIRST_SHIP_DATE,
    SUM(CASE
        WHEN col.PART_ID IS NOT NULL
         AND ISNULL(col.LINE_STATUS, '') <> 'X'
         AND osl.SHIPPED_DATE < DATEADD(day, 1, oc.PROMISE_SHIP_DATE)
        THEN osl.SHIPPED_QTY
        ELSE 0
    END) AS SHIPPED_BY_PROMISE_QTY,
    SUM(CASE
        WHEN col.PART_ID IS NOT NULL
         AND ISNULL(col.LINE_STATUS, '') <> 'X'
         AND CAST(osl.SHIPPED_DATE AS date) = fs.FIRST_SHIP_DATE
        THEN osl.SHIPPED_QTY
        ELSE 0
    END) AS FIRST_DAY_SHIPPED_QTY
INTO #OrderShipping
FROM #OrderCommitments oc
LEFT JOIN #FirstShip fs
    ON fs.CUST_ORDER_ID = oc.CUST_ORDER_ID
LEFT JOIN #OrderShipmentLines osl
    ON osl.CUST_ORDER_ID = oc.CUST_ORDER_ID
LEFT JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON  col.CUST_ORDER_ID = osl.CUST_ORDER_ID
    AND col.LINE_NO = osl.CUST_ORDER_LINE_NO
GROUP BY oc.CUST_ORDER_ID, fs.FIRST_SHIP_DATE;

CREATE UNIQUE CLUSTERED INDEX IX_ShippingMetrics_OrderShipping
    ON #OrderShipping (CUST_ORDER_ID);

SELECT
    osl.CUST_ORDER_ID,
    osl.CUST_ORDER_LINE_NO,
    co.CUSTOMER_ID,
    c.NAME AS CUSTOMER_NAME,
    col.PART_ID,
    p.PRODUCT_CODE,
    osl.PACKLIST_ID,
    osl.SHIPPER_LINE_NO,
    osl.SHIPPED_DATE,
    osl.SHIPPER_STATUS,
    osl.SHIPPED_QTY,
    osl.TRANSACTION_ID
INTO #ShipmentFacts
FROM #OrderShipmentLines osl
LEFT JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON  col.CUST_ORDER_ID = osl.CUST_ORDER_ID
    AND col.LINE_NO = osl.CUST_ORDER_LINE_NO
LEFT JOIN dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    ON co.ID = osl.CUST_ORDER_ID
LEFT JOIN dbo.CUSTOMER c WITH (NOLOCK)
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = col.PART_ID
WHERE osl.SHIPPED_DATE >= @QUERY_START
OPTION (RECOMPILE);

CREATE INDEX IX_ShippingMetrics_Transaction
    ON #ShipmentFacts (TRANSACTION_ID);

SELECT
    tx.TRANSACTION_ID,
    COUNT(DISTINCT t.ID) AS FIREARM_QTY
INTO #TraceQty
FROM (
    SELECT DISTINCT TRANSACTION_ID
    FROM #ShipmentFacts
    WHERE TRANSACTION_ID IS NOT NULL
) tx
INNER JOIN dbo.TRACE_INV_TRANS tit WITH (NOLOCK)
    ON tit.TRANSACTION_ID = tx.TRANSACTION_ID
INNER JOIN dbo.TRACE t WITH (NOLOCK)
    ON  t.PART_ID = tit.PART_ID
    AND t.ID = tit.TRACE_ID
GROUP BY tx.TRANSACTION_ID;

CREATE UNIQUE CLUSTERED INDEX IX_ShippingMetrics_Trace
    ON #TraceQty (TRANSACTION_ID);

SELECT
    CAST('ORDER_LINE' AS varchar(20)) AS RECORD_TYPE,
    co.ID AS CUST_ORDER_ID,
    col.LINE_NO AS CUST_ORDER_LINE_NO,
    co.CUSTOMER_ID,
    c.NAME AS CUSTOMER_NAME,
    col.PART_ID,
    p.PRODUCT_CODE,
    CAST(col.ORDER_QTY AS decimal(18, 4)) AS ORDER_QTY,
    CAST(COALESCE(col.PROMISE_DATE, co.PROMISE_DATE) AS date) AS PROMISE_SHIP_DATE,
    CAST(NULL AS varchar(30)) AS PACKLIST_ID,
    CAST(NULL AS int) AS SHIPPER_LINE_NO,
    CAST(NULL AS datetime) AS SHIPPED_DATE,
    CAST(NULL AS varchar(5)) AS SHIPPER_STATUS,
    CAST(NULL AS decimal(18, 4)) AS SHIPPED_QTY,
    CAST(NULL AS int) AS FIREARM_QTY,
    osf.FIRST_SHIP_DATE AS ORDER_FIRST_SHIP_DATE,
    osf.SHIPPED_BY_PROMISE_QTY AS ORDER_SHIPPED_BY_PROMISE_QTY,
    osf.FIRST_DAY_SHIPPED_QTY AS ORDER_FIRST_DAY_SHIPPED_QTY
FROM #RelevantOrders ro
INNER JOIN dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    ON co.ID = ro.CUST_ORDER_ID
INNER JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON col.CUST_ORDER_ID = co.ID
LEFT JOIN dbo.CUSTOMER c WITH (NOLOCK)
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = col.PART_ID
LEFT JOIN #OrderShipping osf
    ON osf.CUST_ORDER_ID = co.ID
WHERE col.PART_ID IS NOT NULL
  AND col.ORDER_QTY > 0
  AND ISNULL(co.STATUS, '') <> 'X'
  AND ISNULL(col.LINE_STATUS, '') <> 'X'

UNION ALL

SELECT
    CAST('SHIPMENT_LINE' AS varchar(20)) AS RECORD_TYPE,
    sf.CUST_ORDER_ID,
    sf.CUST_ORDER_LINE_NO,
    sf.CUSTOMER_ID,
    sf.CUSTOMER_NAME,
    sf.PART_ID,
    sf.PRODUCT_CODE,
    CAST(NULL AS decimal(18, 4)) AS ORDER_QTY,
    CAST(NULL AS date) AS PROMISE_SHIP_DATE,
    sf.PACKLIST_ID,
    sf.SHIPPER_LINE_NO,
    sf.SHIPPED_DATE,
    sf.SHIPPER_STATUS,
    sf.SHIPPED_QTY,
    ISNULL(tq.FIREARM_QTY, 0) AS FIREARM_QTY,
    CAST(NULL AS date) AS ORDER_FIRST_SHIP_DATE,
    CAST(NULL AS decimal(18, 4)) AS ORDER_SHIPPED_BY_PROMISE_QTY,
    CAST(NULL AS decimal(18, 4)) AS ORDER_FIRST_DAY_SHIPPED_QTY
FROM #ShipmentFacts sf
LEFT JOIN #TraceQty tq
    ON tq.TRANSACTION_ID = sf.TRANSACTION_ID;
