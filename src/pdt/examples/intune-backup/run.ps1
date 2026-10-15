# Backs up an Intune tenant with IntuneManagement and audits the backup.
# run.py starts this script after it downloads IntuneManagement into
# .pdt/intune-management/ in this folder. Each run leaves these files in
# PDT_OUTPUT_DIR, and run.py keeps them in the app's data store under
# runs/<run>/:
#
#   Summary.md                  what this run found, in a few lines
#   backup/                     every policy as IntuneManagement exports it,
#                               with assignments; it imports back from here
#   backup.zip                  the same folder as one file, to download
#   Inventory.csv               one row per policy, and whether it is assigned
#   Changes.csv                 every setting that differs from the last
#                               backup, and every policy added or removed
#   Permissions.csv             each policy type, and whether the app can read it
#   Documentation/Intune.html   every policy's settings, readable
#   Documentation/csv/          the same, one CSV per policy
#   IntuneManagement.log        IntuneManagement's own log of the run
#
# The run fails when the sign-in fails or a policy type fails to export, so
# `pdt health` shows it. The files of a failed run are still kept.

# run.py passes the folder of the last good backup, which the data store keeps.
param([string]$LastBackup = '')

$ErrorActionPreference = 'Stop'
# No progress bars: they would only clutter the run's log.
$ProgressPreference = 'SilentlyContinue'
$output = $env:PDT_OUTPUT_DIR
$backup = Join-Path $output 'backup'
$started = [DateTime]::UtcNow

# No desktop UI and no settings written to this computer, so the module runs
# the same in a container as on a PC.
$env:IM_UI_BACKEND = 'None'
$env:IM_SETTINGS_STORE = 'Memory'

# IntuneManagement writes IntuneManagement.log to the current folder. Its
# progress and warning lines go to that file only, so this log stays short.
Set-Location $output
Import-Module "$PSScriptRoot/.pdt/intune-management/IntuneManagement.psd1" 6>$null 3>$null
$im = Get-Module IntuneManagement
$imVersion = "$($im.Version)$(if ($im.PrivateData.PSData.Prerelease) { "-$($im.PrivateData.PSData.Prerelease)" })"

# The commands below name their module, IntuneManagement\, because it comes
# from GitHub and not from the PowerShell Gallery.

function Connect-Tenant {
    if ($env:IM_MOCK_DATA) {
        return IntuneManagement\Connect-IMIntuneManagement -Interactive -Provider Mock -DefaultToken 6>$null 3>$null
    }
    $sign = @{ TenantId = $env:PDT_AZURE_TENANT_ID; AppId = $env:PDT_AZURE_CLIENT_ID; DefaultToken = $true }
    # run.py points PDT_AZURE_PRIVATE_KEY_PATH at the .pfx file, also when the
    # certificate came as PDT_AZURE_PRIVATE_KEY_B64.
    if ($env:PDT_AZURE_PRIVATE_KEY_PATH) {
        $sign.CertificatePath = $env:PDT_AZURE_PRIVATE_KEY_PATH
        if ($env:PDT_AZURE_PRIVATE_KEY_PASSWORD) {
            $sign.CertificatePassword = ConvertTo-SecureString $env:PDT_AZURE_PRIVATE_KEY_PASSWORD -AsPlainText -Force
        }
    } else {
        $sign.Secret = $env:PDT_AZURE_CLIENT_SECRET
    }
    return IntuneManagement\Connect-IMIntuneManagement @sign 6>$null 3>$null
}

$token = Connect-Tenant
if (-not $token) {
    throw ("Sign-in to tenant $($env:PDT_AZURE_TENANT_ID) failed; the lines above give the reason from Entra. " +
        'Check PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID, and the secret or certificate.')
}
$tenant = $token.TenantName
Write-Host "Signed in to $tenant ($($token.TenantId)) with IntuneManagement $imVersion."

# 1. Which policy types this app can read. A type it cannot read is missing
#    from the backup, so the summary names the permission to add.
# Each policy type's name, by its id, which is also its folder in a backup.
$typeTitles = @{}
$permissions = @(IntuneManagement\Get-IMGraphEffectivePermissions 6>$null 3>$null | ForEach-Object {
    $typeTitles[$_.Id] = $_.Title
    [pscustomobject]@{
        PolicyType = $_.Title
        Group      = $_.Group
        CanRead    = if ($_.TokenLevel -eq 'None') { 'no' } else { 'yes' }
        Access     = $_.TokenAccess
        # A backup only reads, so name the Read permission, not ReadWrite.
        Missing    = ($_.MissingScopes -replace '\.ReadWrite\.', '.Read.' | Select-Object -Unique) -join ' '
        Reason     = $_.Reason
    }
})
$permissions | Export-Csv (Join-Path $output 'Permissions.csv') -NoTypeInformation
$unreadable = @($permissions | Where-Object CanRead -EQ 'no')
Write-Host "Checked access to $($permissions.Count) policy types. Types the app cannot read: $($unreadable.Count)."

