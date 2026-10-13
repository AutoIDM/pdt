"""The intune-export example: `IntuneExport.ps1` runs through tests/intune_export/harness.ps1
against recorded Microsoft Graph responses, and these tests read the zip it writes."""

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from intune_export import graph_fixtures
from intune_export.graph_fixtures import TENANT, gid

SCRIPT = Path(__file__).resolve().parent.parent / "src/pdt/examples/intune-export/IntuneExport.ps1"
HARNESS = Path(__file__).resolve().parent / "intune_export/harness.ps1"
GENERIC = ["AutoPilot", "NamedLocations", "ComplianceScripts", "AzureBranding", "TenantSettings", "DeviceCategories"]

REGISTRY = [
    "DeviceConfiguration", "ConditionalAccess", "TermsOfUse", "NamedLocations", "EndpointSecurity",
    "CompliancePolicies", "CompliancePoliciesV2", "ComplianceScripts", "IntuneBranding", "AzureBranding",
    "EnrollmentStatusPage", "EnrollmentRestrictions", "CoManagementSettings", "AdministrativeTemplates",
    "PowerShellScripts", "MacScripts", "MacCustomAttributes", "TermsAndConditions", "AppProtection",
    "AppConfigurationManagedApp", "AppConfigurationManagedDevice", "Applications", "AutoPilot", "PolicySets",
    "UpdatePolicies", "FeatureUpdates", "QualityUpdates", "QualityUpdatePolicies", "SettingsCatalog",
    "InventoryPolicies", "HardwareConfigurations", "RoleDefinitions", "ScopeTags", "Notifications",
    "AndroidOEMConfig", "AppleEnrollmentTypes", "AssignmentFilters", "DeviceHealthScripts", "ADMXFiles",
    "ReusableSettings", "AuthenticationStrengths", "AuthenticationContext", "W365ProvisioningPolicies",
    "W365UserSettings", "DriverUpdateProfiles", "DeviceCategories",
    "BaselineTemplates", "BaselineTemplatesSettingsCatalog", "AndroidGooglePlay", "AndroidEnrolmentProfiles",
    "AppleVPPTokens", "AppleEnrollmentTokens", "TenantSettings",
]

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")


class Export:
    def __init__(self, folder, result):
        self.folder = folder
        self.result = result
        self.log = json.loads((folder / "log.json").read_text())
        zips = list((folder / "output").glob("IntuneExport-*.zip"))
        self.zip = zipfile.ZipFile(zips[0]) if zips else None
        self.root = zips[0].stem if zips else None

    def names(self):
        return sorted(name.removeprefix(self.root + "/") for name in self.zip.namelist() if not name.endswith("/"))

    def raw(self, name):
        return self.zip.read(f"{self.root}/{name}")

    def json(self, name):
        return json.loads(self.raw(name).decode("utf-8-sig"))

    def urls(self):
        return [request["url"] for request in self.log["requests"]]


def run_export(tmp_path, config=None, fixtures=None, env=None, tenant=None):
    fixtures = graph_fixtures.build() if fixtures is None else fixtures
    (tmp_path / "output").mkdir()
    (tmp_path / "fixtures.json").write_text(json.dumps(fixtures))
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(HARNESS),
         "-Script", str(SCRIPT), "-Fixtures", str(tmp_path / "fixtures.json"),
         "-OutputDir", str(tmp_path / "output"), "-Log", str(tmp_path / "log.json"),
         "-ConfigJson", json.dumps(config or {}), *(["-TenantId", tenant] if tenant else [])],
        capture_output=True, text=True, timeout=300, env={**os.environ, **(env or {})})
    export = Export(tmp_path, result)
    assert export.log["unknown"] == [], result.stdout + result.stderr
    return export


def pwsh_eval(command):
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", f". '{SCRIPT}'; {command}"],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


@needs_pwsh
def test_generic_types_export_one_file_per_object_into_the_company_and_type_folders(tmp_path):
    export = run_export(tmp_path, {"object_types": GENERIC})

    assert export.result.returncode == 0, export.result.stdout + export.result.stderr
    assert export.names() == [
        "Contoso/AutoPilot/Contoso Autopilot.json",
        "Contoso/AzureBranding/0.json",
        "Contoso/ComplianceScripts/ContosoCheck Disk.json",
        "Contoso/DeviceCategories/Contoso Laptops.json",
        "Contoso/Groups/Contoso Sales.json",
        "Contoso/MigrationTable.json",
        "Contoso/NamedLocations/Contoso Countries.json",
        "Contoso/NamedLocations/Contoso Office.json",
        "Contoso/TenantSettings/Tenant Settings.json",
    ]
    assert export.json("Contoso/AutoPilot/Contoso Autopilot.json")["assignments"][0]["target"]["groupId"] == gid(901)
    assert "Autopilot: 1 objects" in export.result.stdout
    assert "Named Locations: 2 objects" in export.result.stdout
    assert f"Wrote {export.root}.zip with 7 objects." in export.result.stdout
    assert not (tmp_path / "output" / "intune-export-work").exists()


