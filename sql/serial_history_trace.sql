/*
===============================================================================
  SERIAL NUMBER HISTORY — TRACE MATCH
===============================================================================
  Finds the serial-master (TRACE) rows matching a user-supplied serial number.
  Matched against BOTH TRACE.ID (the trace key, often used as the serial) and
  TRACE.SERIAL_ID (the explicit serial field, frequently blank). One row per
  (part, trace) match — the same serial value can legitimately exist under
  multiple parts, so callers must handle multiple rows.

  Bind parameters:
    :serial  — the serial number to look up (exact match, uppercased by app).

  SQL Server / Infor VISUAL (VECA). Read-only.
===============================================================================
*/

SELECT
    t.PART_ID,
    t.ID                                        AS TRACE_ID,
    COALESCE(NULLIF(t.SERIAL_ID, ''), t.ID)     AS SERIAL_NO,
    t.IN_QTY,
    t.OUT_QTY,
    (t.IN_QTY - t.OUT_QTY)                      AS NET_QTY,
    p.DESCRIPTION                               AS PART_DESCRIPTION,
    p.PRODUCT_CODE
FROM dbo.TRACE t
LEFT JOIN dbo.PART p
    ON p.ID = t.PART_ID
WHERE t.ID = :serial
   OR t.SERIAL_ID = :serial
ORDER BY t.PART_ID;
