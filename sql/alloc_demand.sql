/*
===============================================================================
  ALLOCATION SCREEN — ALL OPEN DEMAND FOR ONE SKU
===============================================================================
  Every open customer-order line for :part_id, with the raw header- and
  line-level dates and the facts the eligibility rules need. Deliberately
  broader than the picklist:

    * no horizon cut — the screen shows everything and marks what falls
      inside the picklist window;
    * minimal eligibility filtering — credit-held / firmed-not-released /
      RMA / excluded lines come back too, with the facts (ORDER_STATUS,
      CREDIT_STATUS, ...) as columns so allocation.py can badge the reason
      instead of hiding the line. Closed and cancelled orders are NOT open
      backlog and are excluded here: VISUAL leaves LINE_STATUS = 'A' on
      lines of closed orders (35k+ such lines back to 2011), so the header
      status is the real open-order test. 'R' released / 'F' firmed (same
      rule as sql-toolbox so_header_and_lines_open_orders.sql) plus 'H'
      held — held orders are open backlog Inside Sales must see, badged;
    * no COALESCE on dates — allocation.py owns the line->header fallback so
      what-if previews can override the line value.

  Bind parameters:  :part_id
  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    co.ID                                   AS CUST_ORDER_ID,
    col.LINE_NO,
    co.CUSTOMER_ID,
    c.NAME                                  AS CUSTOMER_NAME,
    CAST(co.ORDER_DATE AS date)             AS ORDER_DATE,
    CAST(col.ORDER_QTY AS int)              AS ORDER_QTY,
    CAST(col.TOTAL_SHIPPED_QTY AS int)      AS SHIPPED_QTY,
    CAST(col.ORDER_QTY - col.TOTAL_SHIPPED_QTY AS int) AS OPEN_QTY,

    CAST(co.DESIRED_SHIP_DATE AS date)      AS HDR_DESIRED_SHIP_DATE,
    CAST(col.DESIRED_SHIP_DATE AS date)     AS LINE_DESIRED_SHIP_DATE,
    CAST(co.PROMISE_DATE AS date)           AS HDR_PROMISE_SHIP_DATE,
    CAST(col.PROMISE_DATE AS date)          AS LINE_PROMISE_SHIP_DATE,
    CAST(co.PROMISE_DEL_DATE AS date)       AS HDR_PROMISE_DEL_DATE,
    CAST(col.PROMISE_DEL_DATE AS date)      AS LINE_PROMISE_DEL_DATE,

    co.STATUS                               AS ORDER_STATUS,
    col.LINE_STATUS,
    ce.CREDIT_STATUS,
    co.SALESREP_ID,
    co.CUSTOMER_PO_REF,
    c.DISCOUNT_CODE,

    /* Make-to-order pegging: WOs tied to this line via DEMAND_SUPPLY_LINK.
       alloc_supply.sql drops those WOs from the shared pool, so the pegged
       qty must also come out of this line's pool demand or the line would
       double-dip (consume a shared unit it does not need). Open WOs only —
       a closed linked WO means the unit was received/shipped already. */
    lw.LINKED_WO_QTY,
    lw.LINKED_WO_DATE,
    lw.LINKED_WO_UNRELEASED,
    STUFF((
        SELECT ', ' + dsl2.SUPPLY_BASE_ID + '/' + dsl2.SUPPLY_LOT_ID
               + ' (' + wo2.STATUS + ')'
        FROM dbo.DEMAND_SUPPLY_LINK dsl2
        JOIN dbo.WORK_ORDER wo2
            ON  wo2.TYPE     = 'W'
            AND wo2.BASE_ID  = dsl2.SUPPLY_BASE_ID
            AND wo2.LOT_ID   = dsl2.SUPPLY_LOT_ID
            AND wo2.SPLIT_ID = dsl2.SUPPLY_SPLIT_ID
            AND wo2.SUB_ID   = dsl2.SUPPLY_SUB_ID
        WHERE dsl2.SUPPLY_TYPE    = 'WO'
          AND dsl2.DEMAND_BASE_ID = col.CUST_ORDER_ID
          AND dsl2.DEMAND_SEQ_NO  = col.LINE_NO
          AND wo2.STATUS IN ('U', 'F', 'R')
        ORDER BY dsl2.SUPPLY_BASE_ID, dsl2.SUPPLY_LOT_ID
        FOR XML PATH('')
    ), 1, 2, '')                            AS LINKED_WO_IDS
FROM dbo.CUST_ORDER_LINE col
JOIN dbo.CUSTOMER_ORDER co
    ON col.CUST_ORDER_ID = co.ID
JOIN dbo.CUSTOMER c
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.CUSTOMER_ENTITY ce
    ON ce.CUSTOMER_ID = c.ID
OUTER APPLY (
    SELECT
        CAST(SUM(dsl.ALLOCATED_QTY) AS int)                     AS LINKED_WO_QTY,
        MIN(CAST(COALESCE(wo.SCHED_FINISH_DATE,
                          wo.DESIRED_WANT_DATE) AS date))       AS LINKED_WO_DATE,
        SUM(CASE WHEN wo.STATUS = 'R' THEN 0 ELSE 1 END)        AS LINKED_WO_UNRELEASED
    FROM dbo.DEMAND_SUPPLY_LINK dsl
    JOIN dbo.WORK_ORDER wo
        ON  wo.TYPE     = 'W'
        AND wo.BASE_ID  = dsl.SUPPLY_BASE_ID
        AND wo.LOT_ID   = dsl.SUPPLY_LOT_ID
        AND wo.SPLIT_ID = dsl.SUPPLY_SPLIT_ID
        AND wo.SUB_ID   = dsl.SUPPLY_SUB_ID
    WHERE dsl.SUPPLY_TYPE    = 'WO'
      AND dsl.DEMAND_BASE_ID = col.CUST_ORDER_ID
      AND dsl.DEMAND_SEQ_NO  = col.LINE_NO
      AND wo.STATUS IN ('U', 'F', 'R')
) lw
WHERE col.PART_ID = :part_id
  AND co.STATUS IN ('R', 'F', 'H')
  AND col.LINE_STATUS = 'A'
  AND (col.ORDER_QTY - col.TOTAL_SHIPPED_QTY) > 0
ORDER BY co.ORDER_DATE, co.ID, col.LINE_NO;
