/*
===============================================================================
  SERIAL NUMBER HISTORY — SHIPMENTS
===============================================================================
  Packlist / customer context for every shipping transaction that touched the
  serial: SHIPPER_LINE.TRANSACTION_ID matches the serial's outbound inventory
  transaction, SHIPPER carries the packlist + ship date, and the customer is
  resolved through CUSTOMER_ORDER (SHIPPER has no CUSTOMER_ID of its own).

  Voided/cancelled shippers (STATUS 'X'/'V') are RETURNED, not filtered — the
  app flags them and keeps them out of the "Shipped" timeline event.

  Tracking numbers: SHIPPER.WAYBILL_NUMBER is usually blank on recent
  shipments. The real carrier tracking lives in Z_UPS_SHIPMENTS (the UPS
  WorldShip export table, one row per package — multi-package packlists have
  several), with a fallback copy in the packlist tracking UDF
  (USER_DEF_FIELDS.ID = 'UDF-0000028', DOCUMENT_ID = packlist). Same sourcing
  as the sql-toolbox packlist_tracking_lookup.sql query. Voided UPS rows and
  the legacy '0' UDF placeholder are excluded. Aggregation uses FOR XML PATH
  (SQL Server 2016 — no STRING_AGG).

  Bind parameters:
    :serial  — the serial number to look up (exact match, uppercased by app).

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    t.PART_ID,
    t.ID                   AS TRACE_ID,
    tit.TRANSACTION_ID,
    sl.PACKLIST_ID,
    sl.LINE_NO,
    sl.CUST_ORDER_LINE_NO,
    s.SHIPPED_DATE,
    s.STATUS               AS SHIPPER_STATUS,
    s.WAYBILL_NUMBER,
    s.INVOICE_ID,
    s.CUST_ORDER_ID,
    co.CUSTOMER_ID,
    co.CUSTOMER_PO_REF,
    co.ORDER_DATE,
    c.NAME                 AS CUSTOMER_NAME,
    ups.TRACKING_NUMBERS,
    udfx.UDF_TRACKING_NUMBER
FROM dbo.TRACE t
INNER JOIN dbo.TRACE_INV_TRANS tit
    ON  tit.PART_ID  = t.PART_ID
    AND tit.TRACE_ID = t.ID
INNER JOIN dbo.SHIPPER_LINE sl
    ON sl.TRANSACTION_ID = tit.TRANSACTION_ID
INNER JOIN dbo.SHIPPER s
    ON s.PACKLIST_ID = sl.PACKLIST_ID
LEFT JOIN dbo.CUSTOMER_ORDER co
    ON co.ID = s.CUST_ORDER_ID
LEFT JOIN dbo.CUSTOMER c
    ON c.ID = co.CUSTOMER_ID
OUTER APPLY (
    -- Comma-separated distinct live UPS tracking numbers for the packlist.
    SELECT STUFF((
        SELECT ', ' + z.TRACKING_NUMBER
        FROM dbo.Z_UPS_SHIPMENTS z
        WHERE z.PACKLIST_ID = s.PACKLIST_ID
          AND ISNULL(z.VOID, 'N') <> 'Y'
          AND NULLIF(LTRIM(RTRIM(z.TRACKING_NUMBER)), '') IS NOT NULL
        GROUP BY z.TRACKING_NUMBER
        ORDER BY z.TRACKING_NUMBER
        FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS TRACKING_NUMBERS
) ups
OUTER APPLY (
    SELECT TOP (1) LTRIM(RTRIM(udf.STRING_VAL)) AS UDF_TRACKING_NUMBER
    FROM dbo.USER_DEF_FIELDS udf
    WHERE udf.ID = 'UDF-0000028'
      AND udf.DOCUMENT_ID = s.PACKLIST_ID
      AND NULLIF(LTRIM(RTRIM(udf.STRING_VAL)), '') IS NOT NULL
      AND LTRIM(RTRIM(udf.STRING_VAL)) <> '0'
    ORDER BY udf.ROWID
) udfx
WHERE t.ID = :serial
   OR t.SERIAL_ID = :serial
ORDER BY s.SHIPPED_DATE, sl.PACKLIST_ID;
