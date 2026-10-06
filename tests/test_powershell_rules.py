import json
import shutil
import subprocess

import pytest

from pdt import powershell
from pdt.powershell import Finding, ModuleNeed, ScriptScan, install_command, judge, report, summary_lines

EMPTY_FILE = {
    "parseErrors": [], "requires": None, "usingModules": [], "importModules": [], "commands": [],
    "definedFunctions": [], "params": [], "localInvocations": [], "strings": [], "types": [],
    "newObjects": [], "assemblies": [], "dynamic": [], "remoting": [],
}


def script(name, **facts):
    return {**EMPTY_FILE, "file": name, **facts}


def cmd(name, line=1, parameters=(), splatted=False):
    return {"name": name, "line": line, "parameters": list(parameters), "splatted": splatted}


def facts(*files, requirements=None, known=None):
    return {"files": list(files), "requirements": requirements, "known": known or {}}


def no_gallery(commands):
    raise AssertionError(f"pdt looked up {commands} on the gallery")


def verdict(facts, provider="google-cloud", run_scripts=None, gallery=no_gallery):
    return judge(facts, {"run_scripts": run_scripts}, provider, gallery)


def certain(scan):
    return [(f.kind, f.file, f.line) for f in scan.findings if f.certain]


def warnings(scan):
    return [(f.kind, f.file, f.line) for f in scan.findings if not f.certain]


def test_a_script_that_another_runs_or_imports_is_a_helper():
    scan = verdict(facts(
        script("main.ps1", localInvocations=[
            {"how": "Dot", "target": ".\\helpers.ps1", "line": 1},
            {"how": "Ampersand", "target": "$PSScriptRoot/second.ps1", "line": 2},
            {"how": "Import-Module", "target": "./lib.psm1", "line": 3}]),
        script("helpers.ps1"), script("second.ps1"), script("lib.psm1"),
        script("tools/extra.ps1"), script("zz.ps1")))
    assert scan.entries == ["main.ps1", "zz.ps1"]
    assert scan.helpers == ["helpers.ps1", "second.ps1", "lib.psm1", "tools/extra.ps1"]


def test_run_scripts_sets_the_entries_and_their_order():
    scan = verdict(facts(script("a.ps1"), script("b.ps1"), script("c.ps1")),
                   run_scripts=["c.ps1", "a.ps1"])
    assert scan.entries == ["c.ps1", "a.ps1"]
    assert scan.helpers == ["b.ps1"]


def test_a_windows_only_command_is_certain_on_a_cloud_and_absent_on_windows():
    seen = facts(script("r.ps1", commands=[cmd("Get-ADUser", 5), cmd("Get-WmiObject", 6)]))
    assert certain(verdict(seen)) == [("windows-command", "r.ps1", 5), ("windows-command", "r.ps1", 6)]
    assert verdict(seen, "windows").findings == []
    problems, _ = report(verdict(seen))
    assert problems[0].startswith("r.ps1:5: Get-ADUser only exists on Windows")


def test_a_local_function_with_a_windows_name_is_not_flagged():
    seen = facts(script("r.ps1", commands=[cmd("Get-Service", 5)], definedFunctions=["Get-Service"]))
    assert verdict(seen).findings == []


