# Exports the Intune configuration of a tenant through Microsoft Graph and
# writes IntuneExport-<date>.zip to PDT_OUTPUT_DIR. The zip holds one JSON file
# per object in the folder layout that IntuneManagement imports, the script,
# image, and policy files next to them, MigrationTable.json, and the assigned
# groups. PDT_CONFIG_JSON holds the export settings (see config.yml).
#
# This script ports the export of IntuneManagement.
# Portions derived from IntuneManagement (https://github.com/Micke-K/IntuneManagement),
# Copyright (c) 2019 Mikael Karlsson, MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

#Requires -Version 7.0

# One entry per IntuneManagement view item, in its order. Per-type behavior lives
# only here and in the hook functions the entries call.
# InfoView marks the items of IntuneManagement's "Intune Info" view, which its
# bulk export leaves out. They export only when object_types names them.
$IntuneTypes = @(
    @{ Id = 'DeviceConfiguration'; Title = 'Device Configuration'; API = '/deviceManagement/deviceConfigurations'
        QueryList = "`$filter=not%20isof(%27microsoft.graph.windowsUpdateForBusinessConfiguration%27)%20and%20not%20isof(%27microsoft.graph.iosUpdateConfiguration%27)"
        NavigationProperties = $true; PostGet = { Start-PostGetDeviceConfiguration @args } }
    @{ Id = 'ConditionalAccess'; Title = 'Conditional Access'; API = '/identity/conditionalAccess/policies'
        PostExport = { Start-PostExportConditionalAccess @args } }
    @{ Id = 'TermsOfUse'; Title = 'Terms of use'; API = '/identityGovernance/termsOfUse/agreements'; QueryList = "`$expand=files"
        Expand = 'files'; PostExport = { Start-PostExportTermsOfUse @args } }
    @{ Id = 'NamedLocations'; Title = 'Named Locations'; API = '/identity/conditionalAccess/namedLocations' }
    @{ Id = 'EndpointSecurity'; Title = 'Endpoint Security'; API = '/deviceManagement/intents'
        PostExport = { Start-PostExportEndpointSecurity @args } }
    @{ Id = 'CompliancePolicies'; Title = 'Compliance Policies'; API = '/deviceManagement/deviceCompliancePolicies'
        Expand = 'scheduledActionsForRule($expand=scheduledActionConfigurations)'; PostExport = { Start-PostExportCompliancePolicies @args } }
    @{ Id = 'CompliancePoliciesV2'; Title = 'Compliance Policies - V2'; API = '/deviceManagement/compliancePolicies'
        NameProperty = 'name'; Expand = 'settings' }
    @{ Id = 'ComplianceScripts'; Title = 'Compliance Scripts'; API = '/deviceManagement/deviceComplianceScripts' }
    @{ Id = 'IntuneBranding'; Title = 'Intune Branding'; API = '/deviceManagement/intuneBrandingProfiles'; NameProperty = 'profileName'
        PostGet = { Start-PostGetIntuneBranding @args }; PostExport = { Start-PostExportIntuneBranding @args } }
    @{ Id = 'AzureBranding'; Title = 'Azure Branding'; API = '/organization/%OrganizationId%/branding/localizations'
        NameProperty = 'Id'; SkipAddIDOnExport = $true }
    @{ Id = 'EnrollmentStatusPage'; Title = 'Enrollment Status Page'; API = '/deviceManagement/deviceEnrollmentConfigurations'
        PostList = { Start-PostListESP @args }; PostExport = { Start-PostExportEnrollmentDefaultPolicy @args } }
    @{ Id = 'EnrollmentRestrictions'; Title = 'Enrollment Restrictions'; API = '/deviceManagement/deviceEnrollmentConfigurations'
        PostList = { Start-PostListEnrollmentRestrictions @args }; PostExport = { Start-PostExportEnrollmentDefaultPolicy @args } }
    @{ Id = 'CoManagementSettings'; Title = 'Co-Management Settings'; API = '/deviceManagement/deviceEnrollmentConfigurations'
        PostList = { Start-PostListCoManagementSettings @args } }
    @{ Id = 'AdministrativeTemplates'; Title = 'Administrative Templates'; API = '/deviceManagement/groupPolicyConfigurations'
        PostGet = { Start-PostGetAdministrativeTemplate @args }; PostExport = { Start-PostExportAdministrativeTemplate @args } }
    @{ Id = 'PowerShellScripts'; Title = 'Scripts (PowerShell)'; API = '/deviceManagement/deviceManagementScripts'
        PostExport = { Start-PostExportScripts @args } }
    @{ Id = 'MacScripts'; Title = 'Scripts (Shell)'; API = '/deviceManagement/deviceShellScripts'
        PostExport = { Start-PostExportScripts @args } }
    @{ Id = 'MacCustomAttributes'; Title = 'Custom Attributes'; API = '/deviceManagement/deviceCustomAttributeShellScripts'
        PostExport = { Start-PostExportScripts @args } }
    @{ Id = 'TermsAndConditions'; Title = 'Terms and Conditions'; API = '/deviceManagement/termsAndConditions'
        ExpandAssignments = $false; PostExport = { Add-EMAssignmentsToExportFile @args } }
    @{ Id = 'AppProtection'; Title = 'App Protection'; API = '/deviceAppManagement/managedAppPolicies'
        PreGet = { Start-GetAppProtection @args }; PostList = { Start-PostListAppProtection @args } }
    @{ Id = 'AppConfigurationManagedApp'; Title = 'App Configuration (App)'; API = '/deviceAppManagement/targetedManagedAppConfigurations'
        PreGet = { Start-GetAppProtection @args } }
    @{ Id = 'AppConfigurationManagedDevice'; Title = 'App Configuration (Device)'; API = '/deviceAppManagement/mobileAppConfigurations'
        QueryList = "`$filter=microsoft.graph.androidManagedStoreAppConfiguration/appSupportsOemConfig%20eq%20false%20or%20isof(%27microsoft.graph.androidManagedStoreAppConfiguration%27)%20eq%20false"
        PostExport = { Start-PostExportAppConfiguration @args } }
    @{ Id = 'Applications'; Title = 'Applications'; API = '/deviceAppManagement/mobileApps'
        QueryList = "`$filter=(microsoft.graph.managedApp/appAvailability%20eq%20null%20or%20microsoft.graph.managedApp/appAvailability%20eq%20%27lineOfBusiness%27%20or%20isAssigned%20eq%20true)&`$orderby=displayName"
        Expand = 'categories,assignments'; ODataMetadata = 'minimal'; PostList = { Start-PostListApplications @args }
        PostGet = { Start-PostGetApplications @args }; PostExport = { Start-PostExportApplications @args } }
    @{ Id = 'AutoPilot'; Title = 'Autopilot'; API = '/deviceManagement/windowsAutopilotDeploymentProfiles' }
    @{ Id = 'PolicySets'; Title = 'Policy Sets'; API = '/deviceAppManagement/policySets'; Expand = 'Items' }
    @{ Id = 'UpdatePolicies'; Title = 'Update Policies'; API = '/deviceManagement/deviceConfigurations'
        QueryList = "`$filter=isof(%27microsoft.graph.windowsUpdateForBusinessConfiguration%27)%20or%20isof(%27microsoft.graph.iosUpdateConfiguration%27)" }
    @{ Id = 'FeatureUpdates'; Title = 'Feature Updates'; API = '/deviceManagement/windowsFeatureUpdateProfiles' }
    @{ Id = 'QualityUpdates'; Title = 'Quality Updates (Profiles)'; API = '/deviceManagement/windowsQualityUpdateProfiles' }
    @{ Id = 'QualityUpdatePolicies'; Title = 'Quality Updates (Policies)'; API = '/deviceManagement/windowsQualityUpdatePolicies' }
    @{ Id = 'SettingsCatalog'; Title = 'Settings Catalog'; API = '/deviceManagement/configurationPolicies'; NameProperty = 'name'
        Expand = 'Settings'; PostGet = { Start-PostGetSettingsCatalog @args }; PostExport = { Add-EMAssignmentsToExportFile @args } }
    @{ Id = 'InventoryPolicies'; Title = 'Inventory Policies'; API = '/deviceManagement/inventoryPolicies'; NameProperty = 'name'
        Expand = 'Settings' }
    @{ Id = 'HardwareConfigurations'; Title = 'BIOS Configurations'; API = '/deviceManagement/hardwareConfigurations'
        PolicyFileAttribute = 'configurationFileContent'; PostExport = { Start-PostExportPolicyFile @args } }
    @{ Id = 'RoleDefinitions'; Title = 'Role Definitions'; API = '/deviceManagement/roleDefinitions'
        QueryList = "`$filter=isBuiltIn%20eq%20false"; ExpandAssignments = $false; PostExport = { Start-PostExportRoleDefinitions @args } }
    @{ Id = 'ScopeTags'; Title = 'Scope (Tags)'; API = '/deviceManagement/roleScopeTags'; QueryList = "`$filter=isBuiltIn%20eq%20false"
        PostGet = { Start-PostGetScopeTags @args }; PostExport = { Add-EMAssignmentsToExportFile @args } }
    @{ Id = 'Notifications'; Title = 'Notifications'; API = '/deviceManagement/notificationMessageTemplates'
        Expand = 'localizedNotificationMessages' }
    @{ Id = 'AndroidOEMConfig'; Title = 'Android OEM Config'; API = '/deviceAppManagement/mobileAppConfigurations'
        QueryList = "`$filter=microsoft.graph.androidManagedStoreAppConfiguration/appSupportsOemConfig%20eq%20true"
        PostExport = { Start-PostExportAppConfiguration @args } }
    @{ Id = 'AppleEnrollmentTypes'; Title = 'Apple Enrollment Types'; API = '/deviceManagement/appleUserInitiatedEnrollmentProfiles' }
    @{ Id = 'AssignmentFilters'; Title = 'Filters'; API = '/deviceManagement/assignmentFilters' }
    @{ Id = 'DeviceHealthScripts'; Title = 'Health Scripts'; API = '/deviceManagement/deviceHealthScripts'
        QueryList = "`$filter=isGlobalScript%20eq%20false"; PostExport = { Start-PostExportDeviceHealthScripts @args } }
    @{ Id = 'ADMXFiles'; Title = 'ADMX Files'; API = '/deviceManagement/groupPolicyUploadedDefinitionFiles'; NameProperty = 'fileName' }
    @{ Id = 'ReusableSettings'; Title = 'Reusable Settings'; API = '/deviceManagement/reusablePolicySettings'
        PostGet = { Start-PostGetReusableSettings @args } }
    @{ Id = 'AuthenticationStrengths'; Title = 'Authentication Strengths'; API = '/identity/conditionalAccess/authenticationStrengths/policies' }
    @{ Id = 'AuthenticationContext'; Title = 'Authentication Context'; API = '/identity/conditionalAccess/authenticationContextClassReferences' }
    @{ Id = 'W365ProvisioningPolicies'; Title = 'W365 Provisioning Policies'; API = '/deviceManagement/virtualEndpoint/provisioningPolicies' }
    @{ Id = 'W365UserSettings'; Title = 'W365 User Settings'; API = '/deviceManagement/virtualEndpoint/userSettings' }
    @{ Id = 'DriverUpdateProfiles'; Title = 'Driver Update Profiles'; API = '/deviceManagement/windowsDriverUpdateProfiles' }
    @{ Id = 'DeviceCategories'; Title = 'Device Categories'; API = '/deviceManagement/deviceCategories'; QueryList = "`$top=500" }
    @{ Id = 'BaselineTemplates'; InfoView = $true; Title = 'Baseline Templates - Intent'; API = '/deviceManagement/templates' }
    @{ Id = 'BaselineTemplatesSettingsCatalog'; InfoView = $true; Title = 'Baseline Templates - Settings Catalog'
        API = '/deviceManagement/configurationPolicyTemplates'; QueryList = "`$filter=(templateFamily eq 'Baseline')" }
    @{ Id = 'AndroidGooglePlay'; InfoView = $true; Title = 'Android Google Play'; API = '/deviceManagement/androidManagedStoreAccountEnterpriseSettings' }
    @{ Id = 'AndroidEnrolmentProfiles'; InfoView = $true; Title = 'Android Enrolment Profiles'; API = 'deviceManagement/androidDeviceOwnerEnrollmentProfiles' }
    @{ Id = 'AppleVPPTokens'; InfoView = $true; Title = 'Apple VPP Tokens'; API = '/deviceAppManagement/vppTokens' }
    @{ Id = 'AppleEnrollmentTokens'; InfoView = $true; Title = 'Apple Enrollment Tokens'; API = '/deviceManagement/depOnboardingSettings/?$top=100' }
    @{ Id = 'TenantSettings'; InfoView = $true; Title = 'Tenant Settings'; API = 'deviceManagement/settings'; NameProperty = 'Name'
        GetObjectName = { Start-GetObjectNameTenantSettings @args } }
)

