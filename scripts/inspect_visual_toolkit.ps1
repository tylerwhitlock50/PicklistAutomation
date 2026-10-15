param([Parameter(Mandatory=$true)][string]$ToolkitDirectory)
# Read-only capability inventory. Does not load business objects, log in, save,
# ship, post, or invoke any toolkit transaction.
$resolvedToolkit = (Resolve-Path -LiteralPath $ToolkitDirectory -ErrorAction Stop).Path
Get-ChildItem -LiteralPath $resolvedToolkit -File -Recurse |
    Where-Object { $_.Extension -in '.dll', '.exe', '.pdf', '.chm', '.xml' -and $_.Name -match 'VMFG|VISUAL|Toolkit|Sales' } |
    ForEach-Object {
        [PSCustomObject]@{
            Name = $_.Name
            Path = $_.FullName
            FileVersion = $_.VersionInfo.FileVersion
            ProductVersion = $_.VersionInfo.ProductVersion
            Modified = $_.LastWriteTimeUtc
        }
    } | ConvertTo-Json -Depth 3