def test_windows_signals_are_certain_on_a_cloud_only():
    seen = facts(script(
        "r.ps1",
        requires={"version": None, "editions": ["Desktop"], "modules": [{"name": "ActiveDirectory"}]},
        importModules=[{"name": "SmbShare", "requiredVersion": None, "minimumVersion": None,
                        "maximumVersion": None, "line": 3}],
        commands=[cmd("net", 4), cmd("Add-Type", 9), cmd("New-Object", 7), cmd("New-Object", 8)],
        types=[{"name": "System.DirectoryServices.DirectoryEntry", "line": 5}],
        newObjects=[{"typeName": None, "comObject": "Excel.Application", "line": 7},
                    {"typeName": "System.Windows.Forms.Form", "comObject": None, "line": 8}],
        assemblies=[{"name": "System.Windows.Forms", "line": 9}],
        strings=[{"kind": "registry", "text": "HKLM:\\Software", "line": 10},
                 {"kind": "cert-drive", "text": "Cert:\\LocalMachine\\My", "line": 11},
                 {"kind": "windows-path", "text": "C:\\Reports", "line": 12},
                 {"kind": "unc-path", "text": "\\\\server\\share", "line": 13}]),
        known={"Add-Type": "Microsoft.PowerShell.Utility", "New-Object": "Microsoft.PowerShell.Utility"})
    cloud = verdict(seen)
    assert certain(cloud) == [
        ("desktop-edition", "r.ps1", 0), ("windows-module", "r.ps1", 0), ("windows-module", "r.ps1", 3),
        ("windows-program", "r.ps1", 4), ("windows-drive", "r.ps1", 10), ("windows-drive", "r.ps1", 11),
        ("windows-type", "r.ps1", 5), ("com-object", "r.ps1", 7), ("windows-type", "r.ps1", 8),
        ("windows-assembly", "r.ps1", 9)]
    assert warnings(cloud) == [("windows-path", "r.ps1", 12), ("windows-path", "r.ps1", 13)]
    windows = verdict(seen, "windows")
    assert certain(windows) == [("desktop-edition", "r.ps1", 0)]
    assert [f.kind for f in windows.findings if not f.certain] == ["windows-feature", "windows-feature"]
    assert windows.modules == []


@pytest.mark.parametrize("provider", ["google-cloud", "windows"])
def test_rules_that_hold_on_every_provider(provider):
    seen = facts(
        script("r.ps1", parseErrors=[{"line": 2, "message": "Missing closing '}'"}],
               commands=[cmd("Read-Host", 3), cmd("Get-AutomationVariable", 4), cmd("Out-GridView", 5)],
               params=[{"name": "Tenant", "mandatory": True, "hasDefault": False, "line": 1},
                       {"name": "Days", "mandatory": True, "hasDefault": True, "line": 1},
                       {"name": "Out", "mandatory": False, "hasDefault": False, "line": 1}]),
        script("h.ps1", params=[{"name": "X", "mandatory": True, "hasDefault": False, "line": 1}],
               localInvocations=[]),
        script("r2.ps1", localInvocations=[{"how": "Dot", "target": "./h.ps1", "line": 1}]))
    scan = verdict(seen, provider)
    assert certain(scan) == [
        ("parse-error", "r.ps1", 2), ("mandatory-parameter", "r.ps1", 1), ("mandatory-parameter", "r.ps1", 1),
        ("interactive", "r.ps1", 3), ("azure-automation", "r.ps1", 4), ("interactive", "r.ps1", 5)]
    assert warnings(scan) == []


def test_a_login_needs_an_app_only_parameter():
    person = facts(script("r.ps1", commands=[cmd("Connect-MgGraph", 2, ["Scopes"])]))
    assert certain(verdict(person)) == [("login", "r.ps1", 2)]
    assert "-ClientId" in verdict(person).findings[0].reason
    app = facts(script("r.ps1", commands=[cmd("Connect-MgGraph", 2, ["ClientId", "CertificateThumbprint"])]))
    assert verdict(app).findings == []
    exo = facts(script("r.ps1", commands=[cmd("Connect-ExchangeOnline", 2, ["UserPrincipalName"])]))
    assert certain(verdict(exo)) == [("login", "r.ps1", 2)]
    splat = facts(script("r.ps1", commands=[cmd("Connect-AzAccount", 2, [], splatted=True)]))
    assert certain(verdict(splat)) == []
    assert warnings(verdict(splat)) == [("login", "r.ps1", 2)]


def test_a_module_used_without_its_login_is_a_warning():
    scan = verdict(facts(script("r.ps1", commands=[cmd("Get-Mailbox", 2), cmd("Get-ExoMailbox", 3)])))
    assert scan.modules == [ModuleNeed("ExchangeOnlineManagement", None, "command Get-Mailbox")]
    assert [f.reason for f in scan.findings] == [
        "the scripts use ExchangeOnlineManagement commands but never call Connect-ExchangeOnline, "
        "so the job has no session. Add Connect-ExchangeOnline with an app-only parameter."]


