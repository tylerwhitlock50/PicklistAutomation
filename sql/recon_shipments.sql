/*
===============================================================================
  SHIPPING RECONCILIATION — ACTUAL SHIPMENTS
===============================================================================
  Every packlist line whose SHIPPED_DATE falls in [:start_date, :end_date),
  with the part resolved through CUST_ORDER_LINE (SHIPPER_LINE itself has no
  PART_ID — it references the order line by CUST_ORDER_ID + CUST_ORDER_LINE_NO)
  and the customer resolved through CUSTOMER_ORDER.

  The app lines these rows up against that morning's picklist snapshot to
  answer "did everything that was supposed to ship actually leave?".

  Voided/cancelled shippers (STATUS 'X'/'V') are RETURNED, not filtered — the
  app excludes them from shipped quantities but can report that a packlist
  was voided.

  Quantities: USER_SHIPPED_QTY is the selling-UM quantity (same UM as
  CUST_ORDER_LINE.ORDER_QTY, which the picklist plans in); SHIPPED_QTY is the
  stocking-UM copy. The app uses USER_SHIPPED_QTY with SHIPPED_QTY fallback.

  Tracking numbers: same sourcing as sql/serial_history_shipments.sql —
  Z_UPS_SHIPMENTS per packlist with the packlist tracking UDF (UDF-0000028)
  as fallback. FOR XML PATH aggregation (SQL Server 2016 — no STRING_AGG).

  Bind parameters:
    :start_date — inclusive lower bound on SHIPPER.SHIPPED_DATE (YYYY-MM-DD)
    :end_date   — exclusive upper bound on SHIPPER.SHIPPED_DATE (YYYY-MM-DD)

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    sl.PACKLIST_ID,
    sl.LINE_NO,
    s.SHIPPED_DATE,
    s.STATUS                                   AS SHIPPER_STATUS,
    s.SHIP_VIA,
    COALESCE(sl.CUST_ORDER_ID, s.CUST_ORDER_ID) AS CUST_ORDER_ID,
    sl.CUST_ORDER_LINE_NO,
    col.PART_ID,
    p.PRODUCT_CODE,
    COALESCE(sl.USER_SHIPPED_QTY, sl.SHIPPED_QTY) AS SHIPPED_QTY,
    co.CUSTOMER_ID,
    c.NAME                                     AS CUSTOMER_NAME,
    s.INVOICE_ID,
    ups.TRACKING_NUMBERS,
    udfx.UDF_TRACKING_NUMBER
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
WHERE s.SHIPPED_DATE >= :start_date
  AND s.SHIPPED_DATE <  :end_date
ORDER BY s.SHIPPED_DATE, sl.PACKLIST_ID, sl.LINE_NO;
