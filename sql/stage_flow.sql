/*
===============================================================================
  STAGE FLOW — SERIALIZED UNITS INTO AND OUT OF THE SHIPPING STAGE BINS
===============================================================================
  Answers "how many guns went into stage, and how many left, each day?" for
  every SHIPPING location whose ID contains :stage_term (STAGE, STAGE1,
  STAGE2, STAGE3, ...). Same term the app's stage on-hand count uses
  (STAGE_LOCATION_TERM).

  One row per (day, movement kind). Stage-bin movement kinds:
    ARRIVED      into a stage bin from outside stage: a location transfer
                 (CLASS A) from a rack / the cage, or a work-order receipt
                 (CLASS R) straight into stage.
    SHIPPED      issued out of a stage bin on a customer order
                 (TYPE O, CLASS I, CUST_ORDER_ID set).
    UNSHIPPED    a shipment reversed / returned back into a stage bin
                 (TYPE I, CLASS I, CUST_ORDER_ID set). Not new arrivals.
    MOVED_OUT    left a stage bin for a non-stage bin without shipping
                 (transfer back to a rack or the cage, work-order issue).
    REVERSED     a receipt into stage reversed (TYPE O, CLASS R). Offsets
                 ARRIVED.
    RESHUFFLED   stage-to-stage move (STAGE1 -> STAGE3), outbound half only.
                 Never counted as in or out.
  Plus one company-wide kind, so stage can be read against real output:
    SHIPPED_ALL  serialized units issued on a customer order from ANY
                 location that day (racks, stage, cage, ...).

  VISUAL has no link between the two halves of a location transfer. A CLASS A
  row is paired by serial: an opposite-signed CLASS A TRACE_INV_TRANS row for
  the same (part, serial) in another stage bin, created within 5 seconds.
  Verified 2026-11-10 against live data: 30 of 37 non-ship outbound rows
  since 11/1 were stage-to-stage, and the in/out halves balance exactly.

  Day = CAST(CREATE_DATE AS date) on the ERP server clock (CREATE_DATE carries
  the real timestamp; TRANSACTION_DATE is effectively date-only).

  Params: :days        how many days back from today (inclusive of today)
          :stage_term  location substring that marks a stage bin (upper case)
  Output: FLOW_DATE, MOVE_KIND, QTY, ORDERS, ERP_TODAY
  SQL Server 2016 / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

WITH Params AS (
    SELECT
        DATEADD(day, 1 - :days, CAST(GETDATE() AS date)) AS FROM_DATE,
        '%' + UPPER(:stage_term) + '%'                   AS STAGE_LIKE
),
StageTx AS (
    SELECT
        tit.PART_ID,
        tit.TRACE_ID,
        tit.QTY,
        it.TYPE,
        it.CLASS,
        it.CREATE_DATE,
        it.LOCATION_ID,
        it.CUST_ORDER_ID
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID        = tit.PART_ID
    CROSS JOIN Params prm
    WHERE it.WAREHOUSE_ID = 'SHIPPING'
      AND UPPER(COALESCE(it.LOCATION_ID, '')) LIKE prm.STAGE_LIKE
      AND tit.QTY <> 0
      AND it.CREATE_DATE >= prm.FROM_DATE
),
Classified AS (
    SELECT
        CAST(s.CREATE_DATE AS date) AS FLOW_DATE,
        s.CUST_ORDER_ID,
        ABS(s.QTY) AS QTY,
        CASE
            WHEN s.CLASS = 'I' AND s.QTY < 0 AND s.CUST_ORDER_ID IS NOT NULL THEN 'SHIPPED'
            WHEN s.CLASS = 'I' AND s.QTY > 0 AND s.CUST_ORDER_ID IS NOT NULL THEN 'UNSHIPPED'
            WHEN s.CLASS = 'R' AND s.QTY < 0 THEN 'REVERSED'
            WHEN s.CLASS = 'A' AND EXISTS (
                -- The other half of a transfer, in a different stage bin.
                SELECT 1
                FROM dbo.TRACE_INV_TRANS t2
                INNER JOIN dbo.INVENTORY_TRANS i2
                    ON i2.TRANSACTION_ID = t2.TRANSACTION_ID
                   AND i2.PART_ID        = t2.PART_ID
                CROSS JOIN Params prm
                WHERE t2.PART_ID  = s.PART_ID
                  AND t2.TRACE_ID = s.TRACE_ID
                  AND SIGN(t2.QTY) = -SIGN(s.QTY)
                  AND i2.CLASS = 'A'
                  AND i2.WAREHOUSE_ID = 'SHIPPING'
                  AND UPPER(COALESCE(i2.LOCATION_ID, '')) LIKE prm.STAGE_LIKE
                  AND UPPER(COALESCE(i2.LOCATION_ID, '')) <> UPPER(COALESCE(s.LOCATION_ID, ''))
                  AND i2.CREATE_DATE BETWEEN DATEADD(second, -5, s.CREATE_DATE)
                                         AND DATEADD(second,  5, s.CREATE_DATE)
            ) THEN CASE WHEN s.QTY < 0 THEN 'RESHUFFLED' ELSE 'RESHUFFLED_IN' END
            WHEN s.QTY > 0 THEN 'ARRIVED'
            ELSE 'MOVED_OUT'
        END AS MOVE_KIND
    FROM StageTx s
),
AllShipped AS (
    SELECT
        CAST(it.CREATE_DATE AS date) AS FLOW_DATE,
        it.CUST_ORDER_ID,
        -tit.QTY AS QTY
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID        = tit.PART_ID
    CROSS JOIN Params prm
    WHERE it.TYPE = 'O'
      AND it.CLASS = 'I'
      AND it.CUST_ORDER_ID IS NOT NULL
      AND tit.QTY < 0
      AND it.CREATE_DATE >= prm.FROM_DATE
)
SELECT FLOW_DATE, MOVE_KIND, SUM(QTY) AS QTY, COUNT(DISTINCT CUST_ORDER_ID) AS ORDERS,
       CAST(GETDATE() AS date) AS ERP_TODAY
FROM Classified
WHERE MOVE_KIND <> 'RESHUFFLED_IN'
GROUP BY FLOW_DATE, MOVE_KIND
UNION ALL
SELECT FLOW_DATE, 'SHIPPED_ALL', SUM(QTY), COUNT(DISTINCT CUST_ORDER_ID),
       CAST(GETDATE() AS date)
FROM AllShipped
GROUP BY FLOW_DATE
ORDER BY FLOW_DATE, MOVE_KIND;