def scripts_asking_for_modules():
    return (
        script("a.ps1", requires={"version": "7.0", "editions": [], "modules": [
            {"name": "ImportExcel", "requiredVersion": "7.8.10", "version": None, "maximumVersion": None},
            {"name": "Pester", "requiredVersion": None, "version": "5.0", "maximumVersion": "5.9"},
            {"name": "PSScriptAnalyzer", "requiredVersion": None, "version": "1.2", "maximumVersion": None}]}),
        script("b.ps1", importModules=[
            {"name": "Microsoft.Graph.Users", "requiredVersion": "2.25.0", "minimumVersion": None,
             "maximumVersion": None, "line": 4},
            {"name": "./lib.psm1", "requiredVersion": None, "minimumVersion": None,
             "maximumVersion": None, "line": 5}],
            usingModules=["PnP.PowerShell"]))


def test_modules_come_from_requires_import_module_and_using_module():
    scan = verdict(facts(*scripts_asking_for_modules()))
    assert scan.modules == [
        ModuleNeed("ImportExcel", "7.8.10", "#Requires in a.ps1"),
        ModuleNeed("Microsoft.Graph.Users", "2.25.0", "Import-Module in b.ps1"),
        ModuleNeed("Pester", "[5.0,5.9]", "#Requires in a.ps1"),
        ModuleNeed("PnP.PowerShell", None, "using module in b.ps1"),
        ModuleNeed("PSScriptAnalyzer", "[1.2,)", "#Requires in a.ps1")]
    assert scan.findings == []


def test_modules_come_from_requires_import_module_and_requirements_psd1():
    scan = verdict(facts(
        *scripts_asking_for_modules(),
        requirements={"Az.Accounts": "4.*", "ExchangeOnlineManagement": "latest",
                      "ImportExcel": "7.8.9", "Microsoft.Graph.Users": {"Version": "2.25.0"},
                      "MicrosoftTeams": {"ModuleVersion": "6.0"}, "Pester": "5.5.0",
                      "PnP.PowerShell": "2.*", "PSScriptAnalyzer": "latest"}))
    assert scan.modules == [
        ModuleNeed("Az.Accounts", "[4,5)", "requirements.psd1"),
        ModuleNeed("ExchangeOnlineManagement", None, "requirements.psd1"),
        ModuleNeed("ImportExcel", "7.8.9", "requirements.psd1"),
        ModuleNeed("Microsoft.Graph.Users", "2.25.0", "requirements.psd1"),
        ModuleNeed("MicrosoftTeams", "[6.0,)", "requirements.psd1"),
        ModuleNeed("Pester", "5.5.0", "requirements.psd1"),
        ModuleNeed("PnP.PowerShell", "[2,3)", "requirements.psd1"),
        ModuleNeed("PSScriptAnalyzer", None, "requirements.psd1")]
    assert scan.findings == []


def test_a_module_the_scripts_need_and_requirements_psd1_leaves_out_is_a_warning():
    scan = verdict(facts(
        *scripts_asking_for_modules(),
        requirements={"importexcel": "7.8.9", "Microsoft.Graph.Users": "2.25.0", "Pester": "5.5.0",
                      "PnP.PowerShell": "latest"}))
    assert [m.name for m in scan.modules] == [
        "importexcel", "Microsoft.Graph.Users", "Pester", "PnP.PowerShell"]
    assert [(f.kind, f.file, f.line, f.reason, f.certain) for f in scan.findings] == [
        ("requirements", "requirements.psd1", 0,
         "the scripts need the module PSScriptAnalyzer (#Requires in a.ps1), and requirements.psd1 "
         "does not list it, so pdt will not install it. Add it to requirements.psd1.", False)]


def test_latest_versions_pins_what_the_gallery_answers_and_leaves_the_rest_latest():
    asked = []

    def lookup(names):
        asked.append(names)
        return {"importexcel": "7.8.10"}

    assert powershell.latest_versions(["ImportExcel", "NotOnTheGallery"], lookup) == {
        "ImportExcel": "7.8.10", "NotOnTheGallery": "latest"}
    assert asked == [["ImportExcel", "NotOnTheGallery"]]


def test_two_versions_of_one_module_keep_the_first_and_warn():
    scan = verdict(facts(
        script("a.ps1", requires={"version": None, "editions": [], "modules": [
            {"name": "ImportExcel", "requiredVersion": "7.8.10", "version": None, "maximumVersion": None}]}),
        script("b.ps1", importModules=[
            {"name": "ImportExcel", "requiredVersion": "7.8.9", "minimumVersion": None,
             "maximumVersion": None, "line": 1}])))
    assert scan.modules == [ModuleNeed("ImportExcel", "7.8.10", "#Requires in a.ps1")]
    assert warnings(scan) == [("module-version", "b.ps1", 0)]


