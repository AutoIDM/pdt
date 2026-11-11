"""The intune-export example: `IntuneExport.ps1` runs through tests/intune_export/harness.ps1
against IntuneManagement's own offline mock tenant, and tests/intune_export/import.ps1 imports
the zip it writes with IntuneManagement's own bulk import.

IntuneManagement is the copy the script downloads to pdt's data folder, so these tests need
no network. They run only with PDT_TEST_INTUNE=1, which the intune CI job sets after it
downloads IntuneManagement, and then a missing copy fails them."""

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

REFUSED = ["DeviceConfiguration", "iOSiPadOSUpdatePolicies", "macOSUpdatePolicies", "UpdatePolicies"]
FORBIDDEN = r"^/?deviceManagement/deviceConfigurations\?"

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")
needs_module = pytest.mark.skipif(os.environ.get("PDT_TEST_INTUNE") != "1",
                                  reason="needs PDT_TEST_INTUNE=1 and IntuneManagement in pdt's data folder")


def pwsh(args, env, cwd):
    return subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", *args],
                          capture_output=True, text=True, timeout=600, cwd=cwd,
                          env={**os.environ, "IM_MOCK_DATA": "", "NO_COLOR": "1", **env})


def run_export(tmp_path, settings=None, env=None, forbidden=None):
    assert (MODULE / "IntuneManagement.psd1").is_file(), f"needs IntuneManagement in {MODULE}"
    (tmp_path / "data/pdt").mkdir(parents=True)
    (tmp_path / "data/pdt" / MODULE.name).symlink_to(MODULE)
    (tmp_path / "output").mkdir()
    (tmp_path / "state").mkdir(exist_ok=True)
    result = pwsh(["-File", str(TESTS / "harness.ps1"), "-Script", str(SCRIPT), "-Log", str(tmp_path / "log.json"),
                   *(["-Forbidden", forbidden] if forbidden else [])],
                  {"XDG_DATA_HOME": str(tmp_path / "data"), "IM_MOCK_DATA": str(TESTS / "tenant"),
                   "PDT_OUTPUT_DIR": str(tmp_path / "output"), "PDT_STATE_DIR": str(tmp_path / "state"),
                   "PDT_PROJECT": str(tmp_path),
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


@pytest.fixture(scope="module")
def first_export(tmp_path_factory):
    folder = tmp_path_factory.mktemp("first")
    return folder, *run_export(folder)


@pytest.fixture(scope="module")
def refused_export(tmp_path_factory):
    return run_export(tmp_path_factory.mktemp("refused"), {"skip_types": REFUSED[1:]}, forbidden=FORBIDDEN)


@pytest.fixture(scope="module")
def certificate_export(tmp_path_factory):
    folder = tmp_path_factory.mktemp("certificate")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pdt test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(b"pdt", key, cert, None, BestAvailableEncryption(b"pw"))
    result, archive = run_export(folder, {"policy_types": ["SettingsCatalog", "CompliancePolicies"],
                                          "name_filter": "Edge",
                                          "settings": {"AddCompanyName": False, "AddIDToExportFile": False}},
                                 env={"PDT_AZURE_PRIVATE_KEY_B64": base64.b64encode(pfx).decode(),
                                      "PDT_AZURE_PRIVATE_KEY_PASSWORD": "pw"})
    return folder, cert, result, archive


@needs_pwsh
@needs_module
def test_export_imports_into_another_tenant(first_export, tmp_path):
    folder, result, archive = first_export
    assert result.returncode == 0, result.stdout + result.stderr
    assert (folder / "output/IntuneManagement.log").is_file()
    assert not (folder / "IntuneManagement.log").exists()
    assert json.loads((folder / "log.json").read_text(encoding="utf-8-sig"))["signIns"] == [
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
def test_config_selects_types_and_sets_intunemanagement_settings(certificate_export):
    _, _, result, archive = certificate_export
    assert result.returncode == 0, result.stdout + result.stderr
    assert files(archive) == ["SettingsCatalog/Edge Settings.json"]


@needs_pwsh
@needs_module
def test_certificate_wins_over_the_secret(certificate_export):
    folder, cert, result, _ = certificate_export
    assert result.returncode == 0, result.stdout + result.stderr
    sign_in = json.loads((folder / "log.json").read_text(encoding="utf-8-sig"))["signIns"][0]
    assert sign_in["secret"] == ""
    assert sign_in["certificate"] == cert.fingerprint(hashes.SHA1()).hex().upper()


@needs_pwsh
@needs_module
def test_a_type_graph_refuses_fails_the_run_and_keeps_the_zip(refused_export):
    result, archive = refused_export
    assert result.returncode != 0
    assert all(f"{name}: list request failed (status 403)" in output(result) for name in REFUSED)
    assert "Contoso/SettingsCatalog/Edge Settings_cp-1.json" in files(archive)
    assert not [name for name in files(archive) if "/DeviceConfiguration/" in name]


@needs_pwsh
@needs_module
def test_a_refused_type_in_skip_types_does_not_fail_the_run(refused_export, tmp_path):
    result, _ = refused_export
    assert "refused to list 1 object types" in output(result)

    result, archive = run_export(tmp_path, {"skip_types": REFUSED}, forbidden=FORBIDDEN)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contoso/SettingsCatalog/Edge Settings_cp-1.json" in files(archive)


@needs_pwsh
@needs_module
def test_each_run_lists_what_changed_since_the_last(first_export, tmp_path):
    folder, result, _ = first_export
    assert result.returncode == 0, result.stdout + result.stderr
    assert "This is the first export" in output(result)
    last = json.loads((folder / "state/last-export.json").read_text(encoding="utf-8-sig"))
    assert "Contoso/SettingsCatalog/Edge Settings_cp-1.json" in last["files"]

    del last["files"]["Contoso/SettingsCatalog/Edge Settings_cp-1.json"]
    last["files"]["Contoso/PowerShellScripts/Set Time Zone_ps-1.json"] = "0" * 64
    last["files"]["Contoso/DeviceConfiguration/Retired_dc-9.json"] = "0" * 64
    (tmp_path / "second/state").mkdir(parents=True)
    (tmp_path / "second/state/last-export.json").write_text(json.dumps(last))
    result, _ = run_export(tmp_path / "second")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (f"Since {last['name']}: 1 added, 1 changed, 1 removed. "
            "added Contoso/SettingsCatalog/Edge Settings_cp-1.json "
            "changed Contoso/PowerShellScripts/Set Time Zone_ps-1.json "
            "removed Contoso/DeviceConfiguration/Retired_dc-9.json") in output(result)


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