# 2. The backup itself.
New-Item -ItemType Directory -Force $backup | Out-Null
$export = IntuneManagement\Start-IMGraphBulkExport -ExportFolder $backup -ExportAssignments $true -AddCompanyName $false 6>$null 3>$null

# Policy files in a backup folder, with their type. IntuneManagement keeps a
# few extra files next to them: settings, assignments, the migration table,
# and the groups the policies are assigned to.
function Get-BackupPolicies([string]$Root) {
    foreach ($folder in Get-ChildItem $Root -Directory | Where-Object Name -NE 'Groups') {
        # A few types share a folder whose name is no type's id.
        $type = if ($typeTitles[$folder.Name]) { $typeTitles[$folder.Name] } else { $folder.Name -creplace '(?<=[a-z])(?=[A-Z])', ' ' }
        foreach ($file in Get-ChildItem $folder.FullName -Filter *.json -File) {
            if ($file.Name -like '*_settings.json' -or $file.Name -like '*_assignments.json' -or $file.Name -like 'MigrationTable*') { continue }
            $policy = Get-Content $file.FullName -Raw | ConvertFrom-Json
            [pscustomobject]@{ Type = $type; Policy = $policy; File = "$($folder.Name)/$($file.Name)" }
        }
    }
}

# 3. Inventory: one row per policy. A policy type that takes assignments and
#    a policy with none is a policy that does nothing.
$inventory = @(Get-BackupPolicies $backup | ForEach-Object {
    $p = $_.Policy
    $name = if ($p.displayName) { $p.displayName } elseif ($p.name) { $p.name } else { [IO.Path]::GetFileNameWithoutExtension($_.File) }
    $modified = if ($p.lastModifiedDateTime) { $p.lastModifiedDateTime } else { $p.modifiedDateTime }
    if ($modified -is [datetime]) { $modified = $modified.ToUniversalTime().ToString('yyyy-MM-dd HH:mm') }
    $hasAssignments = $null -ne $p.PSObject.Properties['assignments']
    [pscustomobject]@{
        PolicyType   = $_.Type
        Name         = $name
        Id           = $p.id
        LastModified = $modified
        Assignments  = if ($hasAssignments) { @($p.assignments).Count } else { '' }
        Unassigned   = if ($hasAssignments -and @($p.assignments).Count -eq 0) { 'yes' } else { '' }
        File         = "backup/$($_.File)"
    }
} | Sort-Object PolicyType, Name)
$inventory | Export-Csv (Join-Path $output 'Inventory.csv') -NoTypeInformation
$unassigned = @($inventory | Where-Object Unassigned -EQ 'yes')
$typesBackedUp = @($inventory | Select-Object -ExpandProperty PolicyType -Unique).Count
Write-Host "Backed up $($inventory.Count) policies of $typesBackedUp policy types to backup/. Types that failed: $($export.Failed)."
Write-Host "Wrote Inventory.csv. Policies assigned to nobody: $($unassigned.Count)."
if ($inventory) {
    Compress-Archive -Path (Join-Path $backup '*') -DestinationPath (Join-Path $output 'backup.zip')
}