# A Graph call that has not answered after this many seconds is retried.
$RequestTimeoutSec = 120

# Setting -> its default. A default's type decides how the value is read.
$ConfigDefaults = [ordered]@{
    object_types           = @()
    name_filter            = ''
    add_object_type        = $true
    add_company_name       = $true
    add_id_to_file_name    = $false
    export_assignments     = $true
    export_script_content  = $true
    export_policy_files    = $true
    export_app_files       = $false
    decrypt_oma_settings   = $true
    resolve_reference_info = $true
    use_batch_api          = $true
    graph_version          = 'beta'
    sort_json_properties   = $false
}

function Get-ExportConfig {
    param([string]$Json)

    $values = if ($Json) { $Json | ConvertFrom-Json -AsHashtable } else { @{} }
    $config = [ordered]@{}
    foreach ($key in $ConfigDefaults.Keys) {
        $default = $ConfigDefaults[$key]
        $value = $values[$key]
        if ($null -eq $value) {
            $config[$key] = $default
        } elseif ($key -eq 'object_types') {
            $ids = if ($value -is [string]) { $value -split ',' } else { @($value) }
            $config[$key] = @($ids | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
            foreach ($id in $config[$key]) {
                if ($id -notin $IntuneTypes.Id) {
                    throw "config.yml: object_types lists `"$id`", which is not an Intune object type. Use one of: $($IntuneTypes.Id -join ', ')."
                }
            }
        } elseif ($default -is [bool]) {
            if ($value -is [bool]) {
                $config[$key] = $value
            } elseif ($value -is [string] -and $value.Trim() -in 'true', 'false') {
                $config[$key] = $value.Trim() -eq 'true'
            } else {
                throw "config.yml: $key is `"$value`", which is not true or false."
            }
        } else {
            $config[$key] = "$value"
        }
    }
    if ($config.graph_version -notin 'beta', 'v1.0') {
        throw "config.yml: graph_version is `"$($config.graph_version)`". Use beta or v1.0."
    }
    $config
}

function Get-GraphToken {
    if (-not $script:Token -or $script:Token.ExpiresOn -lt (Get-Date).AddMinutes(5)) {
        $response = Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$($env:PDT_AZURE_TENANT_ID)/oauth2/v2.0/token" -TimeoutSec $RequestTimeoutSec -Body @{
            client_id     = $env:PDT_AZURE_CLIENT_ID
            client_secret = $env:PDT_AZURE_CLIENT_SECRET
            scope         = 'https://graph.microsoft.com/.default'
            grant_type    = 'client_credentials'
        }
        $script:Token = @{ AccessToken = $response.access_token; ExpiresOn = (Get-Date).AddSeconds($response.expires_in) }
        # PDT_AZURE_TENANT_ID may be a domain name. The token names the tenant
        # id, which IntuneManagement also takes from the token.
        $claims = $response.access_token.Split('.')[1].Replace('-', '+').Replace('_', '/')
        $claims += '=' * ((4 - $claims.Length % 4) % 4)
        $script:TenantId = ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($claims)) | ConvertFrom-Json).tid
    }
    $script:Token.AccessToken
}

function Invoke-GraphRequest {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [ValidateSet('GET', 'POST')] [string]$Method = 'GET',
        [string]$Body,
        [ValidateSet('full', 'minimal', 'none', 'skip')] [string]$ODataMetadata = 'full',
        [switch]$AllPages,
        [switch]$NoError,
        [string]$GraphVersion = $script:Config.graph_version
    )

    if ($Url -notmatch '^https?://') {
        $Url = "https://graph.microsoft.com/$GraphVersion/" + $Url.TrimStart('/')
        $Url = $Url -replace '%OrganizationId%', $script:TenantId
    }
    $values = $null
    do {
        $headers = @{ Authorization = "Bearer $(Get-GraphToken)"; 'Content-Type' = 'application/json; charset=utf-8' }
        if ($Method -eq 'GET' -and $ODataMetadata -ne 'skip') {
            $headers.Accept = "application/json;odata.metadata=$ODataMetadata"
        }
        $params = @{ Uri = $Url; Method = $Method; Headers = $headers; TimeoutSec = $RequestTimeoutSec }
        if ($Body) { $params.Body = [Text.Encoding]::UTF8.GetBytes($Body) }
        $attempt = 0
        $response = $null
        while ($true) {
            try {
                $response = Invoke-RestMethod @params
                break
            } catch {
                # Status 0: no answer (timeout, DNS, or a dropped connection).
                $status = [int]$_.Exception.Response.StatusCode
                if ($status -in 0, 429, 503, 504 -and $attempt -lt 10) {
                    $attempt++
                    $wait = $_.Exception.Response.Headers.RetryAfter.Delta.TotalSeconds
                    Start-Sleep -Seconds ($wait ? [int]$wait : 5)
                    continue
                }
                if ($NoError) { return }
                $details = $_.ErrorDetails.Message
                $message = try { ($details | ConvertFrom-Json).error.message } catch { $details }
                $failure = if ($status -eq 0) {
                    [Exception]::new("$Method $Url got no answer from Microsoft Graph. $($_.Exception.Message)".Trim())
                } else {
                    [Exception]::new("$Method $Url returned $status. $message".Trim())
                }
                $failure.Data['StatusCode'] = $status
                throw $failure
            }
        }
        $Url = $null
        if ($AllPages -and $response.value -is [array]) {
            $values += $response.value
            $Url = $response.'@odata.nextLink'
        }
    } while ($Url)
    if ($null -ne $values) { $response.value = $values }
    $response
}

function Invoke-HookRequest {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [ValidateSet('full', 'minimal', 'none', 'skip')] [string]$ODataMetadata = 'full'
    )

    # IntuneManagement logs a failed extra request and exports the object
    # without that data. The run then fails, so the gap is not silent.
    try {
        Invoke-GraphRequest -Url $Url -ODataMetadata $ODataMetadata
    } catch {
        Add-ExportFailure $_.Exception
        Write-Warning "$($script:CurrentObject): $($_.Exception.Message)"
    }
}

function Add-ExportFailure {
    param([Exception]$Exception, $Status = $Exception.Data['StatusCode'] ?? 'error')

    $status = $Status
    $failure = "$($script:CurrentObject) ($status)"
    if ($failure -notin $script:Failures) { $script:Failures.Add($failure) }
}

function Get-GraphMetadata {
    if (-not $script:MetadataLoaded) {
        $script:MetadataLoaded = $true
        try {
            [xml]$script:Metadata = (Invoke-WebRequest -Uri 'https://graph.microsoft.com/beta/$metadata' -UseBasicParsing -TimeoutSec $RequestTimeoutSec).Content
        } catch {
            Write-Warning "Could not download the Microsoft Graph metadata, so references are not resolved: $($_.Exception.Message)"
        }
    }
    $script:Metadata
}

function Get-GraphEntityTypeProperties {
    param([string]$EntityType)

    $xml = Get-GraphMetadata
    if (-not $xml) { return }
    $entity = $xml.SelectSingleNode("//*[name()='EntityType' and @Name='$EntityType']")
    $entities = @()
    while ($entity) {
        $entities = @($entity) + $entities
        $baseType = $entity.GetAttribute('BaseType')
        $entity = if ($baseType) { $xml.SelectSingleNode("//*[name()='EntityType' and @Name='$($baseType.Split('.')[-1])']") }
    }
    foreach ($entity in $entities) {
        $entity.SelectNodes("*[name()='Property' or name()='NavigationProperty']")
    }
}

function Get-GraphObjectClassName {
    param([string]$Type)

    $xml = Get-GraphMetadata
    if (-not $xml) { return }
    foreach ($node in $xml.SelectNodes("//*[@Type='Collection(graph.$Type)']")) {
        if ($node.ParentNode.GetAttribute('Name') -eq 'deviceAppManagement') {
            return $node.GetAttribute('Name')
        }
    }
}

function Get-GraphObjectName {
    param($Object, $ObjectType)

    if ($ObjectType.GetObjectName) { return Invoke-Command $ObjectType.GetObjectName -ArgumentList $Object, $ObjectType }
    $Object.($ObjectType.NameProperty ?? 'displayName')
}

function Remove-InvalidFileNameChars {
    param([string]$Name)

    # Windows rejects these in a file name. Strip them on every OS, so a zip
    # made on Linux unpacks on Windows with the names IntuneManagement writes.
    $Name -replace '[\x00-\x1F"<>|:*?\\/]'
}

function Get-ExportFileName {
    param($Object, $ObjectType)

    $name = "$(Get-GraphObjectName $Object $ObjectType)".Trim('.')
    if ($script:Config.add_id_to_file_name -and $Object.id -and -not $ObjectType.SkipAddIDOnExport) {
        $name = "$($name)_$($Object.id)"
    }
    Remove-InvalidFileNameChars $name
}

function Invoke-SortJsonGraphObject {
    param($InputObject)

    if ($null -eq $InputObject) { return $null }

    if ($InputObject -is [Collections.IList]) {
        $new = @()
        foreach ($item in $InputObject) { $new += Invoke-SortJsonGraphObject $item }
        return , $new
    }

    if ($InputObject -isnot [Collections.IDictionary] -and $InputObject -isnot [Management.Automation.PSCustomObject]) {
        return $InputObject
    }

    $props = if ($InputObject -is [Collections.IDictionary]) { $InputObject.Keys } else { $InputObject.PSObject.Properties.Name }

    # Annotations such as "assignments@odata.context" go right before the
    # property they annotate.
    $baseGroups = @{}
    foreach ($p in $props) {
        if ($p -match '^(.+?)@(.+)$') {
            if (-not $baseGroups.ContainsKey($Matches[1])) { $baseGroups[$Matches[1]] = [Collections.ArrayList]::new() }
            [void]$baseGroups[$Matches[1]].Add($p)
        }
    }

    $atProps = @($props | Where-Object { $_.StartsWith('@') } | Sort-Object)
    $hashProps = @($props | Where-Object { $_.StartsWith('#') } | Sort-Object)
    $normal = @($props | Where-Object { -not $_.StartsWith('@') -and -not $_.StartsWith('#') -and -not $baseGroups.ContainsKey($_) } | Sort-Object)

    $orderedNames = @($atProps)
    foreach ($base in ($baseGroups.Keys | Sort-Object)) {
        $orderedNames += @($baseGroups[$base] | Sort-Object)
        if ($null -ne $InputObject.$base) { $orderedNames += $base }
    }
    $orderedNames += $normal
    $orderedNames += $hashProps

    $result = [ordered]@{}
    foreach ($p in $orderedNames) {
        $value = if ($InputObject -is [Collections.IDictionary]) { $InputObject[$p] } else { $InputObject.$p }
        $result[$p] = $null
        if ($null -ne $value) { $result[$p] = Invoke-SortJsonGraphObject $value }
        if ($InputObject.$p -is [array] -and $result[$p] -isnot [array]) {
            if ($null -eq $result[$p]) { $result[$p] = @() } else { $result[$p] = @($result[$p]) }
        }
    }
    [pscustomobject]$result
}

function ConvertTo-JsonSortedGraph {
    param($InputObject, [int]$Depth = 50)

    Invoke-SortJsonGraphObject $InputObject | ConvertTo-Json -Depth $Depth
}

function Save-GraphObjectToFile {
    param($Object, [string]$Path)

    $json = if ($script:Config.sort_json_properties) {
        ConvertTo-JsonSortedGraph $Object
    } else {
        $Object | ConvertTo-Json -Depth 50
    }
    $json = $json -replace [regex]::Escape($script:Organization.id), '%OrganizationId%'
    # The BOM lets Windows PowerShell 5.1 read non-ASCII names correctly.
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($true))
}

function Get-GraphObjectFromFile {
    param([string]$Path)

    (Get-Content -LiteralPath $Path -Raw) -replace '%OrganizationId%', $script:Organization.id | ConvertFrom-Json
}

function Add-GraphObjectProperties {
    param($Objects, $ObjectType)

    $wrapped = foreach ($object in $Objects) {
        [pscustomobject]@{
            Object      = $object
            ObjectType  = $ObjectType
            Id          = $object.id
            displayName = $object.displayName
            description = $object.description
        }
    }
    @($wrapped | Sort-Object displayName -Stable)
}

function Get-GraphObjects {
    param($ObjectType)

    $url = $ObjectType.API
    if ($ObjectType.QueryList) { $url += ($url.Contains('?') ? '&' : '?') + $ObjectType.QueryList }
    $response = Invoke-GraphRequest -Url $url -ODataMetadata ($ObjectType.ODataMetadata ?? 'full') -AllPages
    $objects = if ($response.PSObject.Properties['value']) { $response.value } else { $response }
    $objects = Add-GraphObjectProperties @($objects | Where-Object { $null -ne $_ }) $ObjectType
    if ($objects -and $ObjectType.PostList) { $objects = @(Invoke-Command $ObjectType.PostList -ArgumentList $objects, $ObjectType) }
    $objects
}

function Get-GraphObjectUrl {
    param($Object, $ObjectType)

    $preConfig = if ($ObjectType.PreGet) { Invoke-Command $ObjectType.PreGet -ArgumentList $Object, $ObjectType }
    $api = if ($preConfig -is [hashtable] -and $preConfig.API) {
        $preConfig.API
    } else {
        # depOnboardingSettings has a query in its list API. TenantSettings has
        # no id: its list call returns the one object.
        $base = ($ObjectType.API -replace '\?.*$').TrimEnd('/')
        if ($Object.id) { "$base/$($Object.id)" } else { $base }
    }

    $expand = @()
    if ($Object.'assignments@odata.navigationLink' -and $ObjectType.ExpandAssignments -ne $false) { $expand += 'assignments' }
    if ($Object.'apps@odata.navigationLink') { $expand += 'apps' }
    if ($Object.'settings@odata.navigationLink') { $expand += 'settings' }
    if ($Object.'roleAssignments@odata.navigationLink') { $expand += 'roleAssignments' }
    if ($Object.'privacyAccessControls@odata.associationLink') {
        $expand += 'microsoft.graph.windows10GeneralConfiguration/privacyAccessControls'
    }
    foreach ($item in "$($ObjectType.Expand)".Split(',', [StringSplitOptions]::RemoveEmptyEntries)) {
        if ($item -notin $expand) { $expand += $item }
    }

    if ($expand) {
        $api += if ($api.IndexOf('?') -eq -1) { '?$expand=' } elseif ($api.IndexOf('$expand') -gt 1) { ',' } else { '&$expand=' }
        $api += $expand -join ','
    }
    $api -replace '%OrganizationId%', $script:TenantId
}

function Get-GraphObject {
    param($Object, $ObjectType)

    $response = Invoke-GraphRequest -Url (Get-GraphObjectUrl $Object $ObjectType) -ODataMetadata ($ObjectType.ODataMetadata ?? 'full')
    Add-GraphObjectProperties @($response) $ObjectType
}

# Sends GET requests 20 per $batch call and retries the throttled ones.
# Returns request id -> its last response (status, body).
function Invoke-GraphBatchRequest {
    param($Requests)

    $results = @{}
    for ($start = 0; $start -lt $Requests.Count; $start += 20) {
        $pending = @($Requests[$start..([Math]::Min($start + 19, $Requests.Count - 1))])
        $attempt = 0
        while ($pending) {
            $response = Invoke-GraphRequest -Url '$batch' -Method POST -Body (@{ requests = $pending } | ConvertTo-Json -Depth 50)
            $retry = @()
            $wait = 0
            foreach ($result in $response.responses) {
                $results["$($result.id)"] = $result
                if ($result.status -eq 429) {
                    $retry += $pending | Where-Object id -EQ $result.id
                    $wait = [Math]::Max($wait, [int]$result.headers.'Retry-After')
                }
            }
            $pending = $retry
            if ($pending) {
                $attempt++
                if ($attempt -gt 10) { break }
                Start-Sleep -Seconds ([Math]::Max($wait, 5))
            }
        }
    }
    $results
}

function Get-GraphBatchObjects {
    param($Objects, $ObjectType)

    $metadata = $ObjectType.ODataMetadata ?? 'full'
    $requests = @(for ($i = 0; $i -lt $Objects.Count; $i++) {
        [pscustomobject]@{
            id      = "$i"
            method  = 'GET'
            url     = Get-GraphObjectUrl $Objects[$i].Object $ObjectType
            headers = @{ Accept = "application/json;odata.metadata=$metadata" }
        }
    })
    if (-not $requests) { return }
    $responses = Invoke-GraphBatchRequest $requests
    $bodies = for ($i = 0; $i -lt $Objects.Count; $i++) {
        $result = $responses["$i"]
        if ($result.status -lt 300 -and $result.body) {
            $result.body
        } else {
            $script:CurrentObject = "$($ObjectType.Title): $(Get-GraphObjectName $Objects[$i].Object $ObjectType)"
            Add-ExportFailure -Status ($result.status ?? 'error')
            Write-Warning "$($script:CurrentObject): Microsoft Graph returned $($result.status) in a batch. $($result.body.error.message)"
        }
    }
    Add-GraphObjectProperties @($bodies) $ObjectType
}

function Add-GraphNavigationProperties {
    param($Object, $ObjectType)

    if (-not $ObjectType.NavigationProperties -or -not $script:Config.resolve_reference_info -or -not $Object.'@odata.type') { return }

    foreach ($property in (Get-GraphEntityTypeProperties $Object.'@odata.type'.Split('.')[-1])) {
        if ($property.LocalName -ne 'NavigationProperty' -or $property.GetAttribute('ContainsTarget') -eq 'true') { continue }
        $name = $property.GetAttribute('Name')
        $link = $Object."$name@odata.navigationLink"
        if (-not $link) { continue }
        $reference = Invoke-GraphRequest -Url $link -ODataMetadata minimal -NoError
        if (-not $reference) { continue }
        $value = $null
        $refType = ''
        if ($reference.value -is [array]) {
            if ($reference.value.Count -gt 0 -and $reference.value[0].'@odata.type') { $refType = $reference.value[0].'@odata.type' }
            $names = @($reference.value | ForEach-Object { Get-GraphObjectName $_ $ObjectType })
            if ($names) {
                if (($names -join '') -like '*,*') {
                    Write-Warning "A referenced object has a comma in its name, so the reference $name cannot be imported."
                }
                $value = $names -join ','
            }
        } else {
            if ($reference.'@odata.type') { $refType = $reference.'@odata.type' }
            $value = Get-GraphObjectName $reference $ObjectType
        }
        if ($refType -and $value) { $value = "$value|:|$refType" }
        $Object | Add-Member -NotePropertyName "#CustomRef_$name" -NotePropertyValue $value -Force
    }
}

function Add-GraphMigrationObject {
    param($Id, [string]$Api, [string]$Type)

    if (-not $Id -or $script:MigrationCache.ContainsKey($Id)) { return }
    $object = Invoke-GraphRequest -Url "$Api/$Id" -ODataMetadata none -NoError
    $script:MigrationCache[$Id] = $object
    if (-not $object) {
        Write-Warning "No $Type found with ID $Id. It might be deleted."
        return
    }
    $script:MigrationObjects.Add([ordered]@{ Id = $object.id; DisplayName = $object.displayName; Type = $Type })
    if ($Type -eq 'Group') {
        $groups = Join-Path $script:MigrationFolder 'Groups'
        [IO.Directory]::CreateDirectory($groups) | Out-Null
        Save-GraphObjectToFile $object (Join-Path $groups "$(Remove-InvalidFileNameChars $object.displayName).json")
    }
}

function Add-GraphMigrationInfo {
    param($Object)

    foreach ($assignment in $Object.assignments) {
        foreach ($target in $assignment.target) {
            $type = $target.'@odata.type'
            if (-not $type) { continue }
            if ($type -in '#microsoft.graph.groupAssignmentTarget', '#microsoft.graph.exclusionGroupAssignmentTarget',
                '#microsoft.graph.cloudPcManagementGroupAssignmentTarget') {
                Add-GraphMigrationObject $target.groupId '/groups' 'Group'
            } elseif ($type -notin '#microsoft.graph.allLicensedUsersAssignmentTarget', '#microsoft.graph.allDevicesAssignmentTarget') {
                Write-Warning "Unsupported migration object: $type"
            }
        }
    }
}

function Export-GraphObject {
    param($Object, $ObjectType, [string]$Folder)

    Add-GraphNavigationProperties $Object $ObjectType
    [IO.Directory]::CreateDirectory($Folder) | Out-Null
    if (-not $script:Config.export_assignments -and $Object.assignments) {
        $Object.PSObject.Properties.Remove('assignments')
    }
    $path = Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType).json"
    if (-not $script:ExportedFiles.Add($path)) {
        Write-Warning ("$($script:CurrentObject) has the same file name as another object, ignoring case, so it replaces that file. " +
            'Set add_id_to_file_name: true in config.yml to keep both.')
    }
    Save-GraphObjectToFile $Object $path
    if ($ObjectType.PostExport) { Invoke-Command $ObjectType.PostExport -ArgumentList $Object, $ObjectType, $Folder }
    Add-GraphMigrationInfo $Object
}

function Save-Base64File {
    param([string]$Path, [string]$Base64)

    [IO.File]::WriteAllBytes($Path, [Convert]::FromBase64String($Base64))
}

function Add-EMAssignmentsToExportFile {
    param($Object, $ObjectType, [string]$Folder)

    if (-not $script:Config.export_assignments) { return }
    $path = Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType).json"
    $saved = Get-GraphObjectFromFile $path
    $assignments = (Invoke-HookRequest -Url "$($ObjectType.API)/$($Object.id)/assignments" -ODataMetadata minimal).value
    if ($assignments) {
        $saved | Add-Member -NotePropertyName assignments -NotePropertyValue $assignments -Force
        Save-GraphObjectToFile $saved $path
    }
}

function Save-EMDefaultPolicy {
    param($Object, $ObjectType, [string]$Folder)

    $fileName = $Object.id.Split('_')[1]
    if ($fileName) {
        Remove-Item -LiteralPath (Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType).json") -ErrorAction Ignore
        Save-GraphObjectToFile $Object (Join-Path $Folder "$(Remove-InvalidFileNameChars $fileName).json")
    }
}

function Start-PostGetDeviceConfiguration {
    param($Item, $ObjectType)

    if (-not $script:Config.decrypt_oma_settings) { return }
    foreach ($omaSetting in ($Item.Object.omaSettings | Where-Object isEncrypted -EQ $true)) {
        $plain = Invoke-HookRequest -Url "/deviceManagement/deviceConfigurations/$($Item.Object.id)/getOmaSettingPlainTextValue(secretReferenceValueId='$($omaSetting.secretReferenceValueId)')"
        if ($plain.value) {
            $omaSetting.isEncrypted = $false
            $omaSetting.secretReferenceValueId = $null
            $omaSetting.value = if ($omaSetting.'@odata.type' -eq '#microsoft.graph.omaSettingStringXml' -or $omaSetting.'value@odata.type' -eq '#Binary') {
                [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($plain.value))
            } else {
                $plain.value
            }
        }
    }
}

function Start-PostExportConditionalAccess {
    param($Object, $ObjectType, [string]$Folder)

    $users = $Object.conditions.users
    foreach ($id in @($users.includeGroups) + @($users.excludeGroups)) {
        if ($id -notin 'GuestsOrExternalUsers', 'All', 'None') { Add-GraphMigrationObject $id '/groups' 'Group' }
    }
    foreach ($id in @($users.includeUsers) + @($users.excludeUsers)) {
        if ($id -notin 'GuestsOrExternalUsers', 'All', 'None') { Add-GraphMigrationObject $id '/users' 'User' }
    }
}

function Start-PostExportTermsOfUse {
    param($Object, $ObjectType, [string]$Folder)

    foreach ($file in $Object.files) {
        $data = (Invoke-HookRequest -Url "agreements/$($Object.id)/file/localizations('$($file.id)')/fileData/data" -ODataMetadata minimal).value
        if ($data) { Save-Base64File (Join-Path $Folder (Remove-InvalidFileNameChars $file.fileName)) $data }
    }
}

function Start-PostExportEndpointSecurity {
    param($Object, $ObjectType, [string]$Folder)

    $settings = Invoke-HookRequest -Url "$($ObjectType.API)/$($Object.id)/settings"
    # IntuneManagement saves this text through its JSON writer, so the file
    # holds one JSON string. Its import reads the file that way.
    $settingsJson = "{ `"settings`": $(ConvertTo-Json $settings.value -Depth 20)`n}"
    Save-GraphObjectToFile $settingsJson (Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType)_Settings.json")
}

function Start-PostExportCompliancePolicies {
    param($Object, $ObjectType, [string]$Folder)

    foreach ($rule in $Object.scheduledActionsForRule) {
        foreach ($action in $rule.scheduledActionConfigurations) {
            foreach ($groupId in $action.notificationMessageCCList) { Add-GraphMigrationObject $groupId '/groups' 'Group' }
        }
    }
}

function Start-PostGetIntuneBranding {
    param($Item, $ObjectType)

    foreach ($image in 'themeColorLogo', 'lightBackgroundLogo', 'landingPageCustomizedImage') {
        $response = Invoke-HookRequest -Url "$($ObjectType.API)/$($Item.Object.id)/$image"
        if ($response.value) { $Item.Object | Add-Member -NotePropertyName $image -NotePropertyValue $response -Force }
    }
}

function Start-PostExportIntuneBranding {
    param($Object, $ObjectType, [string]$Folder)

    foreach ($image in 'themeColorLogo', 'lightBackgroundLogo', 'landingPageCustomizedImage') {
        if ($Object.$image.value) {
            $name = Remove-InvalidFileNameChars "$(Get-GraphObjectName $Object $ObjectType)_$image.jpg"
            Save-Base64File (Join-Path $Folder $name) $Object.$image.value
        }
    }
}

function Start-PostListESP {
    param($Items, $ObjectType)

    $Items | Where-Object { $_.Object.'@odata.type' -eq '#microsoft.graph.windows10EnrollmentCompletionPageConfiguration' }
}

function Start-PostListEnrollmentRestrictions {
    param($Items, $ObjectType)

    $Items | Where-Object {
        $_.Object.'@odata.type' -in '#microsoft.graph.deviceEnrollmentPlatformRestrictionConfiguration',
            '#microsoft.graph.deviceEnrollmentLimitConfiguration', '#microsoft.graph.deviceEnrollmentPlatformRestrictionsConfiguration' -and
        $_.Object.id -notlike '*_PlatformRestrictions' -and $_.Object.platformType -notin 'WindowsPhone', 'AndroidAosp'
    }
}

function Start-PostListCoManagementSettings {
    param($Items, $ObjectType)

    $Items | Where-Object { $_.Object.'@odata.type' -eq '#microsoft.graph.deviceComanagementAuthorityConfiguration' }
}

function Start-PostExportEnrollmentDefaultPolicy {
    param($Object, $ObjectType, [string]$Folder)

    if ($Object.priority -eq 0) { Save-EMDefaultPolicy $Object $ObjectType $Folder }
}

function Get-GPOObjectSettings {
    param($GPOObject)

    if ($GPOObject.policyConfigurationIngestionType -eq 'unknown') {
        $listed = @((Invoke-HookRequest -Url "/deviceManagement/groupPolicyConfigurations?`$filter=id eq '$($GPOObject.id)'").value)[0]
        if ($listed.policyConfigurationIngestionType) { $GPOObject.policyConfigurationIngestionType = $listed.policyConfigurationIngestionType }
    }

    $definitionValues = Invoke-HookRequest -Url "/deviceManagement/groupPolicyConfigurations/$($GPOObject.id)/definitionValues?`$expand=definition" -ODataMetadata skip
    # One presentationValues call per definition value would be the slowest part
    # of a big tenant, so they go through $batch.
    $requests = @(foreach ($definitionValue in $definitionValues.value) {
        [pscustomobject]@{
            id     = "$($definitionValue.id)"
            method = 'GET'
            url    = "/deviceManagement/groupPolicyConfigurations/$($GPOObject.id)/definitionValues/$($definitionValue.id)/presentationValues?`$expand=presentation"
        }
    })
    $responses = if ($requests) { Invoke-GraphBatchRequest $requests } else { @{} }
    foreach ($definitionValue in $definitionValues.value) {
        $definition = $definitionValue.definition
        $result = $responses["$($definitionValue.id)"]
        if ($result.status -ge 300 -or -not $result) {
            Add-ExportFailure -Status ($result.status ?? 'error')
            Write-Warning "$($script:CurrentObject): the values of setting $($definition.displayName) returned $($result.status)."
        }
        $presentationValues = $result.body
        $setting = [ordered]@{
            'enabled'               = $definitionValue.enabled
            'definition@odata.bind' = "https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions('$($definition.id)')"
        }
        if ($definition.categoryPath) {
            $setting['#Definition_Id'] = $definition.id
            $setting['#Definition_displayName'] = $definition.displayName
            $setting['#Definition_classType'] = $definition.classType
            $setting['#Definition_categoryPath'] = $definition.categoryPath
        }
        if ($presentationValues.value) {
            $setting.presentationValues = @(foreach ($presentationValue in $presentationValues.value) {
                $presentationValue | Add-Member -NotePropertyName 'presentation@odata.bind' -NotePropertyValue "https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions('$($definition.id)')/presentations('$($presentationValue.presentation.id)')"
                if ($definition.categoryPath) {
                    $presentationValue | Add-Member -NotePropertyName '#Presentation_Id' -NotePropertyValue $presentationValue.presentation.id
                    $presentationValue | Add-Member -NotePropertyName '#Presentation_Label' -NotePropertyValue $presentationValue.presentation.label
                }
                foreach ($property in 'presentation', 'id', 'lastModifiedDateTime', 'createdDateTime') {
                    $presentationValue.PSObject.Properties.Remove($property)
                }
                $presentationValue
            })
        }
        $setting
    }
}

function Start-PostGetAdministrativeTemplate {
    param($Item, $ObjectType)

    $definitionValues = @(Get-GPOObjectSettings $Item.Object)
    if ($definitionValues) {
        $Item.Object | Add-Member -NotePropertyName definitionValues -NotePropertyValue $definitionValues -Force
    }
}

function Start-PostExportAdministrativeTemplate {
    param($Object, $ObjectType, [string]$Folder)

    $settings = if ($Object.definitionValues) { $Object.definitionValues } else { Get-GPOObjectSettings $Object }
    Save-GraphObjectToFile $settings (Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType)_Settings.json")
}

function Start-PostExportScripts {
    param($Object, $ObjectType, [string]$Folder)

    if ($Object.scriptContent -and $script:Config.export_script_content) {
        Save-Base64File (Join-Path $Folder (Remove-InvalidFileNameChars $Object.fileName)) $Object.scriptContent
    }
}

function Start-GetAppProtection {
    param($Object, $ObjectType)

    if (-not $Object.'@odata.type') { return }
    $objectClass = Get-GraphObjectClassName $Object.'@odata.type'.Split('.')[-1]
    if ($objectClass) {
        $expand = if ($objectClass -eq 'windowsInformationProtectionPolicies') { '?$expand=protectedAppLockerFiles,exemptAppLockerFiles' }
        @{ API = "/deviceAppManagement/$objectClass/$($Object.id)$expand" }
    }
}

function Start-PostListAppProtection {
    param($Items, $ObjectType)

    # The managedAppPolicies list also returns app configurations, and its
    # $filter rejects isof() on them, so they are dropped here.
    $Items | Where-Object { $_.Object.'@odata.type' -ne '#microsoft.graph.targetedManagedAppConfiguration' }
}

function Start-PostExportAppConfiguration {
    param($Object, $ObjectType, [string]$Folder)

    if ($Object.'@odata.type' -notin '#microsoft.graph.androidManagedAppProtection', '#microsoft.graph.androidForWorkMobileAppConfiguration',
        '#microsoft.graph.androidManagedStoreAppConfiguration', '#microsoft.graph.iosMobileAppConfiguration') { return }
    $path = Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType).json"
    $saved = Get-GraphObjectFromFile $path
    $targetedApps = @(foreach ($appId in $saved.targetedMobileApps) {
        $app = Invoke-HookRequest -Url "/deviceAppManagement/mobileApps/$appId"
        if ($app) { "$($app.displayName)|!|$($app.id)|!|$($app.'@odata.type')" }
    })
    if ($targetedApps) {
        $saved | Add-Member -NotePropertyName '#CustomRefTargetedApps' -NotePropertyValue ($targetedApps -join '|*|') -Force
        Save-GraphObjectToFile $saved $path
    }
}

function Start-PostListApplications {
    param($Items, $ObjectType)

    foreach ($item in ($Items | Where-Object { $_.Object.'@odata.type' -eq '#microsoft.graph.winGetApp' })) {
        $installerType = if ($item.Object.packageIdentifier -like '9*') {
            'UWP'
        } elseif ($item.Object.packageIdentifier -like 'X*') {
            'Win32'
        } else {
            Write-Warning "Unknown package identifier for app $($item.Object.displayName): $($item.Object.packageIdentifier)"
            'Unknown'
        }
        $item.Object | Add-Member -NotePropertyName InstallerType -NotePropertyValue $installerType -Force
    }
    $Items
}

function Start-PostGetApplications {
    param($Item, $ObjectType)

    $app = $Item.Object
    if (($app.dependentAppCount -as [int]) -gt 0 -or ($app.supersededAppCount -as [int]) -gt 0) {
        $relationships = (Invoke-HookRequest -Url "/deviceAppManagement/mobileApps/$($app.id)/relationships?`$filter=targetType%20eq%20microsoft.graph.mobileAppRelationshipType%27child%27").value
        $dependencies = @($relationships | Where-Object '@odata.type' -EQ '#microsoft.graph.mobileAppDependency' | ForEach-Object {
            "$($_.targetDisplayName)|!|$($_.targetDisplayVersion)|!|$($_.targetId)|!|$($_.dependencyType)"
        })
        $supersedences = @($relationships | Where-Object '@odata.type' -EQ '#microsoft.graph.mobileAppSupersedence' | ForEach-Object {
            "$($_.targetDisplayName)|!|$($_.targetDisplayVersion)|!|$($_.targetId)|!|$($_.supersedenceType)"
        })
        if ($dependencies) { $app | Add-Member -NotePropertyName '#CustomRefDependency' -NotePropertyValue ($dependencies -join '|*|') }
        if ($supersedences) { $app | Add-Member -NotePropertyName '#CustomRefSupersedence' -NotePropertyValue ($supersedences -join '|*|') }
    }

    if ($app.'@odata.type' -eq '#microsoft.graph.win32LobApp' -and ($app.activeInstallScript.targetId -or $app.activeUninstallScript.targetId)) {
        $scriptsUrl = "/deviceAppManagement/mobileApps/$($app.id)/microsoft.graph.win32LobApp/contentVersions/$($app.committedContentVersion)/scripts"
        foreach ($scriptInfo in (Invoke-HookRequest -Url "$scriptsUrl/").value) {
            $content = (Invoke-HookRequest -Url "$scriptsUrl/$($scriptInfo.id)?`$select=Id,Content").content
            if ($content) { $scriptInfo | Add-Member -NotePropertyName content -NotePropertyValue $content -Force }
            $target = if ($app.activeInstallScript.targetId -eq $scriptInfo.id) {
                $app.activeInstallScript
            } elseif ($app.activeUninstallScript.targetId -eq $scriptInfo.id) {
                $app.activeUninstallScript
            }
            if ($target) {
                $target | Add-Member -NotePropertyName '#ScriptInfo' -NotePropertyValue $scriptInfo -Force
            } else {
                Write-Warning "Script with id $($scriptInfo.id) is not the active install or uninstall script of $($app.displayName). Skipping."
            }
        }
    }
}

function Start-PostExportApplications {
    param($Object, $ObjectType, [string]$Folder)

    $base = Join-Path $Folder (Get-ExportFileName $Object $ObjectType)
    if ($script:Config.export_script_content) {
        foreach ($rule in ($Object.detectionRules | Where-Object '@odata.type' -EQ '#microsoft.graph.win32LobAppPowerShellScriptDetection')) {
            if ($rule.scriptContent) { Save-Base64File "$($base)_DetectionScript.ps1" $rule.scriptContent }
        }
        foreach ($rule in ($Object.requirementRules | Where-Object '@odata.type' -EQ '#microsoft.graph.win32LobAppPowerShellScriptRequirement')) {
            if ($rule.scriptContent) { Save-Base64File "$($base)_RequirementScript.ps1" $rule.scriptContent }
        }
        foreach ($installScript in $Object.activeInstallScript, $Object.activeUninstallScript) {
            $scriptInfo = $installScript.'#ScriptInfo'
            if ($scriptInfo.displayName) {
                Save-Base64File "$($base)_$(Remove-InvalidFileNameChars $scriptInfo.displayName)" $scriptInfo.content
            }
        }
    }
    if ($script:Config.export_app_files -and $Object.committedContentVersion) { Save-AppPackage $Object $Folder }
}

function Find-AppEncryptionInfo {
    param($Object, $ContentFile)

    if (-not $env:INTUNE_APP_ENCRYPTION_KEYS) { return }
    try {
        $keys = $env:INTUNE_APP_ENCRYPTION_KEYS | ConvertFrom-Json -AsHashtable
    } catch {
        throw 'INTUNE_APP_ENCRYPTION_KEYS in .env is not JSON. Set it to an object that maps each encryption file name, without .json, to that file''s content.'
    }
    foreach ($name in "$($Object.displayName)_$($Object.id)_$($Object.committedContentVersion)",
        "$([IO.Path]::GetFileNameWithoutExtension($Object.fileName))_$($ContentFile.size)",
        "$($Object.displayName)_$($ContentFile.size)") {
        $info = $keys[$name]
        if ($info) { return $info.fileEncryptionInfo ?? $info }
    }
}

function Save-AppPackage {
    param($Object, [string]$Folder)

    $app = Invoke-HookRequest -Url "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($Object.id)"
    if (-not $app) { return }
    $filesUrl = "/deviceAppManagement/mobileApps/$($Object.id)/$($app.'@odata.type'.Trim('#'))/contentVersions/$($app.committedContentVersion)/files"
    $files = @((Invoke-HookRequest -Url $filesUrl).value)
    $contentFile = $null
    # Intune does not always list the files in upload order, so the last one
    # is only the first guess.
    foreach ($file in @($files[-1]) + @($files | Select-Object -SkipLast 1)) {
        if (-not $file) { continue }
        $contentFile = Invoke-GraphRequest -Url "$filesUrl/$($file.id)" -NoError
        if ($contentFile) { break }
    }
    if (-not $contentFile.azureStorageUri) {
        Write-Warning "Could not find the package file of app $($Object.displayName) ($($Object.id))."
        return
    }
    $encryption = Find-AppEncryptionInfo $Object $contentFile
    if (-not $encryption) {
        Write-Output "No encryption key in INTUNE_APP_ENCRYPTION_KEYS for app $($Object.displayName), so its package is not exported."
        return
    }

    $packageName = Remove-InvalidFileNameChars $Object.fileName
    $encrypted = Join-Path $Folder "$packageName.encrypted"
    $package = Join-Path $Folder $packageName
    $aes = [Security.Cryptography.Aes]::Create()
    $decryptor = $source = $target = $null
    $done = $false
    try {
        Invoke-WebRequest -Uri $contentFile.azureStorageUri -OutFile $encrypted -UseBasicParsing -TimeoutSec $RequestTimeoutSec
        $decryptor = $aes.CreateDecryptor([Convert]::FromBase64String($encryption.encryptionKey),
            [Convert]::FromBase64String($encryption.initializationVector))
        $source = [IO.File]::OpenRead($encrypted)
        $target = [IO.File]::Create($package)
        $stream = [Security.Cryptography.CryptoStream]::new($target, $decryptor, [Security.Cryptography.CryptoStreamMode]::Write, $true)
        # The first 48 bytes of an Intune package are its HMAC and IV.
        [void]$source.Seek(48, [IO.SeekOrigin]::Begin)
        $source.CopyTo($stream)
        $stream.FlushFinalBlock()
        $stream.Dispose()
        $done = $true
    } catch {
        Add-ExportFailure $_.Exception
        Write-Warning "$($script:CurrentObject): the package could not be downloaded or decrypted: $($_.Exception.Message)"
    } finally {
        foreach ($disposable in $target, $source, $decryptor, $aes) { if ($disposable) { $disposable.Dispose() } }
        Remove-Item -LiteralPath $encrypted -ErrorAction Ignore
        if (-not $done) { Remove-Item -LiteralPath $package -ErrorAction Ignore }
    }
}

function Start-PostGetSettingsCatalog {
    param($Item, $ObjectType)

    if (-not $Item.Object.assignments) {
        $assignments = (Invoke-HookRequest -Url "$($ObjectType.API)/$($Item.Object.id)/assignments").value
        if ($assignments) { $Item.Object | Add-Member -NotePropertyName assignments -NotePropertyValue $assignments -Force }
    }
}

function Start-PostExportPolicyFile {
    param($Object, $ObjectType, [string]$Folder)

    $content = $Object.($ObjectType.PolicyFileAttribute)
    if ($content -and $script:Config.export_policy_files) {
        # IntuneManagement reads a property of the object that never exists,
        # so a policy without fileName is written as ".json".
        $fileName = if ([string]::IsNullOrEmpty($Object.fileName)) { '.json' } else { $Object.fileName }
        Save-Base64File (Join-Path $Folder (Remove-InvalidFileNameChars $fileName)) $content
    }
}

function Start-PostExportRoleDefinitions {
    param($Object, $ObjectType, [string]$Folder)

    $path = Join-Path $Folder "$(Get-ExportFileName $Object $ObjectType).json"
    $saved = Get-GraphObjectFromFile $path
    $roleAssignments = @(foreach ($roleAssignment in $saved.roleAssignments) {
        $assignment = Invoke-HookRequest -Url "/deviceManagement/roleAssignments/$($roleAssignment.id)?`$expand=microsoft.graph.deviceAndAppManagementRoleAssignment/roleScopeTags" -ODataMetadata minimal
        if ($assignment) {
            foreach ($groupId in @($assignment.resourceScopes) + @($assignment.members)) { Add-GraphMigrationObject $groupId '/groups' 'Group' }
            $assignment
        }
    })
    if ($roleAssignments) {
        $saved.roleAssignments = $roleAssignments
        Save-GraphObjectToFile $saved $path
    }
}

function Start-PostGetScopeTags {
    param($Item, $ObjectType)

    $assignments = (Invoke-HookRequest -Url "$($ObjectType.API)/$($Item.Object.id)/assignments").value
    if ($assignments) { $Item.Object | Add-Member -NotePropertyName assignments -NotePropertyValue $assignments -Force }
}

function Start-PostExportDeviceHealthScripts {
    param($Object, $ObjectType, [string]$Folder)

    if (-not $script:Config.export_script_content) { return }
    $base = Join-Path $Folder (Get-ExportFileName $Object $ObjectType)
    if ($Object.detectionScriptContent) { Save-Base64File "$($base)_DetectionScript.ps1" $Object.detectionScriptContent }
    if ($Object.remediationScriptContent) { Save-Base64File "$($base)_RemediationScript.ps1" $Object.remediationScriptContent }
}

function Start-PostGetReusableSettings {
    param($Item, $ObjectType)

    $response = Invoke-HookRequest -Url "$($ObjectType.API)/$($Item.Object.id)?`$select=settinginstance,displayname,description"
    if ($response.settingInstance) {
        $Item.Object | Add-Member -NotePropertyName settingInstance -NotePropertyValue $response.settingInstance -Force
    }
}

function Start-GetObjectNameTenantSettings {
    'Tenant Settings'
}

function Start-IntuneExport {
    $script:Config = Get-ExportConfig $env:PDT_CONFIG_JSON
    $script:Token = $null
    [void](Get-GraphToken)
    $script:Metadata = $null
    $script:MetadataLoaded = $false
    $script:MigrationCache = @{}
    $script:MigrationObjects = [Collections.Generic.List[object]]::new()
    $script:Failures = [Collections.Generic.List[string]]::new()
    $script:ExportedFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

    $script:Organization = @((Invoke-GraphRequest -Url 'organization' -ODataMetadata skip -NoError).value)[0]
    if (-not $script:Organization) {
        Write-Warning 'Could not read the organization. Grant the app Organization.Read.All to name the company folder.'
        $script:Organization = [pscustomobject]@{ id = $script:TenantId; displayName = '' }
    }

    # Outside pdt the zip goes to the temp folder, not the app folder, because
    # it can hold secrets and a deploy copies the app folder into the image.
    $outputDir = $env:PDT_OUTPUT_DIR ?? [IO.Path]::GetTempPath()
    $name = "IntuneExport-$(Get-Date -Format yyyy-MM-dd)"
    $work = Join-Path ([IO.Path]::GetTempPath()) "intune-export-$([guid]::NewGuid())"
    $root = Join-Path $work $name
    try {
        [IO.Directory]::CreateDirectory($root) | Out-Null
        $script:MigrationFolder = $root
        if ($script:Config.add_company_name) {
            $script:MigrationFolder = Join-Path $root (Remove-InvalidFileNameChars $script:Organization.displayName)
        }

        $types = if ($script:Config.object_types) { $IntuneTypes | Where-Object Id -In $script:Config.object_types } else { $IntuneTypes | Where-Object { -not $_.InfoView } }
        $filter = $script:Config.name_filter.Trim()
        $total = 0
        foreach ($type in $types) {
            $folder = $script:MigrationFolder
            if ($script:Config.add_object_type) { $folder = Join-Path $folder $type.Id }
            try {
                $objects = @(Get-GraphObjects $type | Where-Object {
                    -not $filter -or (Get-GraphObjectName $_.Object $type) -match [regex]::Escape($filter)
                })
                $items = if ($script:Config.use_batch_api) { @(Get-GraphBatchObjects $objects $type) } else { $objects }
            } catch {
                $script:CurrentObject = $type.Title
                Add-ExportFailure $_.Exception
                Write-Warning "$($type.Title): $($_.Exception.Message)"
                continue
            }
            $count = 0
            foreach ($item in $items) {
                $script:CurrentObject = "$($type.Title): $(Get-GraphObjectName $item.Object $type)"
                try {
                    $full = if ($script:Config.use_batch_api) { $item } else { Get-GraphObject $item.Object $type }
                    if ($type.PostGet) { Invoke-Command $type.PostGet -ArgumentList $full, $type }
                    Export-GraphObject $full.Object $type $folder
                    $count++
                } catch {
                    Add-ExportFailure $_.Exception
                    Write-Warning "$($script:CurrentObject): $($_.Exception.Message)"
                }
            }
            Write-Output "$($type.Title): $count objects"
            $total += $count
        }

        if ($script:MigrationObjects.Count -gt 0) {
            [IO.Directory]::CreateDirectory($script:MigrationFolder) | Out-Null
            # IntuneManagement compares TenantId with the tenant it imports into,
            # so this file keeps the real id.
            $table = [ordered]@{
                TenantId     = $script:Organization.id
                Organization = $script:Organization.displayName
                Objects      = @($script:MigrationObjects)
            }
            [IO.File]::WriteAllText((Join-Path $script:MigrationFolder 'MigrationTable.json'),
                ($table | ConvertTo-Json -Depth 50) + [Environment]::NewLine, [Text.UTF8Encoding]::new($true))
        }

        $zip = Join-Path $outputDir "$name.zip"
        Remove-Item $zip -Force -ErrorAction Ignore
        [IO.Compression.ZipFile]::CreateFromDirectory($root, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction Ignore
    }
    Write-Output "Wrote $name.zip with $total objects."
    if (-not $env:PDT_OUTPUT_DIR) { Write-Output "It is in $outputDir." }

    if ($script:Failures.Count -gt 0) {
        $typeFailures = @($script:Failures | Where-Object { $_ -notlike '*: *' })
        $message = @("The zip is incomplete. These failed: $($script:Failures -join ', ').")
        if ($script:Failures -match '\(403\)$') {
            $message += '403 means the app lacks a Microsoft Graph permission: add it to the app in Entra and grant admin consent.'
        }
        if ($typeFailures -match '\(40[04]\)$') {
            $message += '400 or 404 for a whole object type often means the tenant does not use that feature: remove the type from object_types in config.yml.'
        }
        if ($script:Failures | Where-Object { $_ -notmatch '\(403\)$' -and ($_ -like '*: *' -or $_ -notmatch '\(40[04]\)$') }) {
            $message += 'For any other failure, check the named object in Intune. The next run tries again.'
        }
        throw ($message -join ' ')
    }
}

if ($MyInvocation.InvocationName -ne '.') { Start-IntuneExport }
