-- dispatch.inventory-scope: validate the exact configured physical boundary.
SELECT w.SITE_ID, l.WAREHOUSE_ID, l.ID AS LOCATION_ID
FROM dbo.WAREHOUSE w
JOIN dbo.LOCATION l ON l.WAREHOUSE_ID = w.ID
WHERE w.SITE_ID = :site AND w.ID = :warehouse AND l.ID = :location;
