-- dispatch.inventory-movements: COMPLETE physical boundary history.
-- TRANSACTION_ID is the business PK, not a monotonic commit watermark.
-- POSTING_CANDIDATE is an accounting candidate flag, not a physical commit/void
-- status; both Y and N occur on valid movements. No such filter is applied.
-- QTY is stocking UOM; TYPE gives its direction. Trace quantity is already signed.
SELECT it.TRANSACTION_ID, it.PART_ID, psv.STOCK_UM,
       tp.PART_ID AS TRACE_PROFILE_PART_ID, it.TYPE, it.CLASS, it.QTY,
       it.TRANSACTION_DATE, it.CREATE_DATE,
       sl.PACKLIST_ID, sl.LINE_NO AS PACKLIST_LINE_NO,
       it.XFER_IN_TRANSIT, it.TRANSFER_TRANS_ID, it.CUST_ORDER_ID, it.CUST_ORDER_LINE_NO,
       it.PURC_ORDER_ID, it.PURC_ORDER_LINE_NO,
       it.WORKORDER_TYPE, it.WORKORDER_BASE_ID, it.WORKORDER_LOT_ID,
       it.WORKORDER_SPLIT_ID, it.WORKORDER_SUB_ID,
       CASE WHEN EXISTS (
           SELECT 1 FROM dbo.WORK_ORDER wo
           WHERE wo.TYPE = 'W' AND wo.TYPE = it.WORKORDER_TYPE
             AND wo.BASE_ID = it.WORKORDER_BASE_ID AND wo.LOT_ID = it.WORKORDER_LOT_ID
             AND wo.SPLIT_ID = it.WORKORDER_SPLIT_ID AND wo.SUB_ID = it.WORKORDER_SUB_ID
             AND wo.SITE_ID = it.SITE_ID AND wo.PART_ID = it.PART_ID
       ) THEN 1 ELSE 0 END AS VERIFIED_WORK_ORDER,
       CASE WHEN EXISTS (
           SELECT 1 FROM dbo.PURCHASE_ORDER po
           JOIN dbo.PURC_ORDER_LINE pol ON pol.PURC_ORDER_ID = po.ID
           WHERE po.ID = it.PURC_ORDER_ID AND pol.LINE_NO = it.PURC_ORDER_LINE_NO
             AND po.SITE_ID = it.SITE_ID AND pol.PART_ID = it.PART_ID
       ) THEN 1 ELSE 0 END AS VERIFIED_PURCHASE_ORDER_LINE,
       tit.TRACE_ID, tit.QTY AS TRACE_QTY,
       pair.TRANSACTION_ID AS PAIR_TRANSACTION_ID,
       pair.TRANSFER_TRANS_ID AS PAIR_TRANSFER_TRANS_ID,
       pair.TYPE AS PAIR_TYPE, pair.CLASS AS PAIR_CLASS,
       pair.PART_ID AS PAIR_PART_ID, pair.QTY AS PAIR_QTY,
       pair.SITE_ID AS PAIR_SITE_ID, pair.WAREHOUSE_ID AS PAIR_WAREHOUSE_ID,
       pair.LOCATION_ID AS PAIR_LOCATION_ID, pair.XFER_IN_TRANSIT AS PAIR_XFER_IN_TRANSIT
FROM dbo.INVENTORY_TRANS it
LEFT JOIN dbo.PART_SITE_VIEW psv ON psv.SITE_ID = it.SITE_ID AND psv.PART_ID = it.PART_ID
LEFT JOIN dbo.TRACE_PROFILE tp ON tp.SITE_ID = it.SITE_ID AND tp.PART_ID = it.PART_ID
LEFT JOIN dbo.TRACE_INV_TRANS tit
  ON tit.TRANSACTION_ID = it.TRANSACTION_ID AND tit.PART_ID = it.PART_ID
LEFT JOIN dbo.SHIPPER_LINE sl ON sl.TRANSACTION_ID = it.TRANSACTION_ID
LEFT JOIN dbo.INVENTORY_TRANS pair ON pair.TRANSACTION_ID = it.TRANSFER_TRANS_ID
WHERE it.SITE_ID = :site AND it.WAREHOUSE_ID = :warehouse AND it.LOCATION_ID = :location
  AND it.PART_ID IS NOT NULL
ORDER BY it.TRANSACTION_ID, tit.TRACE_ID;