def test_the_command_table_names_the_module_without_the_gallery():
    scan = verdict(facts(script("r.ps1", commands=[
        cmd("Set-Mailbox", 1), cmd("Export-Excel", 2), cmd("Connect-ExchangeOnline", 3, ["AppId"]),
        cmd("Get-PnPList", 4), cmd("Connect-PnPOnline", 5, ["ClientId"]),
        cmd("Get-AutomationConnection", 6)])))
    assert [(m.name, m.source) for m in scan.modules] == [
        ("ExchangeOnlineManagement", "command Set-Mailbox"),
        ("ImportExcel", "command Export-Excel"),
        ("PnP.PowerShell", "command Get-PnPList")]


def test_a_graph_or_az_command_finds_its_sub_module_on_the_gallery():
    asked = []

    def gallery(commands):
        asked.append(commands)
        return {"Get-MgUser": [{"name": "Microsoft.Graph.Users", "author": "Microsoft Corporation"}],
                "Get-AzVM": [{"name": "Az.Compute", "author": "Microsoft Corporation"},
                             {"name": "AzureRM.Compute", "author": "Microsoft Corporation"}],
                "Get-MgThing": []}

    scan = verdict(facts(script("r.ps1", commands=[
        cmd("Get-MgUser", 1), cmd("Connect-MgGraph", 2, ["Identity"]), cmd("Get-AzVM", 3),
        cmd("Connect-AzAccount", 4, ["Identity"]), cmd("Get-MgThing", 5)])), gallery=gallery)
    assert asked == [["Get-MgUser", "Get-AzVM", "Get-MgThing"]]
    assert [m.name for m in scan.modules] == [
        "Az.Accounts", "Az.Compute", "Microsoft.Graph", "Microsoft.Graph.Authentication",
        "Microsoft.Graph.Users"]
    assert [(f.kind, f.line) for f in scan.findings] == [("guessed-module", 5)]
    assert "installs all of Microsoft.Graph" in scan.findings[0].reason


def test_a_declared_family_module_skips_the_gallery():
    scan = verdict(facts(script("r.ps1", commands=[cmd("Get-MgUser", 1), cmd("Connect-MgGraph", 2, ["Identity"])],
                                requires={"version": None, "editions": [],
                                          "modules": [{"name": "Microsoft.Graph"}]})))
    assert [m.name for m in scan.modules] == ["Microsoft.Graph", "Microsoft.Graph.Authentication"]


def test_an_unknown_command_asks_the_gallery_and_guesses_only_a_microsoft_module():
    asked = []

    def gallery(commands):
        asked.append(commands)
        return {"Get-MsalToken": [{"name": "MSAL.PS", "author": "Jason Thompson"}],
                "Get-SqlDatabase": [{"name": "SqlServer", "author": "Microsoft Corporation"}]}

    scan = verdict(facts(script("r.ps1", commands=[
        cmd("Get-MsalToken", 1), cmd("Get-SqlDatabase", 2), cmd("Get-Nothing", 3),
        cmd("Export-Csv", 4), cmd("Get-Tree", 5), cmd("My-Helper", 6)],
        definedFunctions=["My-Helper"]),
        known={"Export-Csv": "Microsoft.PowerShell.Utility", "Get-Tree": "PSTree"}), gallery=gallery)
    assert asked == [["Get-MsalToken", "Get-SqlDatabase", "Get-Nothing"]]
    assert scan.modules == [ModuleNeed("PSTree", None, "command Get-Tree"),
                            ModuleNeed("SqlServer", None, "command Get-SqlDatabase")]
    assert [(f.kind, f.line) for f in scan.findings] == [
        ("unknown-command", 1), ("guessed-module", 2), ("unknown-command", 3), ("guessed-module", 5)]
    assert scan.findings[1].reason == (
        "pdt guessed the module SqlServer for Get-SqlDatabase from the PowerShell Gallery; add "
        "`#Requires -Modules SqlServer` to the script to be sure.")