@needs_pwsh
def test_json_files_have_a_bom_and_replace_the_tenant_id(tmp_path):
    export = run_export(tmp_path, {"object_types": GENERIC})

    for name in export.names():
        raw = export.raw(name)
        assert raw.startswith(b"\xef\xbb\xbf"), name
        if name != "Contoso/MigrationTable.json":
            assert TENANT.encode() not in raw, name
    assert "%OrganizationId%" in export.json("Contoso/AzureBranding/0.json")["@odata.context"]


@needs_pwsh
def test_the_migration_table_lists_each_assigned_group_once_with_the_real_tenant(tmp_path):
    export = run_export(tmp_path, {"object_types": GENERIC})

    assert export.json("Contoso/MigrationTable.json") == {
        "TenantId": TENANT,
        "Organization": "Contoso",
        "Objects": [{"Id": gid(901), "DisplayName": "Contoso Sales", "Type": "Group"}],
    }
    assert export.json("Contoso/Groups/Contoso Sales.json")["id"] == gid(901)


@needs_pwsh
def test_requests_send_the_odata_metadata_accept_header_and_follow_pages(tmp_path):
    export = run_export(tmp_path, {"object_types": GENERIC})

    requests = {request["url"]: request for request in export.log["requests"]}
    assert requests["https://graph.microsoft.com/beta/organization"]["accept"] is None
    assert requests["https://graph.microsoft.com/beta/deviceManagement/deviceCategories?$top=500"]["accept"] == (
        "application/json;odata.metadata=full")
    assert requests[f"https://graph.microsoft.com/beta/groups/{gid(901)}"]["accept"] == "application/json;odata.metadata=none"
    assert "https://graph.microsoft.com/beta/identity/conditionalAccess/namedLocations?$skiptoken=page2" in requests


@needs_pwsh
def test_the_registry_holds_every_intune_object_type_in_order():
    ids = json.loads(pwsh_eval("$IntuneTypes.Id | ConvertTo-Json"))

    assert ids == REGISTRY


@pytest.fixture(scope="module")
def full_export(tmp_path_factory):
    return run_export(tmp_path_factory.mktemp("full"), {"object_types": REGISTRY, "use_batch_api": False})


def decoded(export, name):
    return export.raw(name).decode()


@needs_pwsh
def test_every_type_exports_its_objects(full_export):
    assert full_export.result.returncode == 0, full_export.result.stdout + full_export.result.stderr
    folders = {name.split("/")[1] for name in full_export.names() if name.count("/") == 2}
    assert set(REGISTRY) <= folders
    assert "Wrote " in full_export.result.stdout


@needs_pwsh
def test_by_default_only_the_intune_management_bulk_export_types_export(tmp_path):
    export = run_export(tmp_path)

    folders = {name.split("/")[1] for name in export.names() if name.count("/") == 2} - {"Groups"}
    assert folders == set(REGISTRY[:46])


@needs_pwsh
def test_the_default_batch_api_writes_the_same_zip_as_one_request_per_object(full_export, tmp_path):
    batched = run_export(tmp_path, {"object_types": REGISTRY})

    assert any(r["batch"] for r in batched.log["requests"])
    assert {n: batched.raw(n) for n in batched.names()} == {n: full_export.raw(n) for n in full_export.names()}


@needs_pwsh
def test_device_configuration_decrypts_oma_settings_and_resolves_references(full_export):
    oma = full_export.json("Contoso/DeviceConfiguration/Contoso OMA.json")["omaSettings"]
    assert [(s["isEncrypted"], s["secretReferenceValueId"], s["value"]) for s in oma] == [
        (False, None, "proxy.contoso.com"),
        (False, None, "PHByb2ZpbGU+Y29udG9zbzwvcHJvZmlsZT4="),
        (False, None, 1),
    ]
    wifi = full_export.json("Contoso/DeviceConfiguration/Contoso WiFi.json")
    assert wifi["#CustomRef_rootCertificateForServerValidation"] == (
        "Contoso Root CA|:|#microsoft.graph.androidWorkProfileTrustedRootCertificate")
    assert "#CustomRef_assignments" not in wifi
    assert ("https://graph.microsoft.com/beta/deviceManagement/deviceConfigurations/"
            f"{gid(104)}?$expand=assignments,microsoft.graph.windows10GeneralConfiguration/privacyAccessControls") in full_export.urls()


