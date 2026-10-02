-- Attachments on one sales order (VISUAL DOCUMENT_REFERENCE -> DOCUMENT).
-- DOCUMENT.ID is the filename; DOC_FILE_PATH is the directory (V:\... or a UNC
-- path). The app joins them and maps the Windows prefix to its container mount.
-- Param: :so  (CUSTOMER_ORDER.ID)
SELECT
    dr.ID                               AS CUST_ORDER_ID,
    dr.LINE_NO                          AS LINE_NO,
    d.ID                                AS DOCUMENT_ID,
    d.DESCRIPTION                       AS DESCRIPTION,
    d.DOC_FILE_PATH                     AS DOC_FILE_PATH,
    d.PATH_TYPE                         AS PATH_TYPE,
    d.CATEGORY_ID                       AS CATEGORY_ID,
    dr.CREATE_DATE                      AS CREATE_DATE
FROM dbo.DOCUMENT_REFERENCE dr WITH (NOLOCK)
JOIN dbo.DOCUMENT d WITH (NOLOCK)
    ON d.ID = dr.DOCUMENT_ID
WHERE dr.SOURCE_TYPE = 'T'
  AND dr.ID = :so
ORDER BY dr.LINE_NO, d.ID
