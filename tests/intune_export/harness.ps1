# Runs IntuneExport.ps1 against recorded Microsoft Graph responses. It shadows
# Invoke-RestMethod, Invoke-WebRequest, and Start-Sleep, the script's only
# network and wait boundary, and writes every request to -Log as JSON.
#
# -Fixtures is a JSON map of "<METHOD> <path>" to a response or a list of
# responses served in turn. A Graph path has no host and no version. A response
# is {status, body, headers} for Graph, or {content} or {bytes} (base64) for a
# plain download.

param(
    [Parameter(Mandatory)] [string]$Script,
    [Parameter(Mandatory)] [string]$Fixtures,
    [Parameter(Mandatory)] [string]$OutputDir,
    [Parameter(Mandatory)] [string]$Log,
    [string]$ConfigJson = '{}',
    [string]$TenantId = '00000000-0000-0000-0000-000000000000',
    [string]$TokenTenant = '00000000-0000-0000-0000-000000000000'
)

$ErrorActionPreference = 'Stop'
$HarnessGraph = Get-Content $Fixtures -Raw | ConvertFrom-Json -AsHashtable
$HarnessServed = @{}
$HarnessRequests = [Collections.Generic.List[object]]::new()
$HarnessUnknown = [Collections.Generic.List[string]]::new()
$HarnessSleeps = [Collections.Generic.List[int]]::new()

function Get-FixtureKey {
    param([string]$Method, [string]$Uri)

    "$($Method.ToUpper()) $($Uri -replace '^https://graph\.microsoft\.com/(beta|v1\.0)/' -replace '^/')"
}

function Get-Fixture {
    param([string]$Key)

    if (-not $script:HarnessGraph.ContainsKey($Key)) {
        $script:HarnessUnknown.Add($Key)
        throw "No fixture for $Key"
    }
    $fixture = $script:HarnessGraph[$Key]
    if ($fixture -is [array]) {
        $index = [Math]::Min([int]$script:HarnessServed[$Key], $fixture.Count - 1)
        $script:HarnessServed[$Key] = $index + 1
        $fixture = $fixture[$index]
    }
    $fixture
}

function ConvertTo-Response {
    param($Body)

    if ($null -eq $Body) { return }
    $Body | ConvertTo-Json -Depth 100 -Compress | ConvertFrom-Json
}

function Invoke-RestMethod {
    param([string]$Uri, [string]$Method = 'GET', $Headers, $Body, [int]$TimeoutSec)

    $script:HarnessRequests.Add([ordered]@{ method = $Method.ToUpper(); url = $Uri; accept = $Headers.Accept; batch = $false; timeout = $TimeoutSec })
    if ($Uri -like 'https://login.microsoftonline.com/*') {
        $claims = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((@{ tid = $TokenTenant } | ConvertTo-Json -Compress))).TrimEnd('=')
        return [pscustomobject]@{ access_token = "header.$($claims.Replace('+', '-').Replace('/', '_')).signature"; expires_in = 3599 }
    }
    if ($Uri -match '/\$batch$') {
        $batch = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
        $responses = foreach ($request in $batch.requests) {
            $script:HarnessRequests.Add([ordered]@{ method = $request.method; url = $request.url; accept = $request.headers.Accept; batch = $true })
            $fixture = Get-Fixture (Get-FixtureKey $request.method $request.url)
            @{ id = $request.id; status = $fixture.status ?? 200; headers = $fixture.headers ?? @{}; body = $fixture.body }
        }
        return ConvertTo-Response @{ responses = @($responses) }
    }
    $fixture = Get-Fixture (Get-FixtureKey $Method $Uri)
    $status = $fixture.status ?? 200
    if ($status -eq 0) {
        throw [Net.Http.HttpRequestException]::new('The request was canceled due to the configured HttpClient.Timeout.')
    }
    if ($status -ge 300) {
        $response = [Net.Http.HttpResponseMessage]::new([Net.HttpStatusCode]$status)
        if ($fixture.headers.'Retry-After') {
            $response.Headers.RetryAfter = [Net.Http.Headers.RetryConditionHeaderValue]::new([TimeSpan]::FromSeconds($fixture.headers.'Retry-After'))
        }
        $exception = [Microsoft.PowerShell.Commands.HttpResponseException]::new(
            "Response status code does not indicate success: $status.", $response)
        $record = [Management.Automation.ErrorRecord]::new($exception, 'WebCmdletWebResponseException', 'InvalidOperation', $null)
        $record.ErrorDetails = [Management.Automation.ErrorDetails]::new(($fixture.body | ConvertTo-Json -Depth 100 -Compress))
        throw $record
    }
    ConvertTo-Response $fixture.body
}

function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [int]$TimeoutSec)

    $script:HarnessRequests.Add([ordered]@{ method = 'GET'; url = $Uri; accept = $null; batch = $false; timeout = $TimeoutSec })
    $fixture = Get-Fixture (Get-FixtureKey 'GET' $Uri)
    if ($OutFile) {
        [IO.File]::WriteAllBytes($OutFile, [Convert]::FromBase64String($fixture.bytes))
        return
    }
    [pscustomobject]@{ StatusCode = 200; Content = $fixture.content }
}

function Start-Sleep {
    param([int]$Seconds)

    $script:HarnessSleeps.Add($Seconds)
}

$env:PDT_OUTPUT_DIR = $OutputDir
$env:PDT_CONFIG_JSON = $ConfigJson
$env:PDT_AZURE_TENANT_ID = $TenantId
$env:PDT_AZURE_CLIENT_ID = '00000000-0000-0000-0000-000000000001'
$env:PDT_AZURE_CLIENT_SECRET = 'secret'

. $Script
$failure = $null
try {
    Start-IntuneExport
} catch {
    $failure = $_.Exception.Message
} finally {
    [ordered]@{ requests = $HarnessRequests; unknown = $HarnessUnknown; sleeps = $HarnessSleeps; failure = $failure } |
        ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Log
}
if ($failure) { exit 1 }
