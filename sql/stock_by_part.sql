/*
===============================================================================
  STOCK LOOKUP — EVERY BIN HOLDING ONE PART, PLUS THE SERIALS IN EACH BIN
===============================================================================
  Answers "do we really have one on R03S03?" without a walk to the rack.
  Bin rows come from PART_LOCATION; serials come from the net trace balance
  per (warehouse, location) over INVENTORY_TRANS / TRACE_INV_TRANS, the same
  derivation the audit and release gate use. Classification (pickable rack,
  stage, international, rack 10, MAIN) happens in readiness.classify_bin.

  Bind parameter: :part_id (PART.ID, uppercased by the app).
===============================================================================
*/

WITH TraceLocation AS (
    SELECT
        tit.TRACE_ID AS SERIAL_NO,
        it.WAREHOUSE_ID,
        it.LOCATION_ID,
        SUM(CAST(tit.QTY AS decimal(18, 4))) AS NET_QTY,
        MAX(it.CREATE_DATE) AS LAST_TRANSACTION_AT
    FROM dbo.INVENTORY_TRANS it WITH (NOLOCK)
    INNER JOIN dbo.TRACE_INV_TRANS tit WITH (NOLOCK)
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID = tit.PART_ID
    WHERE it.PART_ID = :part_id
      AND NULLIF(LTRIM(RTRIM(COALESCE(tit.TRACE_ID, ''))), '') IS NOT NULL
    GROUP BY tit.TRACE_ID, it.WAREHOUSE_ID, it.LOCATION_ID
    HAVING SUM(CAST(tit.QTY AS decimal(18, 4))) > 0
)
SELECT
    pl.PART_ID,
    p.DESCRIPTION                                   AS PART_DESCRIPTION,
    p.PRODUCT_CODE,
    pl.WAREHOUSE_ID,
    pl.LOCATION_ID,
    CAST(pl.QTY AS decimal(18, 4))                  AS QTY,
    ser.SERIALS,
    ISNULL(ser.SERIAL_COUNT, 0)                     AS SERIAL_COUNT,
    ser.OLDEST_TRANSACTION_AT
FROM dbo.PART_LOCATION pl WITH (NOLOCK)
LEFT JOIN dbo.PART p WITH (NOLOCK)
    ON p.ID = pl.PART_ID
OUTER APPLY (
    SELECT
        STUFF((
            SELECT ', ' + tl.SERIAL_NO
            FROM TraceLocation tl
            WHERE tl.WAREHOUSE_ID = pl.WAREHOUSE_ID
              AND tl.LOCATION_ID = pl.LOCATION_ID
            ORDER BY tl.LAST_TRANSACTION_AT, tl.SERIAL_NO
            FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS SERIALS,
        (SELECT COUNT(*) FROM TraceLocation tl2
          WHERE tl2.WAREHOUSE_ID = pl.WAREHOUSE_ID AND tl2.LOCATION_ID = pl.LOCATION_ID) AS SERIAL_COUNT,
        (SELECT MIN(tl3.LAST_TRANSACTION_AT) FROM TraceLocation tl3
          WHERE tl3.WAREHOUSE_ID = pl.WAREHOUSE_ID AND tl3.LOCATION_ID = pl.LOCATION_ID) AS OLDEST_TRANSACTION_AT
) ser
WHERE pl.PART_ID = :part_id
  AND pl.QTY > 0
  AND NULLIF(LTRIM(RTRIM(pl.LOCATION_ID)), '') IS NOT NULL
ORDER BY pl.WAREHOUSE_ID, pl.LOCATION_ID
OPTION (RECOMPILE);
