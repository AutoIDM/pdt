# Runs IntuneExport.ps1 with IntuneManagement's offline mock tenant
# (IM_MOCK_DATA) in place of the Entra sign-in. Connect-IMIntuneManagement
# resolves to the alias below, which records its arguments to -Log as JSON
# and signs in to the mock tenant, so every Graph request after it goes
# through IntuneManagement's own export code to the mock tenant's files.

param(
    [Parameter(Mandatory)] [string]$Script,
    [Parameter(Mandatory)] [string]$Log,
    [string]$Forbidden = '^$'
)

$global:HarnessSignIns = [Collections.Generic.List[object]]::new()

function global:Connect-HarnessTenant {
    param([string]$TenantId, [string]$AppId, [string]$Secret, $Certificate, [switch]$DefaultToken)

    $global:HarnessSignIns.Add([ordered]@{
        tenant = $TenantId; app = $AppId; secret = $Secret; certificate = $Certificate.Thumbprint })
    & (Get-Module IntuneManagement) {
        param($Forbidden)

        # The mock tenant answers POST directoryObjects/getByIds, the export's
        # group lookup, with its own groups, as Microsoft Graph does, and 403
        # to each GET whose path matches -Forbidden.
        $script:HarnessResolve = ${function:Resolve-MockGraphRequest}
        $script:HarnessForbidden = $Forbidden
        function script:Resolve-MockGraphRequest {
            param([string]$Method, [string]$Url, $Body)

            if ($Method -eq 'GET' -and $Url -match $script:HarnessForbidden) {
                return New-MockGraphResult 403 ([pscustomobject]@{ error = @{ code = 'Forbidden'; message = 'Missing permission' } })
            }
            if ($Url -match 'directoryObjects/getByIds') {
                $groups = (Get-MockGraphStore).Collections['groups'] | Where-Object id -in $Body.ids
                return New-MockGraphResult 200 ([pscustomobject]@{ value = @($groups) })
            }
            & $script:HarnessResolve @PSBoundParameters
        }
    } $Forbidden
    IntuneManagement\Connect-IMIntuneManagement -Provider Mock -Interactive -DefaultToken
}

Set-Alias Connect-IMIntuneManagement Connect-HarnessTenant -Scope Global
try {
    & $Script
} finally {
    @{ signIns = $global:HarnessSignIns } | ConvertTo-Json -Depth 5 | Set-Content $Log
}