@needs_pwsh
def test_the_migration_table_collects_assignment_ca_compliance_and_role_groups(full_export):
    table = full_export.json("Contoso/MigrationTable.json")
    assert table["TenantId"] == TENANT
    assert [(o["Id"], o["Type"]) for o in table["Objects"]] == [
        (gid(901), "Group"), (gid(951), "User"), (gid(903), "Group"), (gid(905), "Group"),
        (gid(906), "Group"), (gid(907), "Group"), (gid(908), "Group"), (gid(909), "Group")]
    assert "Contoso/Groups/Contoso Settings Users.json" in full_export.names()
    assert f"No Group found with ID {gid(902)}. It might be deleted." in full_export.result.stdout + full_export.result.stderr
    assert full_export.urls().count(f"https://graph.microsoft.com/beta/groups/{gid(902)}") == 1


@needs_pwsh
def test_endpoint_security_writes_its_settings_as_a_json_string(full_export):
    text = full_export.json("Contoso/EndpointSecurity/Contoso Firewall_Settings.json")
    assert json.loads(text)["settings"][0]["definitionId"] == "firewallEnabled"


@needs_pwsh
def test_administrative_templates_write_definition_values(full_export):
    custom, builtin = full_export.json("Contoso/AdministrativeTemplates/Contoso Edge_Settings.json")
    assert builtin == {
        "enabled": True,
        "definition@odata.bind": f"https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions('{gid(1407)}')",
    }
    assert custom == {
        "enabled": True,
        "definition@odata.bind": f"https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions('{gid(1403)}')",
        "#Definition_Id": gid(1403),
        "#Definition_displayName": "Home page",
        "#Definition_classType": "user",
        "#Definition_categoryPath": "\\Contoso\\Edge",
        "presentationValues": [{
            "@odata.type": "#microsoft.graph.groupPolicyPresentationValueText",
            "value": "https://contoso.com",
            "presentation@odata.bind": (f"https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions('{gid(1403)}')"
                                        f"/presentations('{gid(1405)}')"),
            "#Presentation_Id": gid(1405),
            "#Presentation_Label": "Home page URL",
        }],
    }
    assert full_export.json("Contoso/AdministrativeTemplates/Contoso Edge.json")["policyConfigurationIngestionType"] == "custom"
    request = next(r for r in full_export.log["requests"] if r["url"].endswith("definitionValues?$expand=definition"))
    assert request["accept"] is None


@needs_pwsh
def test_binary_files_are_written_next_to_their_json(full_export):
    assert decoded(full_export, "Contoso/IntuneBranding/Contoso Branding_themeColorLogo.jpg") == "JPEG Contoso logo"
    assert "Contoso/IntuneBranding/Contoso Branding_lightBackgroundLogo.jpg" not in full_export.names()
    assert decoded(full_export, "Contoso/TermsOfUse/Contoso Terms.pdf") == "%PDF Contoso terms"
    assert decoded(full_export, "Contoso/PowerShellScripts/contoso.ps1") == "echo Contoso Script"
    assert decoded(full_export, "Contoso/MacScripts/contoso.sh") == "echo Contoso Shell"
    assert decoded(full_export, "Contoso/MacCustomAttributes/attribute.sh") == "echo Contoso Attribute"
    assert decoded(full_export, "Contoso/DeviceHealthScripts/Contoso Remediation_DetectionScript.ps1") == "exit 1"
    assert decoded(full_export, "Contoso/DeviceHealthScripts/Contoso Remediation_RemediationScript.ps1") == "exit 0"
    assert decoded(full_export, "Contoso/HardwareConfigurations/bios.bin") == "BIOS Contoso"


@needs_pwsh
def test_applications_keep_relationships_and_write_their_scripts(full_export):
    agent = full_export.json("Contoso/Applications/Contoso Agent.json")
    assert agent["#CustomRefDependency"] == f"Contoso Runtime|!|2.0|!|{gid(2204)}|!|autoInstall"
    assert agent["activeInstallScript"]["#ScriptInfo"]["displayName"] == "install.ps1"
    assert decoded(full_export, "Contoso/Applications/Contoso Agent_DetectionScript.ps1") == "Test-Path C:\\Contoso"
    assert decoded(full_export, "Contoso/Applications/Contoso Agent_RequirementScript.ps1") == "$true"
    assert decoded(full_export, "Contoso/Applications/Contoso Agent_install.ps1") == "Install-Contoso"
    assert "Contoso/Applications/agent.intunewin" not in full_export.names()
    request = next(r for r in full_export.log["requests"] if "mobileApps?$filter" in r["url"])
    assert request["accept"] == "application/json;odata.metadata=minimal"


