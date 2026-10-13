"""Recorded Microsoft Graph responses for the IntuneExport.ps1 harness, in the shape of a
full-metadata response, for the tenant of Contoso. `build()` returns the map the harness
reads: "<METHOD> <path>" to a response."""

import base64
import copy

TENANT = "00000000-0000-0000-0000-000000000000"
GRAPH = "https://graph.microsoft.com/beta/"


def gid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def ok(body):
    return {"status": 200, "body": body}


def error(status, message):
    return {"status": status, "body": {"error": {"code": "Forbidden" if status == 403 else "NotFound", "message": message}}}


def listing(api, objects, next_link=None):
    body = {"@odata.context": f"{GRAPH}$metadata#{api.split('?')[0]}", "value": objects}
    if next_link:
        body["@odata.nextLink"] = GRAPH + next_link
    return ok(body)


def item(api, odata_type, number, links=(), **props):
    """A listed object: its @odata.type, id, properties, and one navigationLink per name in links."""
    object_id = number if isinstance(number, str) else gid(number)
    obj = {"@odata.type": f"#microsoft.graph.{odata_type}", "id": object_id, **props}
    for name in links:
        obj[f"{name}@odata.navigationLink"] = f"{GRAPH}{api}('{object_id}')/{name}"
    return obj


def full(api, obj, **props):
    return ok({"@odata.context": f"{GRAPH}$metadata#{api}/$entity", **copy.deepcopy(obj), **props})


def group_target(number, odata_type="groupAssignmentTarget"):
    return {"id": f"{gid(number)}_0", "target": {"@odata.type": f"#microsoft.graph.{odata_type}", "groupId": gid(number)}}


def all_users():
    return {"id": "all_0", "target": {"@odata.type": "#microsoft.graph.allLicensedUsersAssignmentTarget"}}


GROUPS = {
    901: "Contoso Sales",
    903: "Contoso Compliance CC",
    904: "Contoso Terms Users",
    905: "Contoso: Settings Users",
    906: "Contoso Scope",
    907: "Contoso Helpdesk Admins",
    908: "Contoso Scope Tag Users",
    909: "Contoso Cloud PC Users",
}


METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="microsoft.graph" Alias="graph" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="entity" Abstract="true">
        <Key><PropertyRef Name="id" /></Key>
        <Property Name="id" Type="Edm.String" Nullable="false" />
      </EntityType>
      <EntityType Name="deviceConfiguration" BaseType="graph.entity" Abstract="true">
        <Property Name="displayName" Type="Edm.String" />
        <NavigationProperty Name="assignments" Type="Collection(graph.deviceConfigurationAssignment)" ContainsTarget="true" />
      </EntityType>
      <EntityType Name="windows10CustomConfiguration" BaseType="graph.deviceConfiguration" />
      <EntityType Name="windows10GeneralConfiguration" BaseType="graph.deviceConfiguration">
        <NavigationProperty Name="privacyAccessControls" Type="Collection(graph.windowsPrivacyDataAccessControlItem)" ContainsTarget="true" />
      </EntityType>
      <EntityType Name="androidWorkProfileWiFiConfiguration" BaseType="graph.deviceConfiguration" />
      <EntityType Name="androidWorkProfileEnterpriseWiFiConfiguration" BaseType="graph.androidWorkProfileWiFiConfiguration">
        <NavigationProperty Name="rootCertificateForServerValidation" Type="graph.androidWorkProfileTrustedRootCertificate" />
      </EntityType>
      <EntityType Name="deviceAppManagement" BaseType="graph.entity">
        <NavigationProperty Name="iosManagedAppProtections" Type="Collection(graph.iosManagedAppProtection)" ContainsTarget="true" />
        <NavigationProperty Name="windowsInformationProtectionPolicies" Type="Collection(graph.windowsInformationProtectionPolicy)" ContainsTarget="true" />
        <NavigationProperty Name="targetedManagedAppConfigurations" Type="Collection(graph.targetedManagedAppConfiguration)" ContainsTarget="true" />
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""

PACKAGE_KEY = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
PACKAGE_IV = "ZWZnaGlqa2xtbm9wcXJzdA=="
ENCRYPTED_PACKAGE = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vtKlAvPDbysYRLt684C9nvELJJbpQfaAjWs0pfMthqdI=")
PACKAGE_URL = "https://contoso.blob.core.windows.net/package?sig=placeholder"