# 4. Changes since the last backup, with IntuneManagement's own compare.
$changes = @()
$firstBackup = -not ($LastBackup -and (Test-Path (Join-Path $LastBackup '*')))
if ($firstBackup) {
    Write-Host 'This is the first backup, so there is nothing to compare it with. The next run reports what changed.'
} else {
    # The compare walks the type folders of the new backup, so a type whose
    # every policy was deleted needs its folder back to show up as removed.
    foreach ($folder in Get-ChildItem $LastBackup -Directory) {
        New-Item -ItemType Directory -Force (Join-Path $backup $folder.Name) | Out-Null
    }
    # The compare options are a class inside the module. A script sees a module's
    # classes by name only with `using module`, which needs the module at parse time.
    $providerType = [AppDomain]::CurrentDomain.GetAssemblies().ForEach({ $_.GetType('CompareExportedFoldersProvider') }).Where({ $_ }, 'First')[0]
    $provider = $providerType::new()
    $provider.SourcePath = $backup
    $provider.ComparePath = $LastBackup
    $pairs = @(IntuneManagement\Compare-IMGraphPolicy -ExportedFolders $provider 6>$null 3>$null)
    # Intune changes these on every edit; the settings rows say what the edit was.
    $bookkeeping = 'version', 'lastModifiedDateTime', 'modifiedDateTime'
    $changes = [Collections.Generic.List[object]]::new()
    function Add-Change($Type, $Policy, $Kind, $Setting = '', $Before = '', $After = '') {
        $changes.Add([pscustomobject]@{ PolicyType = $Type; Policy = $Policy; Change = $Kind; Setting = $Setting; Before = $Before; After = $After })
    }
    foreach ($pair in $pairs) {
        foreach ($row in @($pair.Result | Where-Object { -not $_.Match -and $_.PropertyName -notin $bookkeeping })) {
            if ($row.PropertyName -ne 'Object') {
                Add-Change $pair.PolicyType $pair.Name 'changed' $row.PropertyName $row.Object2Value $row.Object1Value
            } elseif ($row.Object1Value) {
                Add-Change $pair.PolicyType $pair.Name 'added'
            } elseif (-not $inventory.Where({ $_.PolicyType -eq $pair.PolicyType -and $_.Name -eq $pair.Name })) {
                Add-Change $pair.PolicyType $pair.Name 'removed'
            }
        }
    }
    # The compare matches policies by id, and skips the tenant-wide settings,
    # which have none. Those are compared here, one property at a time.
    $previous = @(Get-BackupPolicies $LastBackup)
    foreach ($now in @(Get-BackupPolicies $backup | Where-Object { -not $_.Policy.id })) {
        $then = $previous.Where({ $_.File -eq $now.File }, 'First')
        if (-not $then) { continue }
        $names = @($now.Policy.PSObject.Properties.Name) + @($then[0].Policy.PSObject.Properties.Name) | Select-Object -Unique
        foreach ($name in $names | Where-Object { $_ -notin $bookkeeping -and $_ -notlike '@odata.*' }) {
            $after = ConvertTo-Json -InputObject $now.Policy.$name -Depth 20 -Compress
            $before = ConvertTo-Json -InputObject $then[0].Policy.$name -Depth 20 -Compress
            if ($after -cne $before) {
                Add-Change $now.Type ([IO.Path]::GetFileNameWithoutExtension($now.File)) 'changed' $name $before $after
            }
        }
    }
    $changes | Export-Csv (Join-Path $output 'Changes.csv') -NoTypeInformation
    $added = @($changes | Where-Object Change -EQ 'added').Count
    $removed = @($changes | Where-Object Change -EQ 'removed').Count
    $changed = @($changes | Where-Object Change -EQ 'changed').Count
    Write-Host "Wrote Changes.csv. Since the last backup: policies added $added, removed $removed; settings changed $changed."
}

# 5. Documentation of every policy in the backup.
$docs = Join-Path $output 'Documentation'
$documented = IntuneManagement\Start-IMGraphBulkDocumentation -OutputFormat 'html,csv' -SourceFolder $backup -Options @{
    Outputs = @{
        html = @{ HTMLDocumentName = (Join-Path $docs 'Intune.html'); HTMLTitleProperty = "Intune documentation - $tenant"; HTMLOpenFile = $false }
        csv  = @{ CSVDocumentationPath = (Join-Path $docs 'csv') }
    }
} 6>$null 3>$null
Write-Host "Documented $($documented.PolicyCount) policies in Documentation/Intune.html and Documentation/csv/."

# 6. The summary a person reads first.
$lines = @(
    "# Intune backup of $tenant"
    ''
    "$($started.ToString('yyyy-MM-dd HH:mm')) UTC, IntuneManagement $imVersion."
    ''
    "- Policies backed up: $($inventory.Count), of $typesBackedUp policy types, in ``backup/`` and ``backup.zip``."
    "- Policies assigned to nobody: $($unassigned.Count). ``Inventory.csv`` marks them."
)
if ($firstBackup) {
    $lines += '- First backup: the next run reports what changed since this one.'
} else {
    $lines += "- Since the last backup: policies added $added, removed $removed; settings changed $changed. ``Changes.csv`` lists each one."
}
$lines += "- Policy types the app cannot read: $($unreadable.Count). ``Permissions.csv`` has every type."
if ($export.Failed -gt 0) {
    $lines += "- Policy types that failed to export: $($export.Failed). ``IntuneManagement.log`` says why."
}
if ($changes) {
    $lines += '', '## What changed', '', '| Policy type | Policy | Change | Setting | Before | After |', '| --- | --- | --- | --- | --- | --- |'
    foreach ($c in $changes | Select-Object -First 50) {
        $cells = $c.PolicyType, $c.Policy, $c.Change, $c.Setting, $c.Before, $c.After | ForEach-Object { "$_" -replace '\|', '\|' -replace '\r?\n', ' ' }
        $lines += "| $($cells -join ' | ') |"
    }
    if ($changes.Count -gt 50) { $lines += '', "The first 50 of $($changes.Count) changes; ``Changes.csv`` has them all." }
}
if ($unassigned) {
    $lines += '', '## Assigned to nobody', ''
    $lines += $unassigned | ForEach-Object { "- $($_.PolicyType): $($_.Name)" }
}
if ($unreadable) {
    $lines += '', '## Policy types the app cannot read', ''
    $lines += $unreadable | ForEach-Object { "- $($_.PolicyType): add $($_.Missing) to the app registration" }
}
$lines | Set-Content (Join-Path $output 'Summary.md')
Write-Host 'Wrote Summary.md.'

if ($export.Failed -gt 0) {
    throw "Policy types that failed to export: $($export.Failed). IntuneManagement.log in this run's files says why."
}
