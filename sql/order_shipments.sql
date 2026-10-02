/*
===============================================================================
  ORDER SHIPMENTS — EVERY PACKLIST LINE FOR ONE SALES ORDER
===============================================================================
  Same tracking sourcing as sql/recon_shipments.sql (Z_UPS_SHIPMENTS per
  packlist with the UDF-0000028 fallback) plus the serial numbers that went
  out on each packlist line (SHIPPER_LINE.TRANSACTION_ID -> TRACE_INV_TRANS).
  Voided shippers (STATUS 'X'/'V') are returned and flagged by the app.

  Bind parameter: :so (CUSTOMER_ORDER.ID). SQL Server 2016: FOR XML PATH.
===============================================================================
*/

SELECT
    sl.PACKLIST_ID,
    sl.LINE_NO,
    s.CREATE_DATE                               AS PACKLIST_CREATED,
    s.SHIPPED_DATE,
    s.STATUS                                    AS SHIPPER_STATUS,
    s.SHIP_VIA,
    s.WAYBILL_NUMBER,
    COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) AS CUST_ORDER_ID,
    sl.CUST_ORDER_LINE_NO,
    col.PART_ID,
    p.PRODUCT_CODE,
    p.DESCRIPTION                               AS PART_DESCRIPTION,
    COALESCE(sl.USER_SHIPPED_QTY, sl.SHIPPED_QTY) AS SHIPPED_QTY,
    co.CUSTOMER_ID,
    c.NAME                                      AS CUSTOMER_NAME,
    s.INVOICE_ID,
    ups.TRACKING_NUMBERS,
    udfx.UDF_TRACKING_NUMBER,
    ser.SERIALS
FROM dbo.SHIPPER_LINE sl WITH (NOLOCK)
INNER JOIN dbo.SHIPPER s WITH (NOLOCK)
    ON s.PACKLIST_ID = sl.PACKLIST_ID
LEFT JOIN dbo.CUST_ORDER_LINE col WITH (NOLOCK)
    ON  col.CUST_ORDER_ID = COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
    AND col.LINE_NO       = sl.CUST_ORDER_LINE_NO
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = col.PART_ID
LEFT JOIN dbo.CUSTOMER_ORDER co WITH (NOLOCK)
    ON co.ID = COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID)
LEFT JOIN dbo.CUSTOMER c WITH (NOLOCK)
    ON c.ID = co.CUSTOMER_ID
OUTER APPLY (
    SELECT STUFF((
        SELECT ', ' + z.TRACKING_NUMBER
        FROM dbo.Z_UPS_SHIPMENTS z WITH (NOLOCK)
        WHERE z.PACKLIST_ID = s.PACKLIST_ID
          AND ISNULL(z.VOID, 'N') <> 'Y'
          AND NULLIF(LTRIM(RTRIM(z.TRACKING_NUMBER)), '') IS NOT NULL
        GROUP BY z.TRACKING_NUMBER
        ORDER BY z.TRACKING_NUMBER
        FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS TRACKING_NUMBERS
) ups
OUTER APPLY (
    SELECT TOP (1) LTRIM(RTRIM(udf.STRING_VAL)) AS UDF_TRACKING_NUMBER
    FROM dbo.USER_DEF_FIELDS udf WITH (NOLOCK)
    WHERE udf.ID = 'UDF-0000028'
      AND udf.DOCUMENT_ID = s.PACKLIST_ID
      AND NULLIF(LTRIM(RTRIM(udf.STRING_VAL)), '') IS NOT NULL
      AND LTRIM(RTRIM(udf.STRING_VAL)) <> '0'
    ORDER BY udf.ROWID
) udfx
OUTER APPLY (
    SELECT STUFF((
        SELECT ', ' + tit.TRACE_ID
        FROM dbo.TRACE_INV_TRANS tit WITH (NOLOCK)
        WHERE tit.TRANSACTION_ID = sl.TRANSACTION_ID
          AND NULLIF(LTRIM(RTRIM(COALESCE(tit.TRACE_ID, ''))), '') IS NOT NULL
        GROUP BY tit.TRACE_ID
        ORDER BY tit.TRACE_ID
        FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS SERIALS
) ser
WHERE COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) = :so
ORDER BY s.SHIPPED_DATE DESC, sl.PACKLIST_ID, sl.LINE_NO;