@needs_pwsh
def test_role_definitions_replace_role_assignments(full_export):
    role = full_export.json("Contoso/RoleDefinitions/Contoso Helpdesk.json")
    assert role["roleAssignments"][0]["resourceScopes"] == [gid(906)]
    assert role["roleAssignments"][0]["members"] == [gid(907)]


@needs_pwsh
def test_assignments_are_added_for_types_that_do_not_expand_them(full_export):
    tag = full_export.json("Contoso/ScopeTags/Contoso Tag.json")
    assert tag["assignments"][0]["target"]["groupId"] == gid(908)
    terms = full_export.json("Contoso/TermsAndConditions/Contoso Conditions.json")
    assert terms["assignments"][0]["target"]["groupId"] == gid(904)
    settings = full_export.json("Contoso/SettingsCatalog/Contoso Settings.json")
    assert settings["assignments"][0]["target"]["groupId"] == gid(905)
    assert f"https://graph.microsoft.com/beta/deviceManagement/termsAndConditions/{gid(1801)}" in full_export.urls()


@needs_pwsh
def test_enrollment_default_policies_are_saved_under_their_id_suffix(full_export):
    names = full_export.names()
    assert [n for n in names if "/EnrollmentStatusPage/" in n] == [
        "Contoso/EnrollmentStatusPage/Contoso ESP.json",
        "Contoso/EnrollmentStatusPage/DefaultWindows10EnrollmentCompletionPageConfiguration.json"]
    assert [n for n in names if "/EnrollmentRestrictions/" in n] == [
        "Contoso/EnrollmentRestrictions/Contoso Device Limit.json",
        "Contoso/EnrollmentRestrictions/DefaultPlatformRestrictions.json"]
    assert [n for n in names if "/CoManagementSettings/" in n] == ["Contoso/CoManagementSettings/Contoso Co-management.json"]


@needs_pwsh
def test_app_configurations_name_their_targeted_apps(full_export):
    expected = f"Contoso Outlook|!|{gid(2201)}|!|#microsoft.graph.iosStoreApp"
    assert full_export.json("Contoso/AppConfigurationManagedDevice/Contoso Outlook Config.json")["#CustomRefTargetedApps"] == expected
    assert full_export.json("Contoso/AndroidOEMConfig/Contoso OEM Config.json")["#CustomRefTargetedApps"] == expected


@needs_pwsh
def test_app_protection_reads_each_policy_from_its_own_collection(full_export):
    assert {n for n in full_export.names() if "/AppProtection/" in n} == {
        "Contoso/AppProtection/Contoso iOS MAM.json", "Contoso/AppProtection/Contoso WIP.json"}
    urls = full_export.urls()
    assert f"https://graph.microsoft.com/beta/deviceAppManagement/iosManagedAppProtections/{gid(1901)}?$expand=assignments,apps" in urls
    assert (f"https://graph.microsoft.com/beta/deviceAppManagement/windowsInformationProtectionPolicies/{gid(1903)}"
            "?$expand=protectedAppLockerFiles,exemptAppLockerFiles,assignments") in urls
    assert urls.count("https://graph.microsoft.com/beta/$metadata") == 1


@needs_pwsh
def test_reusable_settings_and_tenant_settings(full_export):
    reusable = full_export.json("Contoso/ReusableSettings/Contoso Reusable.json")
    assert reusable["settingInstance"]["settingDefinitionId"] == "contoso_setting"
    assert full_export.json("Contoso/TenantSettings/Tenant Settings.json")["deviceComplianceCheckinThresholdDays"] == 30


@needs_pwsh
def test_a_type_that_fails_is_reported_after_the_zip_exists(tmp_path):
    fixtures = graph_fixtures.build()
    fixtures["GET deviceManagement/deviceCategories?$top=500"] = graph_fixtures.error(403, "Missing scope permissions.")

    export = run_export(tmp_path, {"object_types": ["AutoPilot", "DeviceCategories"]}, fixtures)

    assert export.result.returncode == 1
    assert export.names() == ["Contoso/AutoPilot/Contoso Autopilot.json", "Contoso/Groups/Contoso Sales.json",
                              "Contoso/MigrationTable.json"]
    assert "These failed: Device Categories (403)." in export.log["failure"]
    assert "add it to the app in Entra and grant admin consent" in export.log["failure"]
    assert "object_types" not in export.log["failure"]
    assert "Missing scope permissions." in export.result.stdout + export.result.stderr