def simple(g, api, odata_type, number, links=("assignments",), query="", expand="", details=None, **props):
    """A type with one listed object. Its full object adds `details`."""
    obj = item(api, odata_type, number, links, **props)
    g[f"GET {api}{query}"] = listing(api, [obj])
    g[f"GET {api}/{obj['id']}{expand}"] = full(api, obj, **(details or {}))


def build():
    g = {}
    g["GET organization"] = ok({"value": [{"id": TENANT, "displayName": "Contoso"}]})
    g["GET $metadata"] = {"content": METADATA}
    for number, name in GROUPS.items():
        g[f"GET groups/{gid(number)}"] = ok({"id": gid(number), "displayName": name, "mailEnabled": False, "securityEnabled": True})
    g[f"GET groups/{gid(902)}"] = error(404, f"Resource '{gid(902)}' does not exist.")
    g[f"GET users/{gid(951)}"] = ok({"id": gid(951), "displayName": "Contoso Admin", "userPrincipalName": "admin@contoso.com"})

    api = "deviceManagement/deviceConfigurations"
    query = ("?$filter=not%20isof(%27microsoft.graph.windowsUpdateForBusinessConfiguration%27)"
             "%20and%20not%20isof(%27microsoft.graph.iosUpdateConfiguration%27)")
    oma = item(api, "windows10CustomConfiguration", 101, ["assignments"], displayName="Contoso OMA")
    wifi = item(api, "androidWorkProfileEnterpriseWiFiConfiguration", 102, ["assignments"], displayName="Contoso WiFi")
    general = item(api, "windows10GeneralConfiguration", 104, ["assignments"], displayName="Contoso General")
    general["privacyAccessControls@odata.associationLink"] = f"{GRAPH}{api}('{gid(104)}')/privacyAccessControls/$ref"
    certificate = f"{api}('{gid(102)}')/microsoft.graph.androidWorkProfileEnterpriseWiFiConfiguration/rootCertificateForServerValidation"
    g[f"GET {api}{query}"] = listing(api, [wifi, oma, general])
    g[f"GET {api}/{gid(101)}?$expand=assignments"] = full(api, oma, assignments=[
        group_target(901), group_target(902, "exclusionGroupAssignmentTarget"), all_users()], omaSettings=[
        {"@odata.type": "#microsoft.graph.omaSettingString", "displayName": "Proxy", "omaUri": "./Device/Proxy",
         "isEncrypted": True, "secretReferenceValueId": "secret-1", "value": "****"},
        {"@odata.type": "#microsoft.graph.omaSettingStringXml", "displayName": "Profile", "omaUri": "./Device/Profile",
         "fileName": "profile.xml", "isEncrypted": True, "secretReferenceValueId": "secret-2", "value": "****"},
        {"@odata.type": "#microsoft.graph.omaSettingInteger", "displayName": "Level", "omaUri": "./Device/Level",
         "isEncrypted": False, "secretReferenceValueId": None, "value": 1},
    ])
    for secret, value in (("secret-1", "proxy.contoso.com"), ("secret-2", "<profile>contoso</profile>")):
        g[f"GET {api}/{gid(101)}/getOmaSettingPlainTextValue(secretReferenceValueId='{secret}')"] = ok(
            {"@odata.context": f"{GRAPH}$metadata#Edm.String", "value": value})
    g[f"GET {api}/{gid(102)}?$expand=assignments"] = full(api, wifi, assignments=[], **{
        "rootCertificateForServerValidation@odata.navigationLink": GRAPH + certificate})
    g[f"GET {certificate}"] = ok({"@odata.context": f"{GRAPH}$metadata#deviceManagement/deviceConfigurations/$entity",
                                  "@odata.type": "#microsoft.graph.androidWorkProfileTrustedRootCertificate",
                                  "id": gid(103), "displayName": "Contoso Root CA"})
    g[f"GET {api}/{gid(104)}?$expand=assignments,microsoft.graph.windows10GeneralConfiguration/privacyAccessControls"] = full(
        api, general, assignments=[], privacyAccessControls=[])

    api = "identity/conditionalAccess/policies"
    policy = item(api, "conditionalAccessPolicy", 201, displayName="Contoso Require MFA")
    g[f"GET {api}"] = listing(api, [policy])
    g[f"GET {api}/{gid(201)}"] = full(api, policy, state="enabled", conditions={"users": {
        "includeGroups": [gid(901)], "excludeGroups": [gid(902)],
        "includeUsers": [gid(951), "All"], "excludeUsers": ["GuestsOrExternalUsers"]}})

    api = "identityGovernance/termsOfUse/agreements"
    agreement = item(api, "agreement", 301, displayName="Contoso Terms")
    files = [{"id": gid(302), "fileName": "Contoso Terms.pdf", "language": "en", "fileData": None}]
    g[f"GET {api}?$expand=files"] = listing(api, [{**agreement, "files": files}])
    g[f"GET {api}/{gid(301)}?$expand=files"] = full(api, agreement, files=files)
    g[f"GET agreements/{gid(301)}/file/localizations('{gid(302)}')/fileData/data"] = ok({"value": b64("%PDF Contoso terms")})

    api = "identity/conditionalAccess/namedLocations"
    first = item(api, "ipNamedLocation", 401, displayName="Contoso Office")
    second = item(api, "countryNamedLocation", 402, displayName="Contoso Countries")
    g[f"GET {api}"] = listing(api, [first], next_link=f"{api}?$skiptoken=page2")
    g[f"GET {api}?$skiptoken=page2"] = listing(api, [second])
    g[f"GET {api}/{gid(401)}"] = full(api, first)
    g[f"GET {api}/{gid(402)}"] = full(api, second)

    api = "deviceManagement/intents"
    simple(g, api, "deviceManagementIntent", 501, displayName="Contoso Firewall", templateId=gid(502), expand="?$expand=assignments")
    g[f"GET {api}/{gid(501)}/settings"] = ok({"value": [
        {"@odata.type": "#microsoft.graph.deviceManagementBooleanSettingInstance", "id": "firewall", "definitionId": "firewallEnabled",
         "valueJson": "true", "value": True}]})

    api = "deviceManagement/deviceCompliancePolicies"
    compliance = item(api, "windows10CompliancePolicy", 601, ["assignments"], displayName="Contoso Compliance")
    g[f"GET {api}"] = listing(api, [compliance])
    g[f"GET {api}/{gid(601)}?$expand=assignments,scheduledActionsForRule($expand=scheduledActionConfigurations)"] = full(
        api, compliance, assignments=[], scheduledActionsForRule=[{"ruleName": "PasswordRequired", "scheduledActionConfigurations": [
            {"actionType": "notification", "gracePeriodHours": 0, "notificationMessageCCList": [gid(903)]}]}])

    simple(g, "deviceManagement/compliancePolicies", "deviceManagementCompliancePolicy", 701, ["assignments", "settings"],
           name="Contoso Linux Compliance", expand="?$expand=assignments,settings")

    api = "deviceManagement/deviceComplianceScripts"
    script = item(api, "deviceComplianceScript", 801, ["assignments"], displayName="Contoso/Check: Disk?")
    g[f"GET {api}"] = listing(api, [script])
    g[f"GET {api}/{gid(801)}?$expand=assignments"] = full(api, script, assignments=[])

    api = "deviceManagement/intuneBrandingProfiles"
    simple(g, api, "intuneBrandingProfile", 1001, profileName="Contoso Branding", expand="?$expand=assignments",
           themeColorLogo=None, lightBackgroundLogo=None, landingPageCustomizedImage=None)
    g[f"GET {api}/{gid(1001)}/themeColorLogo"] = ok({"type": "image/jpeg", "value": b64("JPEG Contoso logo")})
    for image in ("lightBackgroundLogo", "landingPageCustomizedImage"):
        g[f"GET {api}/{gid(1001)}/{image}"] = ok({"type": None, "value": None})

    api = f"organization/{TENANT}/branding/localizations"
    branding = item(api, "organizationalBrandingLocalization", "0", signInPageText="Welcome to Contoso")
    g[f"GET {api}"] = listing(api, [branding])
    g[f"GET {api}/0"] = full(api, branding)

    api = "deviceManagement/deviceEnrollmentConfigurations"
    enrollment = [
        item(api, "windows10EnrollmentCompletionPageConfiguration", f"{gid(1101)}_DefaultWindows10EnrollmentCompletionPageConfiguration",
             ["assignments"], displayName="All users and all devices", priority=0),
        item(api, "windows10EnrollmentCompletionPageConfiguration", 1102, ["assignments"], displayName="Contoso ESP", priority=1),
        item(api, "deviceEnrollmentPlatformRestrictionsConfiguration", f"{gid(1103)}_DefaultPlatformRestrictions",
             ["assignments"], displayName="All Users", priority=0),
        item(api, "deviceEnrollmentPlatformRestrictionConfiguration", f"{gid(1104)}_PlatformRestrictions",
             ["assignments"], displayName="Contoso Hidden Restriction", priority=0),
        item(api, "deviceEnrollmentLimitConfiguration", 1105, ["assignments"], displayName="Contoso Device Limit", priority=1),
        item(api, "deviceEnrollmentPlatformRestrictionConfiguration", 1107, ["assignments"], displayName="Contoso Phones",
             priority=2, platformType="WindowsPhone"),
        item(api, "deviceComanagementAuthorityConfiguration", 1106, ["assignments"], displayName="Contoso Co-management", priority=1),
    ]
    g[f"GET {api}"] = listing(api, enrollment)
    for obj in enrollment:
        g[f"GET {api}/{obj['id']}?$expand=assignments"] = full(api, obj, assignments=[])

    api = "deviceManagement/groupPolicyConfigurations"
    simple(g, api, "groupPolicyConfiguration", 1401, displayName="Contoso Edge", policyConfigurationIngestionType="unknown",
           expand="?$expand=assignments")
    g[f"GET {api}?$filter=id eq '{gid(1401)}'"] = listing(api, [{"id": gid(1401), "policyConfigurationIngestionType": "custom"}])
    g[f"GET {api}/{gid(1401)}/definitionValues?$expand=definition"] = ok({"value": [
        {"id": gid(1402), "enabled": True, "createdDateTime": "2024-01-02T03:04:05Z",
         "definition": {"id": gid(1403), "displayName": "Home page", "classType": "user", "categoryPath": "\\Contoso\\Edge"}}]})
    g[f"GET {api}/{gid(1401)}/definitionValues/{gid(1402)}/presentationValues?$expand=presentation"] = ok({"value": [
        {"@odata.type": "#microsoft.graph.groupPolicyPresentationValueText", "id": gid(1404), "value": "https://contoso.com",
         "lastModifiedDateTime": "2024-01-02T03:04:05Z", "createdDateTime": "2024-01-02T03:04:05Z",
         "presentation": {"id": gid(1405), "label": "Home page URL"}}]})

    for api, odata_type, number, name, file_name in (
            ("deviceManagement/deviceManagementScripts", "deviceManagementScript", 1501, "Contoso Script", "contoso.ps1"),
            ("deviceManagement/deviceShellScripts", "deviceShellScript", 1601, "Contoso Shell", "contoso.sh"),
            ("deviceManagement/deviceCustomAttributeShellScripts", "deviceCustomAttributeShellScript", 1701, "Contoso Attribute",
             "attribute.sh")):
        simple(g, api, odata_type, number, displayName=name, fileName=file_name, scriptContent=b64(f"echo {name}"),
               expand="?$expand=assignments")

    api = "deviceManagement/termsAndConditions"
    simple(g, api, "termsAndConditions", 1801, displayName="Contoso Conditions")
    g[f"GET {api}/{gid(1801)}/assignments"] = ok({"value": [{"id": "tc_0", "target": {
        "@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": gid(904)}}]})

    api = "deviceAppManagement/managedAppPolicies"
    ios = item(api, "iosManagedAppProtection", 1901, ["assignments", "apps"], displayName="Contoso iOS MAM")
    config = item(api, "targetedManagedAppConfiguration", 1902, ["assignments", "apps"], displayName="Contoso App Config")
    wip = item(api, "windowsInformationProtectionPolicy", 1903, ["assignments"], displayName="Contoso WIP")
    g[f"GET {api}"] = listing(api, [ios, config, wip])
    g[f"GET deviceAppManagement/iosManagedAppProtections/{gid(1901)}?$expand=assignments,apps"] = full(
        "deviceAppManagement/iosManagedAppProtections", ios, assignments=[], apps=[])
    g[f"GET deviceAppManagement/windowsInformationProtectionPolicies/{gid(1903)}"
      "?$expand=protectedAppLockerFiles,exemptAppLockerFiles,assignments"] = full(
        "deviceAppManagement/windowsInformationProtectionPolicies", wip, assignments=[], protectedAppLockerFiles=[])

    simple(g, "deviceAppManagement/targetedManagedAppConfigurations", "targetedManagedAppConfiguration", 2001,
           ["assignments", "apps"], displayName="Contoso Managed App Config", expand="?$expand=assignments,apps",
           details={"assignments": [], "apps": []})

    api = "deviceAppManagement/mobileAppConfigurations"
    simple(g, api, "iosMobileAppConfiguration", 2101, displayName="Contoso Outlook Config", targetedMobileApps=[gid(2201)],
           expand="?$expand=assignments",
           query="?$filter=microsoft.graph.androidManagedStoreAppConfiguration/appSupportsOemConfig%20eq%20false%20or%20"
                 "isof(%27microsoft.graph.androidManagedStoreAppConfiguration%27)%20eq%20false")
    simple(g, api, "androidManagedStoreAppConfiguration", 3501, displayName="Contoso OEM Config", targetedMobileApps=[gid(2201)],
           expand="?$expand=assignments",
           query="?$filter=microsoft.graph.androidManagedStoreAppConfiguration/appSupportsOemConfig%20eq%20true")

    api = "deviceAppManagement/mobileApps"
    outlook = {"@odata.type": "#microsoft.graph.iosStoreApp", "id": gid(2201), "displayName": "Contoso Outlook"}
    agent = {"@odata.type": "#microsoft.graph.win32LobApp", "id": gid(2202), "displayName": "Contoso Agent",
             "fileName": "agent.intunewin", "committedContentVersion": "1", "dependentAppCount": 1, "supersededAppCount": 0,
             "activeInstallScript": {"targetId": gid(2210)}, "activeUninstallScript": {"targetId": None},
             "detectionRules": [{"@odata.type": "#microsoft.graph.win32LobAppPowerShellScriptDetection",
                                 "scriptContent": b64("Test-Path C:\\Contoso")}],
             "requirementRules": [{"@odata.type": "#microsoft.graph.win32LobAppPowerShellScriptRequirement",
                                   "scriptContent": b64("$true")}]}
    winget = {"@odata.type": "#microsoft.graph.winGetApp", "id": gid(2203), "displayName": "Contoso Store App",
              "packageIdentifier": "9WZDNCRFJ3PZ"}
    query = ("?$filter=(microsoft.graph.managedApp/appAvailability%20eq%20null%20or%20microsoft.graph.managedApp/appAvailability"
             "%20eq%20%27lineOfBusiness%27%20or%20isAssigned%20eq%20true)&$orderby=displayName")
    g[f"GET {api}{query}"] = listing(api, [agent, outlook, winget])
    g[f"GET {api}/{gid(2201)}"] = full(api, outlook)
    g[f"GET {api}/{gid(2202)}"] = full(api, agent)
    for obj in (outlook, agent, winget):
        g[f"GET {api}/{obj['id']}?$expand=categories,assignments"] = full(api, obj, categories=[], assignments=[group_target(901)])
    g[f"GET {api}/{gid(2202)}/relationships?$filter=targetType%20eq%20microsoft.graph.mobileAppRelationshipType%27child%27"] = ok(
        {"value": [{"@odata.type": "#microsoft.graph.mobileAppDependency", "targetDisplayName": "Contoso Runtime",
                    "targetDisplayVersion": "2.0", "targetId": gid(2204), "dependencyType": "autoInstall"}]})
    scripts = f"{api}/{gid(2202)}/microsoft.graph.win32LobApp/contentVersions/1/scripts"
    g[f"GET {scripts}/"] = ok({"value": [{"@odata.type": "#microsoft.graph.win32LobAppInstallPowerShellScript",
                                         "id": gid(2210), "displayName": "install.ps1", "content": None}]})
    g[f"GET {scripts}/{gid(2210)}?$select=Id,Content"] = ok({"id": gid(2210), "content": b64("Install-Contoso")})
    files = f"{api}/{gid(2202)}/microsoft.graph.win32LobApp/contentVersions/1/files"
    g[f"GET {files}"] = ok({"value": [{"id": gid(2220)}, {"id": gid(2221)}]})
    g[f"GET {files}/{gid(2221)}"] = error(404, "Not found")
    g[f"GET {files}/{gid(2220)}"] = ok({"id": gid(2220), "size": 21, "azureStorageUri": PACKAGE_URL})
    g[f"GET {PACKAGE_URL}"] = {"bytes": ENCRYPTED_PACKAGE}

    simple(g, "deviceManagement/windowsAutopilotDeploymentProfiles", "azureADWindowsAutopilotDeploymentProfile", 2301,
           displayName="Contoso Autopilot", expand="?$expand=assignments",
           details={"assignments": [group_target(901), all_users()]})

    simple(g, "deviceAppManagement/policySets", "policySet", 2401, displayName="Contoso Baseline Set", status="notAssigned",
           expand="?$expand=assignments,Items")
    simple(g, "deviceManagement/deviceConfigurations", "windowsUpdateForBusinessConfiguration", 2501, displayName="Contoso Ring",
           query="?$filter=isof(%27microsoft.graph.windowsUpdateForBusinessConfiguration%27)%20or%20isof(%27microsoft.graph."
                 "iosUpdateConfiguration%27)", expand="?$expand=assignments")
    simple(g, "deviceManagement/windowsFeatureUpdateProfiles", "windowsFeatureUpdateProfile", 2601, displayName="Contoso Feature",
           expand="?$expand=assignments")
    simple(g, "deviceManagement/windowsQualityUpdateProfiles", "windowsQualityUpdateProfile", 2701, displayName="Contoso Quality",
           expand="?$expand=assignments")
    simple(g, "deviceManagement/windowsQualityUpdatePolicies", "windowsQualityUpdatePolicy", 2801,
           displayName="Contoso Quality Policy", expand="?$expand=assignments")

    api = "deviceManagement/configurationPolicies"
    simple(g, api, "deviceManagementConfigurationPolicy", 2901, ["settings"], name="Contoso Settings", expand="?$expand=settings")
    g[f"GET {api}/{gid(2901)}/assignments"] = ok({"value": [group_target(905)]})

    simple(g, "deviceManagement/inventoryPolicies", "deviceManagementInventoryPolicy", 3001, (), name="Contoso Inventory",
           expand="?$expand=Settings")
    simple(g, "deviceManagement/hardwareConfigurations", "hardwareConfiguration", 3101, displayName="Contoso BIOS",
           fileName="bios.bin", configurationFileContent=b64("BIOS Contoso"), expand="?$expand=assignments")

    api = "deviceManagement/roleDefinitions"
    simple(g, api, "deviceAndAppManagementRoleDefinition", 3201, ["roleAssignments"], displayName="Contoso Helpdesk",
           query="?$filter=isBuiltIn%20eq%20false", expand="?$expand=roleAssignments",
           details={"roleAssignments": [{"id": gid(3202), "displayName": "Contoso Helpdesk Assignment"}]})
    g[f"GET deviceManagement/roleAssignments/{gid(3202)}"
      "?$expand=microsoft.graph.deviceAndAppManagementRoleAssignment/roleScopeTags"] = ok({
        "@odata.type": "#microsoft.graph.deviceAndAppManagementRoleAssignment", "id": gid(3202),
        "displayName": "Contoso Helpdesk Assignment", "resourceScopes": [gid(906)], "members": [gid(907)], "roleScopeTags": []})

    api = "deviceManagement/roleScopeTags"
    simple(g, api, "roleScopeTag", 3301, displayName="Contoso Tag", query="?$filter=isBuiltIn%20eq%20false",
           expand="?$expand=assignments", details={"assignments": []})
    g[f"GET {api}/{gid(3301)}/assignments"] = ok({"value": [group_target(908)]})

    simple(g, "deviceManagement/notificationMessageTemplates", "notificationMessageTemplate", 3401, (),
           displayName="Contoso Notice", expand="?$expand=localizedNotificationMessages")
    simple(g, "deviceManagement/appleUserInitiatedEnrollmentProfiles", "appleUserInitiatedEnrollmentProfile", 3601,
           displayName="Contoso Apple", expand="?$expand=assignments")
    simple(g, "deviceManagement/assignmentFilters", "deviceAndAppManagementAssignmentFilter", 3701, (),
           displayName="Contoso Filter")
    simple(g, "deviceManagement/deviceHealthScripts", "deviceHealthScript", 3801, displayName="Contoso Remediation",
           detectionScriptContent=b64("exit 1"), remediationScriptContent=b64("exit 0"),
           query="?$filter=isGlobalScript%20eq%20false", expand="?$expand=assignments")
    simple(g, "deviceManagement/groupPolicyUploadedDefinitionFiles", "groupPolicyUploadedDefinitionFile", 3901, (),
           fileName="contoso.admx")

    api = "deviceManagement/reusablePolicySettings"
    simple(g, api, "deviceManagementReusablePolicySetting", 4001, (), displayName="Contoso Reusable")
    g[f"GET {api}/{gid(4001)}?$select=settinginstance,displayname,description"] = ok({
        "displayName": "Contoso Reusable", "settingInstance": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
                                                               "settingDefinitionId": "contoso_setting"}})

    simple(g, "identity/conditionalAccess/authenticationStrengths/policies", "authenticationStrengthPolicy", 4101, (),
           displayName="Contoso Strength", policyType="custom")
    simple(g, "identity/conditionalAccess/authenticationContextClassReferences", "authenticationContextClassReference", "c1", (),
           displayName="Contoso Context")
    simple(g, "deviceManagement/virtualEndpoint/provisioningPolicies", "cloudPcProvisioningPolicy", 4301,
           displayName="Contoso Cloud PC", expand="?$expand=assignments",
           details={"assignments": [group_target(909, "cloudPcManagementGroupAssignmentTarget")]})
    simple(g, "deviceManagement/virtualEndpoint/userSettings", "cloudPcUserSetting", 4401, displayName="Contoso User Setting",
           expand="?$expand=assignments")
    simple(g, "deviceManagement/windowsDriverUpdateProfiles", "windowsDriverUpdateProfile", 4501, displayName="Contoso Drivers",
           expand="?$expand=assignments")
    simple(g, "deviceManagement/deviceCategories", "deviceCategory", 4601, (), displayName="Contoso Laptops", query="?$top=500")
    simple(g, "deviceManagement/templates", "securityBaselineTemplate", 4701, (), displayName="Contoso Baseline")
    simple(g, "deviceManagement/configurationPolicyTemplates", "deviceManagementConfigurationPolicyTemplate", 4801, (),
           displayName="Contoso Baseline Template", query="?$filter=(templateFamily eq 'Baseline')")
    api = "deviceManagement/androidManagedStoreAccountEnterpriseSettings"
    play = {"@odata.type": "#microsoft.graph.androidManagedStoreAccountEnterpriseSettings", "id": gid(4901),
            "displayName": "Contoso Google Play", "bindStatus": "boundAndValidated"}
    g[f"GET {api}"] = ok({"@odata.context": f"{GRAPH}$metadata#{api}/$entity", **play})
    g[f"GET {api}/{gid(4901)}"] = full(api, play)
    simple(g, "deviceManagement/androidDeviceOwnerEnrollmentProfiles", "androidDeviceOwnerEnrollmentProfile", 5001, (),
           displayName="Contoso Android Enrolment")
    simple(g, "deviceAppManagement/vppTokens", "vppToken", 5101, (), displayName="Contoso VPP")
    api = "deviceManagement/depOnboardingSettings"
    token = item(api, "depOnboardingSetting", 5201, displayName="Contoso DEP", tokenName="Contoso DEP")
    g[f"GET {api}/?$top=100"] = listing(api, [token])
    g[f"GET {api}/{gid(5201)}"] = full(api, token)
    api = "deviceManagement/settings"
    g[f"GET {api}"] = ok({"@odata.context": f"{GRAPH}$metadata#deviceManagement/settings",
                          "deviceComplianceCheckinThresholdDays": 30, "secureByDefault": True})

    return g
