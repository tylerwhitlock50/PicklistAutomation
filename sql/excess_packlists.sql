/*
===============================================================================
  EXCESS PACKLIST COST — SHIPPER HEADERS OVER A WINDOW
===============================================================================
  Every non-voided SHIPPER header that either shipped inside
  [:start_date, :end_date) or was created today (:today_start) and has not
  shipped yet. Python groups by (CUST_ORDER_ID, ship day); each packlist
  beyond the first per order-day is an "excess" shipment that cost an extra
  carrier charge. Unshipped rows created today feed the "still fixable —
  consolidate before pickup" list.

  Voided/cancelled shippers (STATUS 'X'/'V') are FILTERED here — unlike
  packlist_daily.sql, which returns them for the verify UI — because a voided
  duplicate never generated a shipping charge.

  Bind parameters:
    :start_date  — inclusive lower bound on SHIPPER.SHIPPED_DATE (YYYY-MM-DD)
    :end_date    — exclusive upper bound on both date branches (YYYY-MM-DD)
    :today_start — inclusive lower bound on CREATE_DATE for unshipped rows

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    s.PACKLIST_ID,
    s.CUST_ORDER_ID,
    s.CREATE_DATE,
    s.SHIPPED_DATE,
    s.STATUS           AS SHIPPER_STATUS,
    s.SHIP_VIA,
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
WHERE s.STATUS NOT IN ('X', 'V')
  AND (
        (s.SHIPPED_DATE >= :start_date AND s.SHIPPED_DATE < :end_date)
     OR (s.SHIPPED_DATE IS NULL
         AND s.CREATE_DATE >= :today_start AND s.CREATE_DATE < :end_date)
      )
ORDER BY s.CUST_ORDER_ID, s.SHIPPED_DATE, s.CREATE_DATE, s.PACKLIST_ID;
