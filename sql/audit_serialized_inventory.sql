/*
===============================================================================
  SERIALIZED INVENTORY AUDIT — EXPECTED LIST
===============================================================================
  Produces the list of serialized firearms the system expects to be in
  Shipping's custody, for the daily scan-based audit. One row per serial.

  Two sources are UNIONed:

    Source A  On-hand serials physically resident in an audited shipping bin.
              Current location is INFERRED (VISUAL stores no "current location"
              on a serial): sum TRACE_INV_TRANS.QTY grouped by the linked
              INVENTORY_TRANS warehouse/location, keep HAVING SUM(qty) > 0.
              Audited bins:
                MAIN / C2               (Cage 2 - shipping cage)
                MAIN / C2-SERIALIZED    (serialized cage)
                SHIPPING / R01..R09     (serialized racks; excl. Stage/Intl)

    Source B  Serials tied to an OPEN (not-yet-shipped) customer order through a
              COMPLETED work order — built and in the cage but possibly not yet
              put away into an audited bin. Peg = DEMAND_SUPPLY_LINK CO->WO; the
              serial is the traced component issued to the WO (INVENTORY_TRANS
              TYPE='O'/CLASS='I' with a TRACE_INV_TRANS row). "Complete" = no
              OPERATION row still open (STATUS in U/F/R). Excludes RMA REPAIR and
              cancelled WOs. Serials already resident in an audited bin (Source A)
              are NOT duplicated — they stay in their Source-A row and are flagged
              TIED_WO = 1. Tied serials with no audited-bin residence appear under
              SCOPE = 'TIED-WO' with their inferred current location (or NULL).

  Output columns (one row per serial):
    SERIAL_NO, PART_ID, PART_DESCRIPTION, PRODUCT_CODE,
    EXPECTED_WAREHOUSE, EXPECTED_LOCATION, SCOPE, TIED_WO,
    CUST_ORDER_ID, CUSTOMER_ID

  SCOPE is one of: 'C2', 'C2-SERIALIZED', 'SHIPPING-RACKS', 'TIED-WO'.

  __AUDIT_SCOPE_FILTER__ is string-substituted by app.py (render_audit_query):
    - empty string            => all scopes (full audit)
    - "AND c.SCOPE = '<scope>'" => single scope

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

WITH TraceLoc AS (
    -- Current inferred on-hand location balance per (part, serial/trace).
    SELECT
        tit.PART_ID,
        tit.TRACE_ID,
        it.WAREHOUSE_ID,
        it.LOCATION_ID,
        SUM(tit.QTY) AS NET_QTY
    FROM dbo.TRACE_INV_TRANS tit
    INNER JOIN dbo.INVENTORY_TRANS it
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID        = tit.PART_ID
    GROUP BY tit.PART_ID, tit.TRACE_ID, it.WAREHOUSE_ID, it.LOCATION_ID
    HAVING SUM(tit.QTY) > 0
),

OnHand AS (
    -- Source A: serials physically resident in an audited shipping bin.
    SELECT
        tl.TRACE_ID     AS SERIAL_NO,
        tl.PART_ID      AS PART_ID,
        tl.WAREHOUSE_ID AS EXPECTED_WAREHOUSE,
        tl.LOCATION_ID  AS EXPECTED_LOCATION,
        CASE
            WHEN tl.WAREHOUSE_ID = 'MAIN' AND tl.LOCATION_ID = 'C2'            THEN 'C2'
            WHEN tl.WAREHOUSE_ID = 'MAIN' AND tl.LOCATION_ID = 'C2-SERIALIZED' THEN 'C2-SERIALIZED'
            ELSE 'SHIPPING-RACKS'
        END AS SCOPE
    FROM TraceLoc tl
    WHERE (tl.WAREHOUSE_ID = 'MAIN' AND tl.LOCATION_ID IN ('C2', 'C2-SERIALIZED'))
       OR (tl.WAREHOUSE_ID = 'SHIPPING'
           AND LEFT(tl.LOCATION_ID, 3) BETWEEN 'R01' AND 'R09'
           AND UPPER(COALESCE(tl.LOCATION_ID, '')) NOT LIKE '%STAGE%'
           AND UPPER(COALESCE(tl.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%')
),

Tied AS (
    -- Source B (raw): serial-issued, ops-complete, unshipped tied units.
    SELECT
        ser.SERIAL_NO,
        col.PART_ID,
        col.CUST_ORDER_ID,
        co.CUSTOMER_ID
    FROM dbo.DEMAND_SUPPLY_LINK dsl
    INNER JOIN dbo.WORK_ORDER wo
        ON  wo.TYPE     = 'W'
        AND wo.BASE_ID  = dsl.SUPPLY_BASE_ID
        AND wo.LOT_ID   = dsl.SUPPLY_LOT_ID
        AND wo.SPLIT_ID = dsl.SUPPLY_SPLIT_ID
        AND wo.SUB_ID   = dsl.SUPPLY_SUB_ID
    INNER JOIN dbo.CUST_ORDER_LINE col
        ON  col.CUST_ORDER_ID = dsl.DEMAND_BASE_ID
        AND col.LINE_NO       = dsl.DEMAND_SEQ_NO
    INNER JOIN dbo.CUSTOMER_ORDER co
        ON co.ID = col.CUST_ORDER_ID
    OUTER APPLY (
        SELECT
            COUNT(*)                                                   AS TOTAL_OPS,
            SUM(CASE WHEN op.STATUS IN ('U','F','R') THEN 1 ELSE 0 END) AS OPEN_OPS
        FROM dbo.OPERATION op
        WHERE op.WORKORDER_TYPE     = wo.TYPE
          AND op.WORKORDER_BASE_ID  = wo.BASE_ID
          AND op.WORKORDER_LOT_ID   = wo.LOT_ID
          AND op.WORKORDER_SPLIT_ID = wo.SPLIT_ID
          AND op.WORKORDER_SUB_ID   = wo.SUB_ID
    ) ops
    OUTER APPLY (
        SELECT TOP 1 tit.TRACE_ID AS SERIAL_NO
        FROM dbo.INVENTORY_TRANS it
        INNER JOIN dbo.TRACE_INV_TRANS tit
            ON  tit.TRANSACTION_ID = it.TRANSACTION_ID
            AND tit.PART_ID        = it.PART_ID
        WHERE it.WORKORDER_TYPE     = wo.TYPE
          AND it.WORKORDER_BASE_ID  = wo.BASE_ID
          AND it.WORKORDER_LOT_ID   = wo.LOT_ID
          AND it.WORKORDER_SPLIT_ID = wo.SPLIT_ID
          AND it.WORKORDER_SUB_ID   = wo.SUB_ID
          AND it.TYPE  = 'O'
          AND it.CLASS = 'I'
        ORDER BY it.TRANSACTION_DATE DESC
    ) ser
    WHERE dsl.DEMAND_TYPE = 'CO'
      AND dsl.SUPPLY_TYPE = 'WO'
      AND col.LINE_STATUS = 'A'
      AND col.TOTAL_SHIPPED_QTY < col.ORDER_QTY    -- not fully shipped
      AND co.STATUS NOT IN ('C','X')               -- open orders only
      AND (col.PART_ID IS NULL OR col.PART_ID <> 'RMA REPAIR')
      AND wo.STATUS <> 'X'
      AND ops.TOTAL_OPS > 0 AND ops.OPEN_OPS = 0   -- all ops complete
      AND ser.SERIAL_NO IS NOT NULL                -- serial issued
),

TiedDedup AS (
    -- One tied row per serial (a serial pegs a single build here; guard anyway).
    SELECT
        SERIAL_NO, PART_ID, CUST_ORDER_ID, CUSTOMER_ID,
        ROW_NUMBER() OVER (PARTITION BY SERIAL_NO ORDER BY CUST_ORDER_ID) AS RN
    FROM Tied
),

Combined AS (
    -- Source A rows, flagged when the serial is also tied to an open order.
    SELECT
        oh.SERIAL_NO,
        oh.PART_ID,
        oh.EXPECTED_WAREHOUSE,
        oh.EXPECTED_LOCATION,
        oh.SCOPE,
        CASE WHEN td.SERIAL_NO IS NOT NULL THEN 1 ELSE 0 END AS TIED_WO,
        td.CUST_ORDER_ID,
        td.CUSTOMER_ID
    FROM OnHand oh
    LEFT JOIN TiedDedup td
        ON td.SERIAL_NO = oh.SERIAL_NO AND td.RN = 1

    UNION ALL

    -- Source B rows: tied serials not resident in any audited bin.
    SELECT
        td.SERIAL_NO,
        td.PART_ID,
        loc.WAREHOUSE_ID AS EXPECTED_WAREHOUSE,
        loc.LOCATION_ID  AS EXPECTED_LOCATION,
        'TIED-WO'        AS SCOPE,
        1                AS TIED_WO,
        td.CUST_ORDER_ID,
        td.CUSTOMER_ID
    FROM TiedDedup td
    OUTER APPLY (
        SELECT TOP 1 tl.WAREHOUSE_ID, tl.LOCATION_ID
        FROM TraceLoc tl
        WHERE tl.TRACE_ID = td.SERIAL_NO
        ORDER BY tl.NET_QTY DESC
    ) loc
    WHERE td.RN = 1
      AND NOT EXISTS (SELECT 1 FROM OnHand oh WHERE oh.SERIAL_NO = td.SERIAL_NO)
)

SELECT
    c.SERIAL_NO,
    c.PART_ID,
    p.DESCRIPTION   AS PART_DESCRIPTION,
    p.PRODUCT_CODE,
    c.EXPECTED_WAREHOUSE,
    c.EXPECTED_LOCATION,
    c.SCOPE,
    c.TIED_WO,
    c.CUST_ORDER_ID,
    c.CUSTOMER_ID
FROM Combined c
LEFT JOIN dbo.PART p
    ON p.ID = c.PART_ID
WHERE 1 = 1
    __AUDIT_SCOPE_FILTER__
ORDER BY
    c.SCOPE,
    c.EXPECTED_LOCATION,
    c.SERIAL_NO;
