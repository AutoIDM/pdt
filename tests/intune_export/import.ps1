# Imports an export folder with IntuneManagement's own bulk import into its
# offline mock tenant (IM_MOCK_DATA), and writes to -Report, as JSON, the
# import's summary and every object the mock tenant holds afterwards, by
# Graph path.

param(
    [Parameter(Mandatory)] [string]$Module,
    [Parameter(Mandatory)] [string]$ImportFolder,
    [Parameter(Mandatory)] [string]$Report
)

$ErrorActionPreference = 'Stop'
$env:IM_UI_BACKEND = 'None'
$env:IM_SETTINGS_STORE = 'Memory'
Import-Module $Module 6>$null
Connect-IMIntuneManagement -Provider Mock -Interactive -DefaultToken | Out-Null
$result = Start-IMGraphBulkImport -ImportFolder $ImportFolder -ImportAssignments $true
$objects = & (Get-Module IntuneManagement) {
    $collections = (Get-MockGraphStore).Collections
    $names = [ordered]@{}
    foreach ($path in $collections.Keys | Sort-Object) {
        $names[$path] = @($collections[$path] | ForEach-Object { [ordered]@{ name = $_.displayName ?? $_.name; assignments = @($_.assignments) } })
    }
    $names
}
@{ result = $result | Select-Object Imported, Updated, Replaced, Skipped; objects = $objects } |
    ConvertTo-Json -Depth 20 | Set-Content $Report