def test_soft_signals_are_warnings():
    scan = verdict(facts(script(
        "r.ps1",
        commands=[cmd("Invoke-Expression", 1), cmd("Install-Module", 2), cmd("Write-Progress", 3),
                  cmd("\ufeffFunction", 4)],
        remoting=[{"text": "Invoke-Command -ComputerName dc1", "line": 5}],
        dynamic=[{"kind": "command-from-expression", "text": "& $tool", "line": 6}]),
        known={"Invoke-Expression": "Microsoft.PowerShell.Utility", "Install-Module": "PowerShellGet",
               "Write-Progress": "Microsoft.PowerShell.Utility", "\ufeffFunction": None}))
    assert certain(scan) == []
    assert [f.kind for f in scan.findings] == [
        "dynamic", "install-at-run-time", "progress", "bom", "remoting", "dynamic"]


def test_install_command_installs_imports_and_probes():
    modules = [ModuleNeed("ImportExcel", "7.8.10", "x"), ModuleNeed("Microsoft.Graph.Users", None, "x")]
    assert install_command(modules) == (
        "$ErrorActionPreference = 'Stop'; "
        "Install-PSResource -Name 'ImportExcel' -Version '7.8.10' -Repository PSGallery "
        "-TrustRepository -Scope AllUsers -Quiet; "
        "Install-PSResource -Name 'Microsoft.Graph.Users' -Repository PSGallery "
        "-TrustRepository -Scope AllUsers -Quiet; "
        "Import-Module 'ImportExcel' -ErrorAction Stop; "
        "Import-Module 'Microsoft.Graph.Users' -ErrorAction Stop; "
        "Import-Module Microsoft.Graph.Authentication -ErrorAction Stop; Get-MgContext | Out-Null; "
        "[pscustomobject]@{ pdt = 1 } | Export-Excel -Path "
        "(Join-Path ([IO.Path]::GetTempPath()) 'pdt-probe.xlsx')")
    assert install_command([]) == ""
    assert install_command([ModuleNeed("It's", "1.0'", "x")]).startswith(
        "$ErrorActionPreference = 'Stop'; Install-PSResource -Name 'It''s' -Version '1.0''' ")


def test_quoted_doubles_single_quotes():
    assert powershell.quoted("it's") == "'it''s'"


def test_the_gallery_lookup_runs_one_pwsh_and_caches_empty_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(powershell, "GALLERY_CACHE", tmp_path / "psgallery.json")
    monkeypatch.setattr(powershell.pwsh, "ensure_pwsh", lambda: "pwsh")
    calls = []

    def run(command, **kwargs):
        calls.append(command[-1])
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "Get-SqlDatabase": [{"Name": "SqlServer", "Author": "Microsoft Corporation"}],
            "Get-Nothing": []}), "")

    monkeypatch.setattr(powershell.subprocess, "run", run)
    expected = {"Get-SqlDatabase": [{"name": "SqlServer", "author": "Microsoft Corporation"}],
                "Get-Nothing": []}
    assert powershell.find_in_gallery(["Get-SqlDatabase", "Get-Nothing"]) == expected
    assert len(calls) == 1
    assert "foreach ($name in @('Get-SqlDatabase', 'Get-Nothing'))" in calls[0]
    assert powershell.find_in_gallery(["Get-Nothing", "Get-SqlDatabase"]) == {
        "Get-Nothing": [], "Get-SqlDatabase": expected["Get-SqlDatabase"]}
    assert len(calls) == 1


def test_report_and_summary_lines():
    scan = ScriptScan(
        ["a.ps1", "b.ps1"], ["lib.psm1"],
        [ModuleNeed("ImportExcel", None, "x"), ModuleNeed("Microsoft.Graph.Users", "2.25.0", "x")],
        [Finding("interactive", "a.ps1", 3, "Read-Host waits.", True),
         Finding("windows-module", "a.ps1", 0, "the module X only exists on Windows.", True),
         Finding("no-login", "", 0, "never calls Connect-MgGraph.", False)])
    assert report(scan) == (
        ["a.ps1:3: Read-Host waits.", "a.ps1: the module X only exists on Windows."],
        ["never calls Connect-MgGraph."])
    assert summary_lines(scan) == [
        "runs, in order: a.ps1, b.ps1",
        "helper files: lib.psm1",
        "modules to install: ImportExcel (latest), Microsoft.Graph.Users 2.25.0"]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")
