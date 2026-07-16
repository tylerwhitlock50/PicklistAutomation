/*
===============================================================================
  PACKLIST VERIFICATION — EXPECTED SERIALS
===============================================================================
  Every line of one packlist with the serial numbers that shipped on it, so
  the app can verify a scanned box against what the ERP says left on that
  packlist. One row per serial; lines with no TRACE rows (non-serialized
  components, accessories) come back once with NULL TRACE_ID and are shown as
  qty-only "visual check" rows.

  Part resolved through CUST_ORDER_LINE (SHIPPER_LINE has no PART_ID) and the
  customer through CUSTOMER_ORDER — same conventions as recon_shipments.sql.
  Serials come from SHIPPER_LINE.TRANSACTION_ID → TRACE_INV_TRANS → TRACE,
  the same join serial_history_shipments.sql uses in reverse. TRACE.ID is the
  gun serial; TRACE.SERIAL_ID is usually NULL but returned so scans can match
  either.

  Voided/cancelled shippers (STATUS 'X'/'V') are RETURNED, not filtered — the
  app refuses to start a verification session on them but can say why.

  Bind parameters:
    :packlist — the packlist ID (exact match, uppercased by app, e.g. PL-283888)

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    sl.PACKLIST_ID,
    sl.LINE_NO,
    s.STATUS                                    AS SHIPPER_STATUS,
    s.CREATE_DATE,
    s.SHIPPED_DATE,
    COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) AS CUST_ORDER_ID,
    sl.CUST_ORDER_LINE_NO,
    col.PART_ID,
    p.PRODUCT_CODE,
    COALESCE(sl.USER_SHIPPED_QTY, sl.SHIPPED_QTY) AS SHIPPED_QTY,
    co.CUSTOMER_ID,
    c.NAME                                      AS CUSTOMER_NAME,
    t.ID                                        AS TRACE_ID,
    t.SERIAL_ID
FROM dbo.SHIPPER_LINE sl
INNER JOIN dbo.SHIPPER s
    ON s.PACKLIST_ID = sl.PACKLIST_ID
LEFT JOIN dbo.CUST_ORDER_LINE col
    ON  col.CUST_ORDER_ID = COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
    AND col.LINE_NO       = sl.CUST_ORDER_LINE_NO
LEFT JOIN dbo.PART p
    ON p.ID = col.PART_ID
LEFT JOIN dbo.CUSTOMER_ORDER co
    ON co.ID = COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
LEFT JOIN dbo.CUSTOMER c
    ON c.ID = co.CUSTOMER_ID
LEFT JOIN dbo.TRACE_INV_TRANS tit
    ON tit.TRANSACTION_ID = sl.TRANSACTION_ID
LEFT JOIN dbo.TRACE t
    ON  t.PART_ID = tit.PART_ID
    AND t.ID      = tit.TRACE_ID
WHERE UPPER(sl.PACKLIST_ID) = :packlist
ORDER BY sl.LINE_NO, t.ID;
