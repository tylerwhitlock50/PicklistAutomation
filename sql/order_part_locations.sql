/*
===============================================================================
  ORDER PART LOCATIONS — WHERE THE PARTS ON ONE ORDER PHYSICALLY ARE
===============================================================================
  Every PART_LOCATION bin with stock for any part on the order (:so), across
  the warehouses Shipping cares about. Bin classification (pickable rack,
  stage, international cage, rack 10, R11 components, MAIN) happens in
  readiness.py so the rules live in one place.
  Bind parameter: :so (CUSTOMER_ORDER.ID).
===============================================================================
*/

SELECT DISTINCT
    pl.PART_ID,
    pl.WAREHOUSE_ID,
    pl.LOCATION_ID,
    CAST(pl.QTY AS decimal(18, 4)) AS QTY
FROM dbo.CUST_ORDER_LINE col WITH (NOLOCK)
INNER JOIN dbo.PART_LOCATION pl WITH (NOLOCK)
    ON pl.PART_ID = col.PART_ID
WHERE col.CUST_ORDER_ID = :so
  AND pl.QTY > 0
  AND NULLIF(LTRIM(RTRIM(pl.LOCATION_ID)), '') IS NOT NULL
  AND pl.WAREHOUSE_ID IN ('SHIPPING', 'DISTRIBUTION', 'MAIN')
ORDER BY pl.PART_ID, pl.WAREHOUSE_ID, pl.LOCATION_ID;
