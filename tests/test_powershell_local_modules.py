"""The modules `pdt run` installs before a PowerShell app runs on this computer."""

import json
import os
import shutil
import subprocess
from pathlib import PurePosixPath

import pytest

from pdt import powershell
from pdt.powershell import ModuleNeed, ScriptScan, install_command


def test_install_command_saves_into_a_folder_and_probes():
    modules = [ModuleNeed("ImportExcel", "7.8.10", "x")]
    assert install_command(modules, PurePosixPath("/data/it's")) == (
        "$ErrorActionPreference = 'Stop'; "
        "Save-PSResource -Name 'ImportExcel' -Version '7.8.10' -Repository PSGallery "
        "-TrustRepository -Path '/data/it''s' -Quiet; "
        "Import-Module 'ImportExcel' -ErrorAction Stop; "
        "[pscustomobject]@{ pdt = 1 } | Export-Excel -Path "
        "(Join-Path ([IO.Path]::GetTempPath()) 'pdt-probe.xlsx')")


@pytest.mark.parametrize(("version", "wanted", "result"), [
    ("1.0", None, True),
    ("7.8.10", "7.8.10", True),
    ("7.8.10.0", "7.8.10", True),
    ("7.8.10", "7.8.9", False),
    ("7.8.10", "[7.8.10]", True),
    ("4.2.0", "[4,5)", True),
    ("5.0.0", "[4,5)", False),
    ("3.9", "[4,5)", False),
    ("2.25.0", "[2.25.0,)", True),
    ("2.24.0", "[2.25.0,)", False),
    ("2.25.0", "(2.25.0,)", False),
    ("3.0", "(,3.0]", True),
    ("3.1", "(,3.0]", False),
])
def test_satisfies_reads_versions_as_install_psresource_does(version, wanted, result):
    assert powershell.satisfies(version, wanted) is result


def fake_pwsh(monkeypatch, *answers):
    calls = []
    queue = list(answers)

    def run(command, **kwargs):
        calls.append((command[-1], kwargs.get("env")))
        return queue.pop(0)

    monkeypatch.setattr(powershell.pwsh, "ensure_pwsh", lambda: "pwsh")
    monkeypatch.setattr(powershell.subprocess, "run", run)
    return calls


def test_the_run_loads_the_newest_version_from_the_first_path_entry(monkeypatch, tmp_path):
    first, second = tmp_path / "pdt", tmp_path / "user"
    listing = {"path": os.pathsep.join([str(first), str(second)]), "modules": [
        {"Name": "ImportExcel", "Version": "7.8.10", "ModuleBase": str(second / "ImportExcel" / "7.8.10")},
        {"Name": "ImportExcel", "Version": "7.8.9", "ModuleBase": str(first / "ImportExcel" / "7.8.9")},
        {"Name": "ImportExcel", "Version": "7.8.6", "ModuleBase": str(first / "ImportExcel" / "7.8.6")},
        {"Name": "Az.Accounts", "Version": "4.0.2", "ModuleBase": str(second / "Az.Accounts" / "4.0.2")}]}
    calls = fake_pwsh(monkeypatch, subprocess.CompletedProcess([], 0, json.dumps(listing), ""))
    path, loaded = powershell.loaded_versions(["ImportExcel", "Az.Accounts", "Pester"], first)
    assert path == listing["path"]
    assert loaded == {"importexcel": "7.8.9", "az.accounts": "4.0.2"}
    script, _env = calls[0]
    assert script.startswith(f"$env:PSModulePath = '{first}' + [IO.Path]::PathSeparator + $env:PSModulePath; ")
    assert "Get-Module -ListAvailable -Name 'ImportExcel', 'Az.Accounts', 'Pester' " in script


def test_a_failed_module_listing_is_an_error(monkeypatch, tmp_path):
    fake_pwsh(monkeypatch, subprocess.CompletedProcess([], 1, "", "boom"))
    with pytest.raises(powershell.PowerShellError, match="could not list the PowerShell modules.*boom"):
        powershell.loaded_versions(["ImportExcel"], tmp_path)


def app_needing(monkeypatch, tmp_path, *modules):
    monkeypatch.setattr(powershell, "scan", lambda app, provider: ScriptScan([], [], list(modules), []))
    return {"name": "report", "dir": tmp_path / "report", "platform": {"provider": "windows"}}