@needs_pwsh
def test_a_failed_decryption_keeps_the_object_and_fails_the_run(tmp_path):
    fixtures = graph_fixtures.build()
    api = "deviceManagement/deviceConfigurations"
    fixtures[f"GET {api}/{gid(101)}/getOmaSettingPlainTextValue(secretReferenceValueId='secret-1')"] = graph_fixtures.error(
        403, "Forbidden.")

    export = run_export(tmp_path, {"object_types": ["DeviceConfiguration"]}, fixtures)

    assert export.result.returncode == 1
    proxy, profile, _ = export.json("Contoso/DeviceConfiguration/Contoso OMA.json")["omaSettings"]
    assert (proxy["isEncrypted"], proxy["value"]) == (True, "****")
    assert profile["isEncrypted"] is False
    assert f"https://graph.microsoft.com/beta/groups/{gid(902)}" in export.urls()
    assert "Device Configuration: Contoso OMA (403)" in export.log["failure"]


@needs_pwsh
def test_a_missing_targeted_app_keeps_the_object_and_fails_the_run(tmp_path):
    fixtures = graph_fixtures.build()
    fixtures[f"GET deviceAppManagement/mobileApps/{gid(2201)}"] = graph_fixtures.error(404, "Not found.")

    export = run_export(tmp_path, {"object_types": ["AppConfigurationManagedDevice"]}, fixtures)

    assert export.result.returncode == 1
    config = export.json("Contoso/AppConfigurationManagedDevice/Contoso Outlook Config.json")
    assert "#CustomRefTargetedApps" not in config
    assert config["targetedMobileApps"] == [gid(2201)]
    assert "App Configuration (Device): Contoso Outlook Config (404)" in export.log["failure"]


@needs_pwsh
def test_throttled_requests_wait_and_retry(tmp_path):
    fixtures = graph_fixtures.build()
    key = "GET deviceManagement/windowsAutopilotDeploymentProfiles"
    fixtures[key] = [{"status": 429, "headers": {"Retry-After": 7}, "body": {"error": {"message": "Too many requests"}}},
                     {"status": 503, "body": {"error": {"message": "Unavailable"}}}, fixtures[key]]

    export = run_export(tmp_path, {"object_types": ["AutoPilot"]}, fixtures)

    assert export.result.returncode == 0, export.result.stdout + export.result.stderr
    assert export.log["sleeps"] == [7, 5]
    assert "Contoso/AutoPilot/Contoso Autopilot.json" in export.names()


@needs_pwsh
def test_add_id_to_file_name_adds_the_id_except_where_the_type_skips_it(tmp_path):
    export = run_export(tmp_path, {"object_types": ["AutoPilot", "AzureBranding", "EnrollmentStatusPage", "DeviceHealthScripts"],
                                   "add_id_to_file_name": "TRUE"})

    names = export.names()
    assert f"Contoso/AutoPilot/Contoso Autopilot_{gid(2301)}.json" in names
    assert "Contoso/AzureBranding/0.json" in names
    assert f"Contoso/EnrollmentStatusPage/Contoso ESP_{gid(1102)}.json" in names
    assert "Contoso/EnrollmentStatusPage/DefaultWindows10EnrollmentCompletionPageConfiguration.json" in names
    assert not [n for n in names if "All users and all devices" in n]
    assert f"Contoso/DeviceHealthScripts/Contoso Remediation_{gid(3801)}_DetectionScript.ps1" in names


@needs_pwsh
def test_export_assignments_false_drops_assignments_and_their_groups(tmp_path):
    export = run_export(tmp_path, {"object_types": ["AutoPilot", "ScopeTags", "ConditionalAccess"],
                                   "export_assignments": False})

    assert "assignments" not in export.json("Contoso/AutoPilot/Contoso Autopilot.json")
    assert "assignments" not in export.json("Contoso/ScopeTags/Contoso Tag.json")
    assert [o["Id"] for o in export.json("Contoso/MigrationTable.json")["Objects"]] == [gid(901), gid(951)]
    assert f"https://graph.microsoft.com/beta/deviceManagement/roleScopeTags/{gid(3301)}/assignments" in export.urls()


@needs_pwsh
def test_without_company_and_type_folders_files_land_in_the_export_root(tmp_path):
    export = run_export(tmp_path, {"object_types": "AutoPilot, NamedLocations", "add_company_name": "false",
                                   "add_object_type": False})

    assert export.names() == ["Contoso Autopilot.json", "Contoso Countries.json", "Contoso Office.json",
                              "Groups/Contoso Sales.json", "MigrationTable.json"]


