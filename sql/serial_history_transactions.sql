/*
===============================================================================
  SERIAL NUMBER HISTORY — INVENTORY TRANSACTIONS
===============================================================================
  Every inventory transaction that ever touched the serial, in chronological
  order, with the work-order and location context needed to explain each one.
  Raw ledger rows only — event classification (built / moved / shipped /
  adjusted) happens in the app (serial_history.py), not here.

  Reading the rows:
    TRACE_QTY          signed serial-level quantity (+in / -out of the bin).
    TYPE / CLASS       direction (I/O) and kind (R receipt, I issue,
                       A adjustment, C correction). NOTE: bin-to-bin transfers
                       are PAIRED CLASS='A' rows linked reciprocally via
                       TRANSFER_TRANS_ID.
    WORKORDER_* keys   populated when the txn belongs to a work order
                       (inbound CLASS='R' = build received into stock).
    PURC_ORDER_ID      populated on purchase-order receipts.
    CUST_ORDER_ID      populated on customer-order issues (shipments).
    USER_ID            the VISUAL login that performed the transaction.

  Bind parameters:
    :serial  — the serial number to look up (exact match, uppercased by app).

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    t.PART_ID,
    t.ID                       AS TRACE_ID,
    tit.TRANSACTION_ID,
    tit.QTY                    AS TRACE_QTY,
    it.TRANSACTION_DATE,
    it.CREATE_DATE,
    it.USER_ID,
    it.TYPE,
    it.CLASS,
    it.QTY                     AS TRANS_QTY,
    it.WAREHOUSE_ID,
    it.LOCATION_ID,
    loc.DESCRIPTION            AS LOCATION_DESCRIPTION,
    it.DESCRIPTION             AS TRANS_DESCRIPTION,
    it.TRANSFER_TRANS_ID,
    it.WORKORDER_TYPE,
    it.WORKORDER_BASE_ID,
    it.WORKORDER_LOT_ID,
    it.WORKORDER_SPLIT_ID,
    it.WORKORDER_SUB_ID,
    wo.PART_ID                 AS WO_PART_ID,
    wo.STATUS                  AS WO_STATUS,
    wo.CREATE_DATE             AS WO_CREATE_DATE,
    wo.CLOSE_DATE              AS WO_CLOSE_DATE,
    it.PURC_ORDER_ID,
    it.PURC_ORDER_LINE_NO,
    it.CUST_ORDER_ID,
    it.CUST_ORDER_LINE_NO
FROM dbo.TRACE t
INNER JOIN dbo.TRACE_INV_TRANS tit
    ON  tit.PART_ID  = t.PART_ID
    AND tit.TRACE_ID = t.ID
INNER JOIN dbo.INVENTORY_TRANS it
    ON  it.TRANSACTION_ID = tit.TRANSACTION_ID
    AND it.PART_ID        = tit.PART_ID
LEFT JOIN dbo.WORK_ORDER wo
    ON  wo.TYPE     = it.WORKORDER_TYPE
    AND wo.BASE_ID  = it.WORKORDER_BASE_ID
    AND wo.LOT_ID   = it.WORKORDER_LOT_ID
    AND wo.SPLIT_ID = it.WORKORDER_SPLIT_ID
    AND wo.SUB_ID   = it.WORKORDER_SUB_ID
LEFT JOIN dbo.LOCATION loc
    ON  loc.WAREHOUSE_ID = it.WAREHOUSE_ID
    AND loc.ID           = it.LOCATION_ID
WHERE t.ID = :serial
   OR t.SERIAL_ID = :serial
ORDER BY it.TRANSACTION_DATE, tit.TRANSACTION_ID;