def test_the_extractor_reads_a_real_script(tmp_path, monkeypatch):
    (tmp_path / "report.ps1").write_text(
        "#Requires -Version 7.0\n"
        "#Requires -Modules @{ ModuleName = 'ImportExcel'; RequiredVersion = '7.8.10' }\n"
        "param([Parameter(Mandatory)][string]$Tenant, [int]$Days = 30)\n"
        "Import-Module ./lib.psm1\n"
        ". \"$PSScriptRoot\\helpers.ps1\"\n"
        "function Get-Thing { 1 }\n"
        "Get-Thing\n"
        "$out = 'C:\\Reports'\n"
        "Connect-MgGraph -ClientId $env:ID -CertificateThumbprint $env:TP\n"
        "Get-MgUser -All | Export-Excel -Path (Join-Path $out 'u.xlsx')\n"
        "[System.DirectoryServices.DirectoryEntry]'LDAP://x'\n"
        "$x = New-Object -ComObject Excel.Application\n")
    (tmp_path / "lib.psm1").write_text("function Get-Lib { 1 }\n")
    (tmp_path / "helpers.ps1").write_text("function Get-Help2 { 1 }\n")
    (tmp_path / "requirements.psd1").write_text("@{ 'Az.Accounts' = '4.*' }\n")
    monkeypatch.setattr(powershell.pwsh, "ensure_pwsh", lambda: shutil.which("pwsh"))
    seen = powershell.extract(tmp_path)
    assert [f["file"] for f in seen["files"]] == ["helpers.ps1", "lib.psm1", "report.ps1"]
    main = seen["files"][2]
    assert main["requires"]["modules"] == [
        {"name": "ImportExcel", "requiredVersion": "7.8.10", "version": None, "maximumVersion": None}]
    assert main["params"] == [
        {"name": "Tenant", "mandatory": True, "hasDefault": False, "line": 3},
        {"name": "Days", "mandatory": False, "hasDefault": True, "line": 3}]
    assert main["localInvocations"] == [
        {"how": "Import-Module", "target": "./lib.psm1", "line": 4},
        {"how": "Dot", "target": "$PSScriptRoot\\helpers.ps1", "line": 5}]
    assert main["definedFunctions"] == ["Get-Thing"]
    assert main["strings"] == [{"kind": "windows-path", "text": "C:\\Reports", "line": 8}]
    assert {"name": "System.DirectoryServices.DirectoryEntry", "line": 11} in main["types"]
    assert main["newObjects"] == [{"typeName": None, "comObject": "Excel.Application", "line": 12}]
    login = [c for c in main["commands"] if c["name"] == "Connect-MgGraph"][0]
    assert login["parameters"] == ["ClientId", "CertificateThumbprint"]
    assert seen["requirements"] == {"Az.Accounts": "4.*"}
    assert seen["known"]["Import-Module"] == "Microsoft.PowerShell.Core"
    assert seen["known"]["Get-Thing"] is None
    scan = judge(seen, {"run_scripts": None}, "google-cloud", lambda commands: {})
    assert scan.entries == ["report.ps1"]
    assert scan.helpers == ["helpers.ps1", "lib.psm1"]
    assert [(f.kind, f.line, f.certain) for f in scan.findings][:2] == [
        ("mandatory-parameter", 3, True), ("windows-path", 8, False)]


def test_in_box_and_built_in_modules_are_never_installed():
    facts = {"files": [{
        "file": "a.ps1", "parseErrors": [], "requires": {"editions": [], "modules": [
            {"name": "ActiveDirectory"}, {"name": "CimCmdlets"}]},
        "usingModules": [], "importModules": [], "commands": [], "definedFunctions": [],
        "params": [], "localInvocations": [], "strings": [], "types": [], "newObjects": [],
        "assemblies": [], "dynamic": [], "remoting": []}], "requirements": None, "known": {}}
    scan = powershell.judge(facts, {"run_scripts": None}, "windows", gallery=no_gallery)
    assert scan.modules == []
    assert scan.host_modules == ["ActiveDirectory"]