@needs_pwsh
def test_sort_json_properties_orders_annotations_before_their_property(tmp_path):
    export = run_export(tmp_path, {"object_types": ["DeviceConfiguration"], "sort_json_properties": True})

    wifi = export.json("Contoso/DeviceConfiguration/Contoso WiFi.json")
    assert list(wifi) == [
        "@odata.context", "@odata.type", "assignments@odata.navigationLink", "assignments",
        "rootCertificateForServerValidation@odata.associationLink", "rootCertificateForServerValidation@odata.navigationLink",
        "displayName", "id",
        "#CustomRef_rootCertificateForServerValidation"]


@needs_pwsh
def test_the_batch_api_reads_full_objects_in_batch_requests_and_retries_throttled_ones(tmp_path):
    fixtures = graph_fixtures.build()
    key = f"GET deviceManagement/windowsAutopilotDeploymentProfiles/{gid(2301)}?$expand=assignments"
    fixtures[key] = [{"status": 429, "headers": {"Retry-After": "2"}}, fixtures[key]]

    export = run_export(tmp_path, {"object_types": ["AutoPilot", "Applications", "NamedLocations"], "use_batch_api": True},
                        fixtures)

    assert export.result.returncode == 0, export.result.stdout + export.result.stderr
    batched = [r for r in export.log["requests"] if r["batch"]]
    assert {r["url"]: r["accept"] for r in batched}[f"/deviceAppManagement/mobileApps/{gid(2202)}?$expand=categories,assignments"] == (
        "application/json;odata.metadata=minimal")
    assert [r["url"] for r in batched].count(f"/deviceManagement/windowsAutopilotDeploymentProfiles/{gid(2301)}?$expand=assignments") == 2
    assert export.log["sleeps"] == [5]
    assert export.json("Contoso/AutoPilot/Contoso Autopilot.json")["assignments"][0]["target"]["groupId"] == gid(901)
    assert "Contoso/Applications/Contoso Agent_install.ps1" in export.names()
    assert "Contoso/NamedLocations/Contoso Countries.json" in export.names()


@needs_pwsh
def test_graph_version_v1_sends_relative_urls_to_v1(tmp_path):
    export = run_export(tmp_path, {"object_types": ["AppProtection"], "graph_version": "v1.0"})

    urls = [url for url in export.urls() if url.startswith("https://graph.microsoft.com/")]
    assert "https://graph.microsoft.com/beta/$metadata" in urls
    assert all(url.startswith("https://graph.microsoft.com/v1.0/") for url in urls if "$metadata" not in url)
    assert "Contoso/AppProtection/Contoso iOS MAM.json" in export.names()


@needs_pwsh
def test_name_filter_and_file_switches(tmp_path):
    config = {"object_types": ["PowerShellScripts", "HardwareConfigurations", "DeviceConfiguration"], "name_filter": "contoso (",
              "export_script_content": False, "export_policy_files": False, "resolve_reference_info": False}
    assert run_export(tmp_path, config).names() == []

    (tmp_path / "second").mkdir()
    export = run_export(tmp_path / "second", {**config, "name_filter": "CONTOSO"})
    names = export.names()
    assert "Contoso/PowerShellScripts/Contoso Script.json" in names
    assert "Contoso/PowerShellScripts/contoso.ps1" not in names
    assert "Contoso/HardwareConfigurations/bios.bin" not in names
    assert "#CustomRef_rootCertificateForServerValidation" not in export.json("Contoso/DeviceConfiguration/Contoso WiFi.json")
    assert "https://graph.microsoft.com/beta/$metadata" not in export.urls()


KEYS = {f"Contoso Agent_{gid(2202)}_1": {"fileEncryptionInfo": {
    "encryptionKey": graph_fixtures.PACKAGE_KEY, "initializationVector": graph_fixtures.PACKAGE_IV}}}


@needs_pwsh
def test_app_files_are_decrypted_with_an_intunemanagement_encryption_key(tmp_path):
    export = run_export(tmp_path, {"object_types": ["Applications"], "export_app_files": True},
                        env={"INTUNE_APP_ENCRYPTION_KEYS": json.dumps(KEYS)})

    assert export.raw("Contoso/Applications/agent.intunewin") == b"Contoso Agent package"
    assert not [n for n in export.names() if n.endswith(".encrypted")]
    assert graph_fixtures.PACKAGE_URL in export.urls()


