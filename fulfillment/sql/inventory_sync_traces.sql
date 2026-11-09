-- dispatch.inventory-traces: TRACE_INV_TRANS.QTY is already signed.
-- Include negative/non-unit balances so validation can reject them explicitly.
SELECT tit.PART_ID, tit.TRACE_ID, SUM(tit.QTY) AS NET_QTY
FROM dbo.INVENTORY_TRANS it
JOIN dbo.TRACE_INV_TRANS tit
  ON tit.TRANSACTION_ID = it.TRANSACTION_ID AND tit.PART_ID = it.PART_ID
WHERE it.SITE_ID = :site AND it.WAREHOUSE_ID = :warehouse AND it.LOCATION_ID = :location
GROUP BY tit.PART_ID, tit.TRACE_ID
HAVING SUM(tit.QTY) <> 0
ORDER BY tit.PART_ID, tit.TRACE_ID;
