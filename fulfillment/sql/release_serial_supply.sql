/*
===============================================================================
  ORDER RELEASE GATE — ELIGIBLE SERIALIZED GUN SUPPLY
===============================================================================
  One row per serial currently on hand in the same SHIPPING R01-R09 locations
  used by the gun picklist. The app keeps the ERP read-only and uses this fresh
  snapshot to validate its local sticky reservation ledger before every release
  evaluation and picklist generation.
===============================================================================
*/

WITH TraceLocation AS (
    SELECT
        tit.TRACE_ID AS SERIAL_NO,
        tit.PART_ID AS PART_ID,
        it.WAREHOUSE_ID,
        it.LOCATION_ID,
        SUM(CAST(tit.QTY AS decimal(18, 4))) AS NET_QTY,
        MAX(it.CREATE_DATE) AS LAST_TRANSACTION_AT
    FROM dbo.INVENTORY_TRANS it WITH (NOLOCK)
    INNER JOIN dbo.TRACE_INV_TRANS tit WITH (NOLOCK)
        ON it.TRANSACTION_ID = tit.TRANSACTION_ID
       AND it.PART_ID = tit.PART_ID
    WHERE it.WAREHOUSE_ID = 'SHIPPING'
      __RELEASE_SERIAL_PART_FILTER__
      AND LEFT(UPPER(COALESCE(it.LOCATION_ID, '')), 3) BETWEEN 'R01' AND 'R09'
      AND UPPER(COALESCE(it.LOCATION_ID, '')) NOT LIKE '%STAGE%'
      AND UPPER(COALESCE(it.LOCATION_ID, '')) NOT LIKE '%INTERNATIONAL%'
      AND NULLIF(LTRIM(RTRIM(COALESCE(tit.TRACE_ID, ''))), '') IS NOT NULL
    GROUP BY
        tit.TRACE_ID,
        tit.PART_ID,
        it.WAREHOUSE_ID,
        it.LOCATION_ID
    HAVING SUM(CAST(tit.QTY AS decimal(18, 4))) > 0
)
SELECT
    SERIAL_NO,
    PART_ID,
    WAREHOUSE_ID,
    LOCATION_ID,
    NET_QTY,
    LAST_TRANSACTION_AT
FROM TraceLocation
ORDER BY PART_ID, LAST_TRANSACTION_AT, SERIAL_NO
OPTION (RECOMPILE);
