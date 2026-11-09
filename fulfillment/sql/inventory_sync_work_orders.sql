-- dispatch.inventory-work-orders: retain closed/cancelled/linked/sub-order rows
-- so the bridge can reduce former promises. Only F/R top-level, unpegged supply
-- is free future manufacturing output. Receipt into Shipping is a separate fact.
SELECT wo.TYPE, wo.BASE_ID, wo.LOT_ID, wo.SPLIT_ID, wo.SUB_ID, wo.PART_ID,
       psv.STOCK_UM, tp.PART_ID AS TRACE_PROFILE_PART_ID,
       wo.DESIRED_QTY, wo.RECEIVED_QTY, wo.STATUS,
       COALESCE(wo.SCHED_FINISH_DATE, wo.DESIRED_WANT_DATE) AS DUE_DATE,
       CASE WHEN EXISTS (
           SELECT 1 FROM dbo.DEMAND_SUPPLY_LINK dsl
           WHERE dsl.SUPPLY_TYPE = 'WO'
             AND dsl.SUPPLY_BASE_ID = wo.BASE_ID AND dsl.SUPPLY_LOT_ID = wo.LOT_ID
             AND dsl.SUPPLY_SPLIT_ID = wo.SPLIT_ID AND dsl.SUPPLY_SUB_ID = wo.SUB_ID
       ) THEN 1 ELSE 0 END AS HAS_DEMAND_LINK
FROM dbo.WORK_ORDER wo
LEFT JOIN dbo.PART_SITE_VIEW psv ON psv.SITE_ID = wo.SITE_ID AND psv.PART_ID = wo.PART_ID
LEFT JOIN dbo.TRACE_PROFILE tp ON tp.SITE_ID = wo.SITE_ID AND tp.PART_ID = wo.PART_ID
WHERE wo.SITE_ID = :site AND wo.TYPE = 'W' AND wo.PART_ID IS NOT NULL
ORDER BY wo.TYPE, wo.BASE_ID, wo.LOT_ID, wo.SPLIT_ID, wo.SUB_ID;