@needs_pwsh
def test_app_files_without_an_encryption_key_are_skipped(tmp_path):
    export = run_export(tmp_path, {"object_types": ["Applications"], "export_app_files": True},
                        env={"INTUNE_APP_ENCRYPTION_KEYS": ""})

    assert "Contoso/Applications/agent.intunewin" not in export.names()
    assert "No encryption key in INTUNE_APP_ENCRYPTION_KEYS for app Contoso Agent" in export.result.stdout
    assert graph_fixtures.PACKAGE_URL not in export.urls()


@needs_pwsh
def test_a_package_that_cannot_be_decrypted_leaves_no_partial_file_and_fails_the_run(tmp_path):
    keys = {name: {"fileEncryptionInfo": {**info["fileEncryptionInfo"], "encryptionKey": "AAAA"}} for name, info in KEYS.items()}

    export = run_export(tmp_path, {"object_types": ["Applications"], "export_app_files": True},
                        env={"INTUNE_APP_ENCRYPTION_KEYS": json.dumps(keys)})

    assert export.result.returncode == 1
    assert not [n for n in export.names() if n.endswith((".intunewin", ".encrypted"))]
    assert "Applications: Contoso Agent (error)" in export.log["failure"]


@needs_pwsh
def test_decrypt_oma_settings_false_keeps_the_values_encrypted(tmp_path):
    export = run_export(tmp_path, {"object_types": ["DeviceConfiguration"], "decrypt_oma_settings": False})

    proxy, profile, _ = export.json("Contoso/DeviceConfiguration/Contoso OMA.json")["omaSettings"]
    assert proxy["isEncrypted"] and profile["isEncrypted"]
    assert not [url for url in export.urls() if "getOmaSettingPlainTextValue" in url]


@needs_pwsh
def test_a_tenant_domain_name_still_replaces_the_tenant_id_from_the_token(tmp_path):
    fixtures = graph_fixtures.build()
    fixtures["GET organization"] = graph_fixtures.error(403, "Forbidden.")

    export = run_export(tmp_path, {"object_types": ["AzureBranding"]}, fixtures, tenant="contoso.onmicrosoft.com")

    assert f"https://graph.microsoft.com/beta/organization/{TENANT}/branding/localizations" in export.urls()
    assert all(r["timeout"] == 120 for r in export.log["requests"] if not r["batch"])


@needs_pwsh
def test_a_request_with_no_answer_is_retried(tmp_path):
    fixtures = graph_fixtures.build()
    key = "GET deviceManagement/windowsAutopilotDeploymentProfiles"
    fixtures[key] = [{"status": 0}, fixtures[key]]

    export = run_export(tmp_path, {"object_types": ["AutoPilot"]}, fixtures)

    assert export.result.returncode == 0, export.result.stdout + export.result.stderr
    assert export.log["sleeps"] == [5]


@needs_pwsh
def test_two_objects_whose_names_differ_only_in_case_get_a_warning(tmp_path):
    fixtures = graph_fixtures.build()
    key = "GET deviceManagement/windowsAutopilotDeploymentProfiles"
    listing = fixtures[key]["body"]
    twin = {**listing["value"][0], "id": gid(2399), "displayName": "CONTOSO AUTOPILOT"}
    listing["value"].append(twin)
    detail = fixtures[f"{key}/{gid(2301)}?$expand=assignments"]
    fixtures[f"{key}/{gid(2399)}?$expand=assignments"] = {**detail, "body": {**detail["body"], **twin}}

    export = run_export(tmp_path, {"object_types": ["AutoPilot"]}, fixtures)

    assert "Autopilot: CONTOSO AUTOPILOT has the same file name as another object" in export.result.stdout


@needs_pwsh
def test_a_policy_file_with_an_empty_file_name_is_written_as_dot_json(tmp_path):
    fixtures = graph_fixtures.build()
    for key, value in fixtures.items():
        if key.startswith("GET deviceManagement/hardwareConfigurations/") and isinstance(value, dict):
            value["body"]["fileName"] = ""

    export = run_export(tmp_path, {"object_types": ["HardwareConfigurations"]}, fixtures)

    assert "Contoso/HardwareConfigurations/.json" in export.names()


@needs_pwsh
@pytest.mark.parametrize(("config", "message"), [
    ({"object_types": ["Foo"]}, 'config.yml: object_types lists "Foo", which is not an Intune object type. Use one of: DeviceConfiguration,'),
    ({"export_assignments": "yes"}, 'config.yml: export_assignments is "yes", which is not true or false.'),
    ({"graph_version": "v2"}, 'config.yml: graph_version is "v2". Use beta or v1.0.'),
])
def test_a_wrong_config_value_names_config_yml_and_the_key(config, message):
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command",
         f". '{SCRIPT}'; try {{ Get-ExportConfig '{json.dumps(config)}' }} catch {{ $_.Exception.Message }}"],
        capture_output=True, text=True, timeout=120)
    assert result.stdout.startswith(message)


