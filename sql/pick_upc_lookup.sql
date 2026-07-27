/* Resolve a scanned component UPC to its VISUAL part ID. */
SELECT DISTINCT
    psv.PART_ID,
    LTRIM(RTRIM(psv.USER_6)) AS UPC
FROM dbo.PART_SITE_VIEW psv
WHERE LTRIM(RTRIM(psv.USER_6)) = :upc;