def test_a_run_installs_only_the_modules_it_would_not_load(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "modules"
    (folder / "ImportExcel" / "7.8.10").mkdir(parents=True)
    app = app_needing(monkeypatch, tmp_path, ModuleNeed("Az.Accounts", None, "x"),
                      ModuleNeed("ImportExcel", "7.8.9", "x"), ModuleNeed("PSWriteColor", None, "x"))
    monkeypatch.setattr(powershell, "loaded_versions", lambda names, where: (
        "MODULE-PATH", {"az.accounts": "4.0.2", "importexcel": "7.8.10"}))
    calls = fake_pwsh(monkeypatch, subprocess.CompletedProcess([], 0, "", ""),
                      subprocess.CompletedProcess([], 0, "", ""))
    assert powershell.install_for_run(app, folder) == {"PSModulePath": "MODULE-PATH"}
    assert [script for script, _env in calls] == [
        install_command([ModuleNeed("ImportExcel", "7.8.9", "x")], folder),
        install_command([ModuleNeed("PSWriteColor", None, "x")], folder)]
    assert all(env["PSModulePath"] == "MODULE-PATH" for _script, env in calls)
    assert not (folder / "ImportExcel" / "7.8.10").exists()
    out = " ".join(capsys.readouterr().out.split())
    assert (f"Installing the PowerShell module ImportExcel 7.8.9 into {folder}, because this computer "
            "has version 7.8.10...") in out
    assert (f"Installing the PowerShell module PSWriteColor (latest) into {folder}, because this "
            "computer does not have it...") in out
    assert "Az.Accounts" not in out


def test_a_run_with_every_module_in_place_installs_nothing(monkeypatch, tmp_path, capsys):
    app = app_needing(monkeypatch, tmp_path, ModuleNeed("ImportExcel", "[7.0,8.0)", "x"))
    monkeypatch.setattr(powershell, "loaded_versions", lambda names, where: (
        "MODULE-PATH", {"importexcel": "7.8.10"}))
    monkeypatch.setattr(powershell.subprocess, "run", lambda *a, **k: pytest.fail("pdt installed a module"))
    assert powershell.install_for_run(app, tmp_path / "modules") == {"PSModulePath": "MODULE-PATH"}
    assert capsys.readouterr().out == ""


def test_an_app_with_no_module_starts_no_pwsh(monkeypatch, tmp_path):
    app = app_needing(monkeypatch, tmp_path)
    monkeypatch.setattr(powershell, "loaded_versions", lambda *a: pytest.fail("pwsh started"))
    assert powershell.install_for_run(app, tmp_path / "modules") == {}


def test_a_failed_install_names_the_module_and_a_command_to_run(monkeypatch, tmp_path):
    app = app_needing(monkeypatch, tmp_path, ModuleNeed("ImportExcel", "7.8.9", "x"))
    monkeypatch.setattr(powershell, "loaded_versions", lambda names, where: ("MODULE-PATH", {}))
    fake_pwsh(monkeypatch, subprocess.CompletedProcess([], 1, "", "Save-PSResource: Connection refused\n"))
    with pytest.raises(powershell.PowerShellError) as error:
        powershell.install_for_run(app, tmp_path / "modules")
    assert str(error.value) == (
        "pdt could not install the PowerShell module ImportExcel from the PowerShell Gallery. "
        "Make sure this computer reaches www.powershellgallery.com and run the command again, or "
        "install the module yourself in pwsh: Install-PSResource -Name 'ImportExcel' -Version '7.8.9' "
        "-Repository PSGallery -Scope CurrentUser. pwsh said: Save-PSResource: Connection refused")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")
def test_pwsh_finds_the_module_in_the_pdt_folder_first(tmp_path, monkeypatch):
    def module(root, name, version):
        folder = root / name / version
        folder.mkdir(parents=True)
        (folder / f"{name}.psd1").write_text(f"@{{ ModuleVersion = '{version}' }}\n")

    folder, other = tmp_path / "pdt", tmp_path / "other"
    module(folder, "PdtFake", "1.0.0")
    module(other, "PdtFake", "2.0.0")
    module(other, "PdtOther", "3.1.0")
    monkeypatch.setenv("PSModulePath", str(other))
    monkeypatch.setattr(powershell.pwsh, "ensure_pwsh", lambda: shutil.which("pwsh"))
    path, loaded = powershell.loaded_versions(["PdtFake", "PdtOther", "PdtMissing"], folder)
    assert path.split(os.pathsep)[0] == str(folder)
    assert loaded == {"pdtfake": "1.0.0", "pdtother": "3.1.0"}
    proc = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-Command",
         "Import-Module PdtFake; (Get-Module PdtFake).Version.ToString()"],
        capture_output=True, text=True, env={**os.environ, "PSModulePath": path})
    assert proc.stdout.strip() == "1.0.0"