@needs_pwsh
def test_an_env_override_of_a_config_key_reaches_the_script_as_a_string(project, monkeypatch):
    from pdt import config, scaffold

    assert scaffold.new_app("intune-backup", "intune-export") == 0
    monkeypatch.setenv("PDT_INTUNE_BACKUP_USE_BATCH_API", "false")
    monkeypatch.setenv("PDT_INTUNE_BACKUP_OBJECT_TYPES", "AutoPilot, NamedLocations")
    merged = json.dumps(config.merged_app("intune-backup")["config"], default=str).replace("'", "''")

    parsed = json.loads(pwsh_eval(f"Get-ExportConfig '{merged}' | ConvertTo-Json"))

    assert parsed["use_batch_api"] is False
    assert parsed["object_types"] == ["AutoPilot", "NamedLocations"]


@needs_pwsh
def test_config_accepts_strings_for_booleans_and_lists():
    config = json.loads(pwsh_eval(
        "Get-ExportConfig '{\"object_types\": \"AutoPilot, NamedLocations\", \"use_batch_api\": \"True\", "
        "\"add_company_name\": \"false\", \"name_filter\": null}' | ConvertTo-Json"))
    assert config["object_types"] == ["AutoPilot", "NamedLocations"]
    assert config["use_batch_api"] is True
    assert config["add_company_name"] is False
    assert config["name_filter"] == ""
    assert json.loads(pwsh_eval("Get-ExportConfig '' | ConvertTo-Json"))["graph_version"] == "beta"


@needs_pwsh
def test_file_names_lose_the_characters_windows_rejects():
    name = pwsh_eval("Remove-InvalidFileNameChars ('a<b>c:d\"e/f\\g|h?i*j' + [char]1 + [char]31 + 'k.json')")
    assert name.strip() == "abcdefghijk.json"


@needs_pwsh
def test_sorted_json_puts_at_props_then_annotated_groups_then_names_then_hash_props():
    output = pwsh_eval(
        "ConvertTo-JsonSortedGraph ([pscustomobject][ordered]@{ zeta = 1; '#CustomRef_b' = 'r'; 'assignments' = @(1);"
        " 'assignments@odata.context' = 'c'; '@odata.type' = 't'; 'settings@odata.navigationLink' = 'l'; settings = $null;"
        " alpha = $null; list = @(@{ b = 2; a = 1 }) }) -Depth 5")
    value = json.loads(output)
    assert list(value) == ["@odata.type", "assignments@odata.context", "assignments", "settings@odata.navigationLink",
                           "alpha", "list", "zeta", "#CustomRef_b"]
    assert value["assignments"] == [1]
    assert value["alpha"] is None
    assert list(value["list"][0]) == ["a", "b"]


@needs_pwsh
def test_without_organization_read_the_export_uses_the_tenant_id_and_no_company_folder(tmp_path):
    fixtures = graph_fixtures.build()
    fixtures["GET organization"] = graph_fixtures.error(403, "Insufficient privileges.")

    export = run_export(tmp_path, {"object_types": ["AutoPilot"]}, fixtures)

    assert export.result.returncode == 0, export.result.stdout + export.result.stderr
    assert export.names() == ["AutoPilot/Contoso Autopilot.json", "Groups/Contoso Sales.json", "MigrationTable.json"]
    assert export.json("MigrationTable.json")["TenantId"] == TENANT
    assert export.json("MigrationTable.json")["Organization"] == ""
    assert "Organization.Read.All" in export.result.stdout + export.result.stderr


def test_the_copied_example_validates(project):
    from pdt import config, scaffold

    assert scaffold.new_app("intune-backup", "intune-export") == 0
    assert config.validate_app("intune-backup") == []
    assert config.merged_app("intune-backup")["schedule"] == "weekly"


@needs_pwsh
@pytest.mark.parametrize("provider", ["aws", "azure", "google-cloud", "windows"])
def test_the_scan_finds_one_entry_script_no_module_and_no_problem_on_every_provider(
        project, monkeypatch, tmp_path, provider):
    from pdt import config, powershell, scaffold

    (tmp_path / "scan").mkdir()
    monkeypatch.setenv("PDT_SCAN_DIR", str(tmp_path / "scan"))
    assert scaffold.new_app("intune-backup", "intune-export") == 0
    scan = powershell.scan(config.merged_app("intune-backup"), provider)

    assert scan.entries == ["IntuneExport.ps1"]
    assert scan.modules == []
    assert powershell.report(scan)[0] == []
