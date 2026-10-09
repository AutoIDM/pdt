# Sample PowerShell app. Run it with `pdt run <name>`.
#
# Env vars come from the .env file when you run it on your computer, and
# from the deployed secrets when it runs in the cloud. List each one under
# env: in config.yml.
#
# PDT_OUTPUT_DIR is an empty folder pdt makes for each run. After the run,
# pdt copies every file left in it to the app's data store, under
# runs/<run>/output/.

$greeting = $env:REPORT_GREETING
if (-not $greeting) {
    $greeting = 'Hello from PowerShell'
}

$rows = @(
    [pscustomobject]@{ Name = 'disk'; Status = 'ok'; Checked = (Get-Date).ToString('s') }
    [pscustomobject]@{ Name = 'backup'; Status = 'ok'; Checked = (Get-Date).ToString('s') }
    [pscustomobject]@{ Name = 'updates'; Status = 'pending'; Checked = (Get-Date).ToString('s') }
)

$path = Join-Path $env:PDT_OUTPUT_DIR 'report.csv'
$rows | Export-Csv -Path $path -NoTypeInformation
Write-Output "$greeting. Wrote $($rows.Count) rows to report.csv."
