"""The intune-export example: `IntuneExport.ps1` runs through tests/intune_export/harness.ps1
against IntuneManagement's own offline mock tenant, and tests/intune_export/import.ps1 imports
the zip it writes with IntuneManagement's own bulk import.

IntuneManagement is the copy the script downloads to pdt's data folder, so these tests need
no network; they skip until one run of the app (or of the script) has downloaded it."""

import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID

from pdt import config

SCRIPT = Path(__file__).resolve().parent.parent / "src/pdt/examples/intune-export/IntuneExport.ps1"
TESTS = Path(__file__).resolve().parent / "intune_export"
COMMIT = re.search(r"\$commit = '(\w+)'", SCRIPT.read_text())[1]
MODULE = config.data_home() / "pdt" / f"IntuneManagement-{COMMIT}"
GROUP = "aaaaaaaa-0000-0000-0000-000000000001"
ENV = {"PDT_AZURE_TENANT_ID": "tenant-id", "PDT_AZURE_CLIENT_ID": "client-id", "PDT_AZURE_CLIENT_SECRET": "secret"}

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")
needs_module = pytest.mark.skipif(not (MODULE / "IntuneManagement.psd1").is_file(),
                                  reason=f"needs IntuneManagement in {MODULE}")


def pwsh(args, env, cwd):
    return subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", *args],
                          capture_output=True, text=True, timeout=600, cwd=cwd,
                          env={**os.environ, "IM_MOCK_DATA": "", "NO_COLOR": "1", **env})


def run_export(tmp_path, settings=None, env=None, forbidden=None):
    (tmp_path / "data/pdt").mkdir(parents=True)
    (tmp_path / "data/pdt" / MODULE.name).symlink_to(MODULE)
    (tmp_path / "output").mkdir()
    result = pwsh(["-File", str(TESTS / "harness.ps1"), "-Script", str(SCRIPT), "-Log", str(tmp_path / "log.json"),
                   *(["-Forbidden", forbidden] if forbidden else [])],
                  {"XDG_DATA_HOME": str(tmp_path / "data"), "IM_MOCK_DATA": str(TESTS / "tenant"),
                   "PDT_OUTPUT_DIR": str(tmp_path / "output"), "PDT_PROJECT": str(tmp_path),
                   "PDT_CONFIG_JSON": json.dumps(settings or {}), **ENV, **(env or {})}, tmp_path)
    zips = list((tmp_path / "output").glob("IntuneExport-*.zip"))
    return result, zipfile.ZipFile(zips[0]) if zips else None


def output(result):
    return " ".join((result.stdout + result.stderr).replace("|", " ").split())


def files(archive):
    root = f"IntuneExport-{datetime.date.today():%Y-%m-%d}/"
    assert all(name.startswith(root) for name in archive.namelist())
    return sorted(name.removeprefix(root) for name in archive.namelist() if not name.endswith("/"))


def import_into_new_tenant(tmp_path, archive):
    tenant = tmp_path / "target"
    for path in ["groups", "organization", *(f"deviceManagement/{name}" for name in
                 ["deviceConfigurations", "deviceCompliancePolicies", "configurationPolicies", "deviceManagementScripts"])]:
        (tenant / "graph" / path).parent.mkdir(parents=True, exist_ok=True)
        (tenant / "graph" / f"{path}.json").write_text('{"value": []}')
    (tenant / "graph/organization.json").write_text(
        '{"value": [{"id": "99999999-0000-0000-0000-000000000000", "displayName": "Fabrikam"}]}')
    (tenant / "tenant.json").write_text('{"TenantId": "99999999-0000-0000-0000-000000000000", "TenantName": "Fabrikam"}')
    archive.extractall(tmp_path / "unzipped")
    result = pwsh(["-File", str(TESTS / "import.ps1"), "-Module", str(MODULE / "IntuneManagement.psd1"),
                   "-ImportFolder", str(next((tmp_path / "unzipped").glob("*/Contoso"))),
                   "-Report", str(tmp_path / "import.json")], {"IM_MOCK_DATA": str(tenant)}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / "import.json").read_text(encoding="utf-8-sig"))


