"""Terraform shell-provider configuration for Windows Task Scheduler."""

from __future__ import annotations

import base64


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _script(name: str, xml: str, action: str, adopt: bool = False) -> str:
    payload = _encoded(xml)
    encoded_name = _encoded(name)
    prefix = r'''$ErrorActionPreference = 'Stop'
$name = '%s'
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))
function Write-OutputJson($value) { $text = $value | ConvertTo-Json -Compress -Depth 8; [IO.File]::WriteAllText($env:TF_SCRIPT_OUTPUT, $text, (New-Object Text.UTF8Encoding($false))) }
function Write-ErrorText($value) { [IO.File]::WriteAllText($env:TF_SCRIPT_ERROR, $value, (New-Object Text.UTF8Encoding($false))) }
function Get-TaskXml { $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue; if ($null -eq $task) { return $null }; Export-ScheduledTask -TaskName $name -TaskPath '\' }
function State($drift) { [ordered]@{ name = $name; xml = $xml; __meta = [ordered]@{ output_drift_detected = $drift } } }
function Matches($want, $have) {
    if ($null -eq $have) { return $false }
    $children = @($want.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
    if ($children.Count -eq 0) {
        if ($want.LocalName -in @('Interval', 'Duration', 'ExecutionTimeLimit')) { return [Xml.XmlConvert]::ToTimeSpan($want.InnerText) -eq [Xml.XmlConvert]::ToTimeSpan($have.InnerText) }
        return $want.InnerText -eq $have.InnerText
    }
    foreach ($child in $children) {
        $wanted = @($children | Where-Object { $_.LocalName -eq $child.LocalName })
        $actual = @($have.ChildNodes | Where-Object { $_.LocalName -eq $child.LocalName })
        if ($wanted.Count -ne $actual.Count) { return $false }
        for ($i = 0; $i -lt $wanted.Count; $i++) { if (-not (Matches $wanted[$i] $actual[$i])) { return $false } }
    }
    return $true
}
''' % (name.replace("'", "''"), payload)
    owned = "Managed by pdt;"
    read = r'''$actual = Get-TaskXml
if ($null -eq $actual) { Write-OutputJson (State $true); exit 0 }
$desiredDoc = New-Object XML; $desiredDoc.LoadXml($xml)
$actualDoc = New-Object XML; $actualDoc.LoadXml($actual)
if (-not $actualDoc.GetElementsByTagName('Description')[0].InnerText.StartsWith('%s')) { Write-ErrorText "Windows scheduled task $name exists but is not managed by PDT"; exit 1 }
$changed = -not (Matches $desiredDoc.DocumentElement $actualDoc.DocumentElement)
Write-OutputJson (State $changed)''' % owned
    if action == "read" or action == "plan":
        return prefix + read
    if action in ("create", "update"):
        if adopt:
            return prefix + read + r'''
if ((Get-TaskXml) -eq $null) { Write-ErrorText "Windows scheduled task $name does not exist for adoption"; exit 1 }'''
        elevated = r'''$ErrorActionPreference = 'Stop'
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))
try {
    $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task -and -not $task.Description.StartsWith('Managed by pdt;')) { exit 1 }
    Register-ScheduledTask -Xml $xml -TaskName $name -TaskPath '\' -Force -ErrorAction Stop | Out-Null
    exit 0
} catch { exit 1 }''' % (payload, encoded_name)
        return prefix + r'''
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(@'
%s
'@))
try { $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded } catch { Write-ErrorText "Windows Task Scheduler registration failed: $($_.Exception.Message)"; exit 1 }
if ($process.ExitCode -ne 0) { Write-ErrorText "Windows Task Scheduler registration failed"; exit $process.ExitCode }
''' % elevated + read
    return prefix + r'''
$actual = Get-TaskXml
if ($null -eq $actual) { exit 0 }
$actualDoc = New-Object XML; $actualDoc.LoadXml($actual)
if (-not $actualDoc.GetElementsByTagName('Description')[0].InnerText.StartsWith('%s')) { Write-ErrorText "Windows scheduled task $name exists but is not managed by PDT"; exit 1 }
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(@'
$ErrorActionPreference = 'Stop'
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))
try {
    $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    if ($null -eq $task) { exit 0 }
    if (-not $task.Description.StartsWith('Managed by pdt;')) { exit 1 }
    Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false -ErrorAction Stop
    exit 0
} catch { exit 1 }
'@))
try { $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded } catch { Write-ErrorText "Windows Task Scheduler removal failed: $($_.Exception.Message)"; exit 1 }
if ($process.ExitCode -ne 0) { Write-ErrorText "Windows Task Scheduler removal failed"; exit $process.ExitCode }''' % (owned, encoded_name)


def configuration(name: str, xml: str, adopt: bool = False) -> dict:
    commands = {action: {"command": _script(name, xml, action, adopt),
                         "interpreter": ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]}
                for action in ("create", "read", "update", "delete", "plan")}
    return {
        "shell_script": {
            "task": {
                "inputs": {"name": name, "xml": xml, "adopt": adopt},
                "os_commands": {"default": commands, "windows": commands},
            },
        },
    }
