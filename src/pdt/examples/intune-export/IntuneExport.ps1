# Exports the Intune configuration of a tenant with IntuneManagement's own
# bulk export (https://github.com/Micke-K/IntuneManagement, version 4), and
# writes IntuneExport-<date>.zip to PDT_OUTPUT_DIR. IntuneManagement imports
# the folder in the zip. PDT_CONFIG_JSON holds the settings (see config.yml).
# PDT_STATE_DIR keeps a hash of each exported file, so each run lists what
# changed since the last.

#Requires -Version 7.4

$ErrorActionPreference = 'Stop'

# The v4 branch at 4.0.0-beta1. To move to a newer commit, change both lines:
# the hash is the SHA-256 of https://github.com/Micke-K/IntuneManagement/archive/<commit>.zip.
$commit = '7869619510e97bb7f482acd44789bb35d5b8e807'
$archiveSha256 = 'CD8550A8D5768BD993D0B88835D279AB28E26D122503FC00BF8E616D520F6BBE'

$config = $env:PDT_CONFIG_JSON | ConvertFrom-Json -AsHashtable
$dataFolder = if ($IsWindows) { $env:LOCALAPPDATA } else { $env:XDG_DATA_HOME ?? "$HOME/.local/share" }
$moduleFolder = Join-Path $dataFolder "pdt/IntuneManagement-$commit"

if (-not (Test-Path (Join-Path $moduleFolder 'IntuneManagement.psd1'))) {
    $download = Join-Path ([IO.Path]::GetTempPath()) "IntuneManagement-$([guid]::NewGuid())"
    New-Item -ItemType Directory $download | Out-Null
    Invoke-WebRequest "https://github.com/Micke-K/IntuneManagement/archive/$commit.zip" -OutFile "$download/archive.zip"
    $hash = (Get-FileHash "$download/archive.zip" -Algorithm SHA256).Hash
    if ($hash -ne $archiveSha256) {
        throw "The IntuneManagement download has SHA-256 $hash, not $archiveSha256. Run again; if it repeats, GitHub changed the archive."
    }
    Expand-Archive "$download/archive.zip" $download
    New-Item -ItemType Directory (Split-Path $moduleFolder) -Force | Out-Null
    Move-Item "$download/IntuneManagement-$commit" $moduleFolder -Force
    Remove-Item $download -Recurse -Force
}

$env:IM_UI_BACKEND = 'None'
$env:IM_SETTINGS_STORE = 'Memory'
# IntuneManagement writes IntuneManagement.log to the current folder, so it is
# kept with the run's output instead of growing in the app folder.
Set-Location $env:PDT_OUTPUT_DIR
Import-Module "$moduleFolder/IntuneManagement.psd1" 6>$null

foreach ($key in $config.settings.Keys) {
    Set-IMSetting $key $config.settings[$key]
}

[byte[]]$pfx = if ($env:PDT_AZURE_PRIVATE_KEY_B64) {
    [Convert]::FromBase64String($env:PDT_AZURE_PRIVATE_KEY_B64)
} elseif ($env:PDT_AZURE_PRIVATE_KEY_PATH) {
    [IO.File]::ReadAllBytes([IO.Path]::Combine("$env:PDT_PROJECT", $env:PDT_AZURE_PRIVATE_KEY_PATH))
}
$credential = if ($pfx) {
    @{ Certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($pfx, $env:PDT_AZURE_PRIVATE_KEY_PASSWORD) }
} else {
    @{ Secret = $env:PDT_AZURE_CLIENT_SECRET }
}
$token = Connect-IMIntuneManagement -TenantId $env:PDT_AZURE_TENANT_ID -AppId $env:PDT_AZURE_CLIENT_ID @credential -DefaultToken
if (-not $token) {
    throw "Sign-in to tenant $env:PDT_AZURE_TENANT_ID with app $env:PDT_AZURE_CLIENT_ID failed. The log above holds the reason from Entra."
}

$name = "IntuneExport-$(Get-Date -Format yyyy-MM-dd)"
$work = Join-Path ([IO.Path]::GetTempPath()) "IntuneExport-$([guid]::NewGuid())"
$exportFolder = New-Item -ItemType Directory (Join-Path $work $name)
$export = @{ ExportFolder = $exportFolder; Filter = $config.name_filter }
if ($config.policy_types) { $export.PolicyType = $config.policy_types }
if ($config.policy_groups) { $export.PolicyGroup = $config.policy_groups }
$result = Start-IMGraphBulkExport @export -WarningVariable warnings
$unlisted = @($warnings -match 'list request failed' | Where-Object { "$_".Split(':')[0] -notin $config.skip_types })

# The SHA-256 of each exported file, by its path in the zip. PDT_STATE_DIR
# keeps the last successful run's list, so this run can say what changed.
$hashes = [ordered]@{}
foreach ($file in Get-ChildItem $exportFolder -Recurse -File | Sort-Object FullName) {
    $path = [IO.Path]::GetRelativePath($exportFolder, $file.FullName).Replace('\', '/')
    $hashes[$path] = (Get-FileHash $file.FullName -Algorithm SHA256).Hash
}
$lastExport = Join-Path $env:PDT_STATE_DIR 'last-export.json'
$changes = if (Test-Path $lastExport) {
    $last = Get-Content $lastExport -Raw | ConvertFrom-Json -AsHashtable
    $added = @($hashes.Keys | Where-Object { -not $last.files.ContainsKey($_) })
    $changed = @($hashes.Keys | Where-Object { $last.files.ContainsKey($_) -and $last.files[$_] -ne $hashes[$_] })
    $removed = @($last.files.Keys | Where-Object { -not $hashes.Contains($_) } | Sort-Object)
    "Since $($last.name): $($added.Count) added, $($changed.Count) changed, $($removed.Count) removed."
    $added | ForEach-Object { "  added    $_" }
    $changed | ForEach-Object { "  changed  $_" }
    $removed | ForEach-Object { "  removed  $_" }
} else {
    'This is the first export, so there is nothing to compare it with.'
}
@{ name = $name; files = $hashes } | ConvertTo-Json | Set-Content $lastExport

Compress-Archive $exportFolder (Join-Path $env:PDT_OUTPUT_DIR "$name.zip") -Force
Remove-Item $work -Recurse -Force
Write-Output "Exported $($result.Policies) objects of $($result.Types) types to $name.zip."
$changes | Write-Output
if ($result.UnknownSelectors) {
    throw "config.yml names policy types or groups that IntuneManagement does not know: $($result.UnknownSelectors -join ', ')."
}
if ($unlisted) {
    throw "Microsoft Graph refused to list $($unlisted.Count) object types, so the zip is incomplete: $($unlisted -join ' ') A status of 403 means the Entra app lacks a permission that config.yml lists."
}
if ($result.Failed) {
    throw "$($result.Failed) of $($result.Types) object types failed to export, so the zip is incomplete. The log above names them."
}