@needs_pwsh
@needs_module
def test_export_imports_into_another_tenant(tmp_path):
    result, archive = run_export(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "log.json").read_text(encoding="utf-8-sig"))["signIns"] == [
        {"tenant": "tenant-id", "app": "client-id", "secret": "secret", "certificate": None}]
    assert files(archive) == [
        "Contoso/AndroidGooglePlay/Managed Google Play.json",
        "Contoso/CompliancePolicies/Win10 Compliance_cmp-1.json",
        "Contoso/DeviceConfiguration/Win10 Baseline_dc-1.json",
        "Contoso/Groups/Pilot Devices.json",
        "Contoso/MigrationTable.json",
        "Contoso/PowerShellScripts/Set Time Zone_ps-1.json",
        "Contoso/SettingsCatalog/Edge Settings_cp-1.json",
        "Contoso/TenantSettings/Intune Tenant Settings.json",
    ]
    migration = json.loads(archive.read(f"IntuneExport-{datetime.date.today():%Y-%m-%d}/Contoso/MigrationTable.json")
                           .decode("utf-8-sig"))
    assert migration["Objects"] == [{"Id": GROUP, "DisplayName": "Pilot Devices", "Type": "Group"}]

    report = import_into_new_tenant(tmp_path, archive)
    assert report["result"]["Imported"] == 4
    objects = report["objects"]
    assert [group["name"] for group in objects["groups"]] == ["Pilot Devices"]
    assigned = {policy["name"]: [a["target"]["groupId"] for a in policy["assignments"]]
                for path in ["deviceManagement/deviceConfigurations", "deviceManagement/deviceCompliancePolicies"]
                for policy in objects[path]}
    assert len(set(map(tuple, assigned.values()))) == 1
    assert assigned["Win10 Baseline"] != [GROUP]
    assert [p["name"] for p in objects["deviceManagement/configurationPolicies"]] == ["Edge Settings"]
    assert [p["name"] for p in objects["deviceManagement/deviceManagementScripts"]] == ["Set Time Zone"]


@needs_pwsh
@needs_module
def test_config_selects_types_and_sets_intunemanagement_settings(tmp_path):
    result, archive = run_export(tmp_path, {"policy_types": ["SettingsCatalog", "CompliancePolicies"],
                                            "name_filter": "Edge",
                                            "settings": {"AddCompanyName": False, "AddIDToExportFile": False}})
    assert result.returncode == 0, result.stdout + result.stderr
    assert files(archive) == ["SettingsCatalog/Edge Settings.json"]


@needs_pwsh
@needs_module
def test_certificate_wins_over_the_secret(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pdt test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(b"pdt", key, cert, None, BestAvailableEncryption(b"pw"))
    result, _ = run_export(tmp_path, env={"PDT_AZURE_PRIVATE_KEY_B64": base64.b64encode(pfx).decode(),
                                          "PDT_AZURE_PRIVATE_KEY_PASSWORD": "pw"})
    assert result.returncode == 0, result.stdout + result.stderr
    sign_in = json.loads((tmp_path / "log.json").read_text(encoding="utf-8-sig"))["signIns"][0]
    assert sign_in["secret"] == ""
    assert sign_in["certificate"] == cert.fingerprint(hashes.SHA1()).hex().upper()


@needs_pwsh
@needs_module
def test_a_type_graph_refuses_fails_the_run_and_keeps_the_zip(tmp_path):
    result, archive = run_export(tmp_path, forbidden=r"^/?deviceManagement/deviceConfigurations\?")
    assert result.returncode != 0
    assert "refused to list 4 object types" in output(result)
    assert "DeviceConfiguration: list request failed (status 403)" in output(result)
    assert "Contoso/SettingsCatalog/Edge Settings_cp-1.json" in files(archive)
    assert not [name for name in files(archive) if "/DeviceConfiguration/" in name]


@needs_pwsh
@needs_module
def test_an_unknown_policy_type_fails_the_run(tmp_path):
    result, _ = run_export(tmp_path, {"policy_types": ["NoSuchType"]})
    assert result.returncode != 0
    assert "IntuneManagement does not know: PolicyType 'NoSuchType'" in output(result)


@needs_pwsh
def test_a_download_with_another_checksum_stops_the_run(tmp_path):
    command = (f"function Invoke-WebRequest {{ param($Uri, $OutFile) Set-Content $OutFile 'not the archive' }}; "
               f"& '{SCRIPT}'")
    result = pwsh(["-Command", command], {"XDG_DATA_HOME": str(tmp_path), "PDT_CONFIG_JSON": "{}", **ENV}, tmp_path)
    assert result.returncode != 0
    assert "The IntuneManagement download has SHA-256" in output(result)
    assert not (tmp_path / "pdt" / MODULE.name).exists()
