/*
===============================================================================
  PACKLIST VERIFICATION — DAILY PACKLIST HEADERS
===============================================================================
  Every SHIPPER header whose CREATE_DATE falls in [:start_date, :end_date),
  with line and serial counts, so the verification dashboard can show which
  of today's packlists have been scan-verified. CREATE_DATE (not SHIPPED_DATE)
  because a packlist should be verifiable as soon as it is created, before
  the carrier pickup.

  SERIAL_COUNT counts TRACE rows reachable through each line's outbound
  inventory transaction — packlists with SERIAL_COUNT = 0 (accessories only)
  have nothing to scan-verify and the app labels them accordingly.

  Voided/cancelled shippers (STATUS 'X'/'V') are RETURNED, not filtered — the
  app shows them as voided instead of "not verified".

  Bind parameters:
    :start_date — inclusive lower bound on SHIPPER.CREATE_DATE (YYYY-MM-DD)
    :end_date   — exclusive upper bound on SHIPPER.CREATE_DATE (YYYY-MM-DD)

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    s.PACKLIST_ID,
    s.CREATE_DATE,
    s.SHIPPED_DATE,
    s.STATUS           AS SHIPPER_STATUS,
    s.CUST_ORDER_ID,
    co.CUSTOMER_ID,
    c.NAME             AS CUSTOMER_NAME,
    agg.LINE_COUNT,
    agg.SERIAL_COUNT
FROM dbo.SHIPPER s
LEFT JOIN dbo.CUSTOMER_ORDER co
    ON co.ID = s.CUST_ORDER_ID
LEFT JOIN dbo.CUSTOMER c
    ON c.ID = co.CUSTOMER_ID
OUTER APPLY (
    SELECT
        COUNT(DISTINCT sl.LINE_NO) AS LINE_COUNT,
        COUNT(t.ID)                AS SERIAL_COUNT
    FROM dbo.SHIPPER_LINE sl
    LEFT JOIN dbo.TRACE_INV_TRANS tit
        ON tit.TRANSACTION_ID = sl.TRANSACTION_ID
    LEFT JOIN dbo.TRACE t
        ON  t.PART_ID = tit.PART_ID
        AND t.ID      = tit.TRACE_ID
    WHERE sl.PACKLIST_ID = s.PACKLIST_ID
) agg
WHERE s.CREATE_DATE >= :start_date
  AND s.CREATE_DATE <  :end_date
ORDER BY s.CREATE_DATE, s.PACKLIST_ID;
