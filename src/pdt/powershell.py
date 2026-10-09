"""Rules for an app made of PowerShell scripts.

`scan_powershell.ps1` lists facts about the scripts; this module judges them.
A `Finding` with `certain=True` fails `pdt validate`, because the job would
fail for sure; the rest are warnings. A `ModuleNeed` is a module the job's
image or PC must install before the scripts run; `install_for_run` installs
the same ones for `pdt run`. The tables below decide both; `judge` only walks
the facts. A requirements.psd1 in the app folder replaces the modules the
scripts ask for.

Modules named only by a command (Get-MgUser, Get-AzVM) come from the
PowerShell Gallery through `find_in_gallery`, which `judge` calls once with
every such command; answers are cached in the data folder. Tests pass their
own lookup to `judge`, so no test reaches the network.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pdt import console, pwsh
from pdt.config import data_home, locked, write_text_atomically


@dataclass(frozen=True)
class Finding:
    kind: str
    file: str
    line: int
    reason: str
    certain: bool


@dataclass(frozen=True)
class ModuleNeed:
    name: str
    version: str | None
    source: str


@dataclass
class ScriptScan:
    entries: list[str]
    helpers: list[str]
    modules: list[ModuleNeed]
    findings: list[Finding]
    host_modules: list[str] = field(default_factory=list)
    entry_rule: str = ""
    from_requirements: bool = False


class PowerShellError(Exception):
    pass


SCANNER = Path(__file__).with_name("scan_powershell.ps1")
GALLERY_CACHE = data_home() / "pdt" / "psgallery.json"
MODULE_DIR = data_home() / "pdt" / "powershell-modules"
BOM = "\ufeff"


def lower(names):
    """PowerShell names are case-insensitive, so every table is looked up in lower case."""
    if isinstance(names, dict):
        return {name.lower(): value for name, value in names.items()}
    return frozenset(name.lower() for name in names)


# Modules that come with Windows or its RSAT features, so no gallery holds them,
# each with the Windows optional feature that provides it (None: Windows itself),
# for the message a Windows deploy prints when the PC lacks it.
WINDOWS_IN_BOX_MODULES = lower({
    "ActiveDirectory": "RSAT: Active Directory Domain Services and Lightweight Directory Services Tools",
    "GroupPolicy": "RSAT: Group Policy Management Tools",
    "DnsServer": "RSAT: DNS Server Tools",
    "DhcpServer": "RSAT: DHCP Server Tools",
    "FailoverClusters": "RSAT: Failover Clustering Tools",
    "ServerManager": "RSAT: Server Manager",
    "Hyper-V": "Hyper-V Module for Windows PowerShell",
    "WebAdministration": "IIS Management Scripts and Tools",
    **dict.fromkeys((
        "ScheduledTasks", "Microsoft.PowerShell.LocalAccounts", "Storage", "NetTCPIP",
        "NetAdapter", "NetSecurity", "SmbShare", "Defender", "BitLocker", "PrintManagement",
        "Dism", "PKI", "IISAdministration", "Microsoft.PowerShell.Diagnostics",
        "Microsoft.WSMan.Management", "CimCmdlets", "Appx", "International")),
})
# Gallery modules that load only on Windows.
WINDOWS_ONLY_GALLERY_MODULES = lower({
    "MSOnline", "AzureAD", "AzureADPreview", "Microsoft.Online.SharePoint.PowerShell",
    "SharePointPnPPowerShellOnline", "SkypeOnlineConnector", "NTFSSecurity",
})
WINDOWS_ONLY_MODULES = WINDOWS_IN_BOX_MODULES.keys() | WINDOWS_ONLY_GALLERY_MODULES
WINDOWS_ONLY_COMMANDS = lower({
    "Get-WmiObject", "Invoke-WmiMethod", "Set-WmiInstance", "Remove-WmiObject",
    "Register-WmiEvent", "Get-EventLog", "Write-EventLog", "Clear-EventLog",
    "Limit-EventLog", "New-EventLog", "Get-WinEvent", "Get-Service", "Start-Service",
    "Stop-Service", "Restart-Service", "Set-Service", "New-Service", "Get-HotFix",
    "Get-Acl", "Set-Acl", "Get-ComputerInfo", "Get-Counter", "Get-CimInstance",
    "Invoke-CimMethod", "New-CimSession", "New-CimSessionOption", "Remove-CimSession",
    "Get-CimClass", "Set-CimInstance", "Remove-CimInstance", "Get-NetAdapter",
    "Get-NetIPAddress", "Test-NetConnection", "Resolve-DnsName",
    "Get-DnsServerResourceRecord", "Get-Clipboard", "Set-Clipboard", "Out-Printer",
    "Add-PSSnapin", "Get-PSSnapin", "Get-Cluster", "Get-ClusterSharedVolume",
})
# Commands whose name suggests a Windows module (Get-ADUser, New-SmbShare, ...).
WINDOWS_ONLY_COMMAND_PATTERN = re.compile(
    r"^\w+-((?i:ad(?:user|computer|group|object|domain|forest|organizationalunit|account"
    r"|replication|principal|rootdse|serviceaccount|trust|finegrained|default|central|optional"
    r"|resource|claim|auth|dc|kds|site))|Smb[A-Z]|Cluster|ScheduledTask|ScheduledJob|Msol|AzureAD"
    r"|SPO[A-Z]|Local(User|Group)|NetAdapter|NetIP|NetRoute|NetFirewall|Dns(Server|Client)"
    r"|GP(O|Link|Permission|Inheritance|RegistryValue|Report|Starter)|WindowsFeature"
    r"|WindowsOptionalFeature|Appx|BitLocker|MpPreference|MpComputerStatus)")
INTERACTIVE_COMMANDS = lower({"Read-Host", "Get-Credential", "Out-GridView", "Show-Command", "Pause"})
INSTALL_COMMANDS = lower({"Install-Module", "Install-PSResource", "Update-Module",
                          "Update-PSResource", "Install-Package", "Save-Module"})
AZURE_AUTOMATION_COMMANDS = lower({"Get-AutomationConnection", "Get-AutomationVariable",
                                   "Get-AutomationCertificate", "Get-AutomationPSCredential",
                                   "Set-AutomationVariable"})
# Env vars the operating system or pdt sets, which a script's read does not make required.
SYSTEM_ENV_VARS = lower({
    "APPDATA", "COMPUTERNAME", "HOME", "HOSTNAME", "LOCALAPPDATA", "OS", "PATH", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "PSModulePath", "PUBLIC", "PWD", "SystemDrive",
    "SystemRoot", "TEMP", "TMP", "TMPDIR", "USER", "USERDOMAIN", "USERNAME", "USERPROFILE", "windir",
})
# Login command -> the parameter a message names, and every parameter that signs in
# without a person.
LOGIN_COMMANDS = {command.lower(): (params[0], lower(params)) for command, params in {
    "Connect-MgGraph": ("ClientId", "ClientSecretCredential", "CertificateThumbprint",
                        "CertificateName", "Certificate", "Identity", "AccessToken",
                        "ManagedIdentity"),
    "Connect-AzAccount": ("ServicePrincipal", "Identity", "Credential",
                          "CertificateThumbprint", "AccessToken", "FederatedToken"),
    "Connect-ExchangeOnline": ("AppId", "CertificateThumbprint", "CertificateFilePath",
                               "Certificate", "ManagedIdentity", "AccessToken", "Credential"),
    "Connect-IPPSSession": ("AppId", "CertificateThumbprint", "CertificateFilePath",
                            "Certificate", "ManagedIdentity", "AccessToken", "Credential"),
    "Connect-PnPOnline": ("ClientId", "ClientSecret", "Thumbprint", "CertificatePath",
                          "ManagedIdentity", "AccessToken", "Credentials"),
    "Connect-MicrosoftTeams": ("ApplicationId", "CertificateThumbprint", "Identity",
                               "AccessTokens", "Credential"),
    "Connect-AzureAD": ("ApplicationId", "CertificateThumbprint", "Credential", "AadAccessToken"),
    "Connect-MsolService": ("Credential",),
    "Connect-SPOService": ("Credential", "ClientTag", "ModernAuth"),
}.items()}
WINDOWS_TYPE_PATTERN = re.compile(
    r"^(System\.)?(Windows\.Forms|Windows\.(Media|Controls)|DirectoryServices"
    r"|Management\.(?!Automation)|Diagnostics\.EventLog|Security\.Principal\.WindowsIdentity"
    r"|Security\.AccessControl|ServiceProcess|Speech)"
    r"|^(Microsoft\.Win32|adsi|adsisearcher|wmi|wmiclass|wmisearcher|Microsoft\.Office\.Interop"
    r"|Microsoft\.ActiveDirectory|System\.Printing|Microsoft\.SqlServer\.Management\.Smo)",
    re.IGNORECASE)
WINDOWS_ASSEMBLIES = lower({
    "System.Windows.Forms", "PresentationFramework", "PresentationCore", "System.Drawing",
    "System.DirectoryServices", "System.DirectoryServices.AccountManagement", "System.Speech",
    "Microsoft.Office.Interop.Excel", "Microsoft.Office.Interop.Outlook",
})
WINDOWS_EXES = lower({
    "attrib", "icacls", "cacls", "takeown", "robocopy", "xcopy", "net", "net1", "sc", "reg",
    "schtasks", "wmic", "netsh", "gpupdate", "gpresult", "nltest", "dcdiag", "repadmin",
    "dsquery", "dsget", "klist", "certutil", "msiexec", "powershell", "cmd", "ipconfig",
    "systeminfo", "tasklist", "taskkill", "w32tm", "bcdedit", "diskpart", "cscript", "wscript",
    "regsvr32", "rundll32", "sfc", "dism", "runas", "explorer", "notepad",
})

# Command -> module, for commands whose name does not say which module they come
# from, or whose gallery lookup picks a wrong module (Set-Mailbox -> MailcowHelper).
COMMAND_MODULES = lower({
    "Connect-MgGraph": "Microsoft.Graph.Authentication",
    "Disconnect-MgGraph": "Microsoft.Graph.Authentication",
    "Get-MgContext": "Microsoft.Graph.Authentication",
    "Invoke-MgGraphRequest": "Microsoft.Graph.Authentication",
    "Connect-AzAccount": "Az.Accounts",
    "Disconnect-AzAccount": "Az.Accounts",
    "Get-AzContext": "Az.Accounts",
    "Set-AzContext": "Az.Accounts",
    "Get-AzAccessToken": "Az.Accounts",
    "Connect-ExchangeOnline": "ExchangeOnlineManagement",
    "Disconnect-ExchangeOnline": "ExchangeOnlineManagement",
    "Connect-IPPSSession": "ExchangeOnlineManagement",
    "Get-ConnectionInformation": "ExchangeOnlineManagement",
    "Get-Mailbox": "ExchangeOnlineManagement",
    "Set-Mailbox": "ExchangeOnlineManagement",
    "Get-MailboxStatistics": "ExchangeOnlineManagement",
    "Get-MailboxPermission": "ExchangeOnlineManagement",
    "Get-MailboxFolderStatistics": "ExchangeOnlineManagement",
    "Get-CASMailbox": "ExchangeOnlineManagement",
    "Get-DistributionGroup": "ExchangeOnlineManagement",
    "Get-DistributionGroupMember": "ExchangeOnlineManagement",
    "Get-DynamicDistributionGroup": "ExchangeOnlineManagement",
    "Get-MessageTrace": "ExchangeOnlineManagement",
    "Get-MessageTraceDetail": "ExchangeOnlineManagement",
    "Search-UnifiedAuditLog": "ExchangeOnlineManagement",
    "Search-Mailbox": "ExchangeOnlineManagement",
    "Get-Recipient": "ExchangeOnlineManagement",
    "Get-UnifiedGroup": "ExchangeOnlineManagement",
    "Get-UnifiedGroupLinks": "ExchangeOnlineManagement",
    "Get-OrganizationConfig": "ExchangeOnlineManagement",
    "Set-OrganizationConfig": "ExchangeOnlineManagement",
    "Get-TransportRule": "ExchangeOnlineManagement",
    "Get-InboxRule": "ExchangeOnlineManagement",
    "Get-MailContact": "ExchangeOnlineManagement",
    "Get-MailUser": "ExchangeOnlineManagement",
    "Get-AcceptedDomain": "ExchangeOnlineManagement",
    "Get-RetentionPolicy": "ExchangeOnlineManagement",
    "Get-MobileDevice": "ExchangeOnlineManagement",
    "Get-MobileDeviceStatistics": "ExchangeOnlineManagement",
    "Get-SharingPolicy": "ExchangeOnlineManagement",
    "Get-QuarantineMessage": "ExchangeOnlineManagement",
    "Get-HostedContentFilterPolicy": "ExchangeOnlineManagement",
    "Get-AntiPhishPolicy": "ExchangeOnlineManagement",
    "Get-SafeLinksPolicy": "ExchangeOnlineManagement",
    "Get-DlpCompliancePolicy": "ExchangeOnlineManagement",
    "Get-ComplianceSearch": "ExchangeOnlineManagement",
    "New-ComplianceSearch": "ExchangeOnlineManagement",
    "Start-ComplianceSearch": "ExchangeOnlineManagement",
    "Get-RetentionCompliancePolicy": "ExchangeOnlineManagement",
    "Get-AdminAuditLogConfig": "ExchangeOnlineManagement",
    "Set-AdminAuditLogConfig": "ExchangeOnlineManagement",
    "Get-MailboxAuditBypassAssociation": "ExchangeOnlineManagement",
    "Export-Excel": "ImportExcel",
    "Import-Excel": "ImportExcel",
    "Open-ExcelPackage": "ImportExcel",
    "Close-ExcelPackage": "ImportExcel",
    "Add-Worksheet": "ImportExcel",
    "Get-ExcelSheetInfo": "ImportExcel",
    "Get-ExcelWorkbookInfo": "ImportExcel",
    "Set-ExcelRange": "ImportExcel",
    "Set-ExcelColumn": "ImportExcel",
    "Set-ExcelRow": "ImportExcel",
    "Add-ConditionalFormatting": "ImportExcel",
    "New-ConditionalText": "ImportExcel",
    "Add-ExcelChart": "ImportExcel",
    "New-ExcelChartDefinition": "ImportExcel",
    "Add-PivotTable": "ImportExcel",
    "New-PivotTableDefinition": "ImportExcel",
    "Export-ExcelSheet": "ImportExcel",
    "Merge-Worksheet": "ImportExcel",
    "Join-Worksheet": "ImportExcel",
    "Copy-ExcelWorksheet": "ImportExcel",
    "Remove-Worksheet": "ImportExcel",
    "Send-SQLDataToExcel": "ImportExcel",
    "ConvertFrom-ExcelSheet": "ImportExcel",
    "ConvertFrom-ExcelToSQLInsert": "ImportExcel",
    "Connect-MicrosoftTeams": "MicrosoftTeams",
    "Disconnect-MicrosoftTeams": "MicrosoftTeams",
    "Connect-PnPOnline": "PnP.PowerShell",
    "Disconnect-PnPOnline": "PnP.PowerShell",
    "Connect-AzureAD": "AzureAD",
    "Disconnect-AzureAD": "AzureAD",
    "Connect-MsolService": "MSOnline",
    "Connect-SPOService": "Microsoft.Online.SharePoint.PowerShell",
    "Disconnect-SPOService": "Microsoft.Online.SharePoint.PowerShell",
})
# Command noun prefix -> module. A family (Microsoft.Graph, Az) is many
# sub-modules; the gallery names the one that holds the command.
COMMAND_PREFIX_MODULES = (
    (re.compile(r"^\w+-Mg[A-Z]"), "Microsoft.Graph"),
    (re.compile(r"^\w+-Az(?!ure)[A-Z]"), "Az"),
    (re.compile(r"^\w+-PnP[A-Z]", re.IGNORECASE), "PnP.PowerShell"),
    (re.compile(r"^\w+-EXO[A-Z]", re.IGNORECASE), "ExchangeOnlineManagement"),
    (re.compile(r"^\w+-(Team[A-Z]|CsOnline|CsTeams|CsTenant|CsUser|CsPhone|CsAutoAttendant|CsCallQueue)"),
     "MicrosoftTeams"),
    (re.compile(r"^\w+-AzureAD[A-Z]"), "AzureAD"),
    (re.compile(r"^\w+-Msol[A-Z]"), "MSOnline"),
    (re.compile(r"^\w+-SPO[A-Z]"), "Microsoft.Online.SharePoint.PowerShell"),
)
FAMILIES = ("Microsoft.Graph", "Az")
# Module name prefix -> the login command its commands need first.
LOGIN_FOR = (
    ("Microsoft.Graph", "Connect-MgGraph"),
    ("Az", "Connect-AzAccount"),
    ("ExchangeOnlineManagement", "Connect-ExchangeOnline"),
    ("PnP.PowerShell", "Connect-PnPOnline"),
    ("MicrosoftTeams", "Connect-MicrosoftTeams"),
    ("AzureAD", "Connect-AzureAD"),
    ("MSOnline", "Connect-MsolService"),
    ("Microsoft.Online.SharePoint.PowerShell", "Connect-SPOService"),
)
# Module name prefix -> a command that runs without a login, so a module that
# imports but fails at its first call stops the build, not the scheduled job.
PROBES = (
    ("Microsoft.Graph", "Import-Module Microsoft.Graph.Authentication -ErrorAction Stop; "
                        "Get-MgContext | Out-Null"),
    ("Az", "Import-Module Az.Accounts -ErrorAction Stop; Get-AzContext | Out-Null"),
    ("ExchangeOnlineManagement", "Get-ConnectionInformation | Out-Null"),
    ("ImportExcel", "[pscustomobject]@{ pdt = 1 } | Export-Excel -Path "
                    "(Join-Path ([IO.Path]::GetTempPath()) 'pdt-probe.xlsx')"),
)
# Modules that ship with pwsh 7, so a command they provide needs no install.
BUILT_IN_MODULES = lower({
    "Microsoft.PowerShell.Core", "Microsoft.PowerShell.Utility", "Microsoft.PowerShell.Management",
    "Microsoft.PowerShell.Security", "Microsoft.PowerShell.Host", "Microsoft.PowerShell.Archive",
    "Microsoft.PowerShell.Diagnostics", "Microsoft.WSMan.Management", "CimCmdlets", "PSReadLine",
    "ThreadJob", "Microsoft.PowerShell.ThreadJob", "PackageManagement", "PowerShellGet",
    "Microsoft.PowerShell.PSResourceGet", "PSDesiredStateConfiguration",
})
MICROSOFT_AUTHORS = ("Microsoft", "AzureAutomationTeam")


def scan(app: dict, provider: str) -> ScriptScan:
    return judge(extract(app["dir"]), app, provider)


def extract(folder: Path) -> dict:
    """Run the fact extractor on `folder` through pwsh. It writes its JSON to a file,
    because a module that loads while it runs can print to stdout. The file stays in
    `scan_dir()`, named for the path, mtime, and size of each PowerShell file, so a
    later scan of the same files in this command, or in a process it starts, runs no pwsh."""
    files = sorted(path for path in Path(folder).rglob("*")
                   if path.is_file() and path.suffix.lower() in (".ps1", ".psm1", ".psd1"))
    state = json.dumps([str(Path(folder).resolve()),
                        [[str(path), path.stat().st_mtime_ns, path.stat().st_size] for path in files]])
    out = scan_dir() / f"{hashlib.sha256(state.encode()).hexdigest()}.json"
    if out.is_file():
        return json.loads(out.read_text(encoding="utf-8"))
    proc = subprocess.run(
        [pwsh.ensure_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCANNER),
         str(folder), str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        raise PowerShellError(
            f"pdt could not read the scripts in {folder}: {proc.stderr.strip()}")
    return json.loads(out.read_text(encoding="utf-8"))


def scan_dir() -> Path:
    """The folder of this command's scans. The first scan makes it and names it in
    PDT_SCAN_DIR, which every process the command starts inherits; it goes at exit."""
    folder = os.environ.get("PDT_SCAN_DIR", "")
    if folder == "":
        folder = tempfile.mkdtemp(prefix="pdt-scan-")
        atexit.register(shutil.rmtree, folder, ignore_errors=True)
        os.environ["PDT_SCAN_DIR"] = folder
    return Path(folder)


def unlisted_env_reads(facts: dict, env: dict) -> dict[str, str]:
    """{name: "file:line"} of the first read of each env var the scripts read that the
    scripts do not set, the env spec does not list, and the system or pdt does not set."""
    listed = {name.lower() for key in ("required", "optional") for name in env.get(key) or []}
    listed |= {name.lower() for group in env.get("one_of") or [] for name in group}
    reads = [(f["file"], read) for f in facts["files"] for read in f["envReads"]]
    listed |= {read["name"].lower() for _file, read in reads if read["set"]}
    found = {}
    for file, read in reads:
        key = read["name"].lower()
        if key in listed or key in SYSTEM_ENV_VARS or key.startswith("pdt_"):
            continue
        listed.add(key)
        found[read["name"]] = f"{file}:{read['line']}"
    return found


def judge(facts: dict, app: dict, provider: str, gallery=None) -> ScriptScan:
    """Apply the rules to the extractor's facts. `gallery` replaces `find_in_gallery`:
    it takes every command to look up and returns {command: [{name, author}]}."""
    gallery = gallery or find_in_gallery
    on_windows = provider == "windows"
    files = facts["files"]
    entries, helpers = split_files(files, app.get("run_scripts"))
    defined = {name.lower() for f in files for name in f["definedFunctions"]}
    all_commands = {c["name"].lower() for f in files for c in f["commands"]}
    requirements = facts.get("requirements")
    findings: list[Finding] = []
    needs: dict[str, ModuleNeed] = {}
    host: dict[str, str] = {}

    def need(name: str, version: str | None, source: str, file: str) -> None:
        if name.lower() in BUILT_IN_MODULES:
            return
        if name.lower() in WINDOWS_IN_BOX_MODULES:
            host.setdefault(name.lower(), name)
            if on_windows:
                findings.append(Finding(
                    "windows-feature", file, 0,
                    f"the module {name} is a Windows feature (RSAT), which pdt cannot install "
                    "from the PowerShell Gallery. Make sure it is installed on this PC.", False))
            return
        held = needs.get(name.lower())
        if held is None or (held.version is None and version is not None):
            needs[name.lower()] = ModuleNeed(name, version, source)
        elif version is not None and held.version != version and requirements is None:
            findings.append(Finding(
                "module-version", file, 0,
                f"two versions are asked for the module {name}: {held.version} ({held.source}) and "
                f"{version} ({source}); pdt installs {held.version}. Pick one.", False))

    for f in files:
        path = f["file"]
        for error in f["parseErrors"]:
            findings.append(Finding(
                "parse-error", path, error["line"],
                f"PowerShell cannot read this file: {error['message']} Fix the syntax.", True))
        requires = f["requires"] or {}
        if "Desktop" in requires.get("editions", []):
            findings.append(Finding(
                "desktop-edition", path, 0,
                "#Requires -PSEdition Desktop asks for Windows PowerShell 5.1, but pdt runs "
                "PowerShell 7. Make the script run on PowerShell 7; most current modules, such as "
                "Microsoft.Graph, Az, ExchangeOnlineManagement, and ActiveDirectory, work there. "
                "Or, only on the windows provider, load the Windows PowerShell module with "
                "Import-Module <name> -UseWindowsPowerShell and remove the #Requires -PSEdition "
                "Desktop line.", True))
        for spec in requires.get("modules", []):
            need(spec["name"], requires_version(spec), f"#Requires in {path}", path)
            if not on_windows and spec["name"].lower() in WINDOWS_ONLY_MODULES:
                findings.append(windows_only_module(path, 0, spec["name"]))
        for name in f["usingModules"]:
            need(name, None, f"using module in {path}", path)
            if not on_windows and name.lower() in WINDOWS_ONLY_MODULES:
                findings.append(windows_only_module(path, 0, name))
        for imp in f["importModules"]:
            if is_path(imp["name"]):
                continue
            need(imp["name"], import_version(imp), f"Import-Module in {path}", path)
            if not on_windows and imp["name"].lower() in WINDOWS_ONLY_MODULES:
                findings.append(windows_only_module(path, imp["line"], imp["name"]))
        if path in entries:
            for param in f["params"]:
                if param["mandatory"]:
                    findings.append(Finding(
                        "mandatory-parameter", path, param["line"],
                        f"the parameter -{param['name']} is mandatory, and pdt runs the script "
                        "with no arguments, so PowerShell stops with 'missing mandatory "
                        "parameters'. Remove Mandatory and give it a default, or read the "
                        "value from an environment variable.", True))
        for c in f["commands"]:
            findings.extend(command_findings(c, path, on_windows, defined))
        if not on_windows:
            findings.extend(windows_findings(f))

    declared = {n.lower() for n in needs} | {n.lower() for n in requirements or {}}
    known = facts["known"]
    first_uses: dict[str, tuple[str, str, int]] = {}
    for f in files:
        for c in f["commands"]:
            name = c["name"]
            key = name.lower()
            if key not in first_uses and key not in defined and not name.startswith(BOM):
                first_uses[key] = (name, f["file"], c["line"])
    asked = [name for name, _file, _line in first_uses.values()
             if module_for(name, known.get(name), declared, None) is None]
    found = gallery(asked) if asked else {}
    for name, file, line in first_uses.values():
        module, finding = module_for(name, known.get(name), declared, found)
        if finding is not None:
            findings.append(Finding(finding.kind, file, line, finding.reason, False))
        if module is not None:
            need(module, None, f"command {name} in {file}", file)

    for prefix, login in LOGIN_FOR:
        users = [m.name for m in needs.values() if m.source.startswith("command ")
                 and (m.name == prefix or m.name.startswith(prefix + "."))]
        if users and login.lower() not in all_commands:
            findings.append(Finding(
                "no-login", "", 0,
                f"the scripts use {', '.join(users)} commands but never call {login}, so the "
                f"job has no session. Add {login} with an app-only parameter.", False))

    if requirements is not None:
        scanned, needs = needs, {}
        for name, spec in requirements.items():
            need(name, requirements_version(spec), "requirements.psd1", "requirements.psd1")
        for key, m in scanned.items():
            if key not in needs:
                findings.append(Finding(
                    "requirements", "requirements.psd1", 0,
                    f"the scripts need the module {m.name} ({m.source}), and requirements.psd1 "
                    "does not list it, so pdt will not install it. Add it to requirements.psd1.",
                    False))

    modules = sorted(needs.values(), key=lambda m: m.name.lower())
    return ScriptScan(entries, helpers, modules, list(dict.fromkeys(findings)),
                      sorted(host.values(), key=str.lower),
                      entry_rule(files, app.get("run_scripts")), requirements is not None)


def split_files(files: list[dict], run_scripts: list[str] | None) -> tuple[list[str], list[str]]:
    """Entries are run.ps1, configured scripts, or unreferenced top-level scripts."""
    names = [f["file"] for f in files]
    run_script = next((name for name in names if name.lower() == "run.ps1"), None)
    if run_script is not None:
        return [run_script], [name for name in names if name != run_script]
    if run_scripts:
        return list(run_scripts), [n for n in names if n not in run_scripts]
    referenced = set()
    for f in files:
        for call in f["localInvocations"]:
            target = re.sub(r"^\$\w+[\\/]", "", call["target"])
            referenced.add(Path(target.replace("\\", "/")).name.lower())
    entries = [n for n in names
               if "/" not in n and n.lower().endswith(".ps1") and n.lower() not in referenced]
    return entries, [n for n in names if n not in entries]


def entry_rule(files: list[dict], run_scripts: list[str] | None) -> str:
    """Why split_files picks the entry scripts it picks, for the user."""
    if any(f["file"].lower() == "run.ps1" for f in files):
        return f"{console.value('run.ps1')} is in the folder, so only it runs"
    if run_scripts:
        return f"the order {console.value('run_scripts')} sets"
    return "each .ps1 that no other script loads, in name order"


def script_lines(entries: list[str], helpers: list[str], rule: str) -> list[str]:
    lines = [f"runs, in order: {', '.join(map(console.value, entries)) or 'no script'}"
             + (f" ({rule})" if rule else "")]
    if helpers:
        lines.append("helper files, which run only when a script loads them: "
                     f"{', '.join(map(console.value, helpers))}")
    return lines


def is_path(name: str) -> bool:
    return bool(re.search(r"[\\/]|\.psm1$|\.psd1$", name))


def windows_only_module(path: str, line: int, name: str) -> Finding:
    return Finding(
        "windows-module", path, line,
        f"the module {name} only exists on Windows, and this job runs on Linux. Set "
        "platform.provider to windows, or replace what the module does.", True)


LINUX = "and this job runs on Linux. Set platform.provider to windows, or replace it."


def command_findings(c: dict, path: str, on_windows: bool, defined: set[str]) -> list[Finding]:
    name = c["name"]
    line = c["line"]
    found = []
    if name.startswith(BOM):
        found.append(Finding(
            "bom", path, line,
            "the file starts with two byte-order marks, so PowerShell reads its first word as "
            "an unknown command. Save the file again as UTF-8.", False))
        name = name.lstrip(BOM)
    key = name.lower()
    if key in defined:
        return found
    if key in INTERACTIVE_COMMANDS:
        found.append(Finding(
            "interactive", path, line,
            f"{name} waits for a person to type, and a scheduled job has none. Remove it, "
            "or read the value from an environment variable.", True))
    if key in AZURE_AUTOMATION_COMMANDS:
        found.append(Finding(
            "azure-automation", path, line,
            f"{name} only works inside Azure Automation. Read the value from an environment "
            "variable instead.", True))
    if key in LOGIN_COMMANDS:
        shown, app_only = LOGIN_COMMANDS[key]
        given = [p for p in c["parameters"] if p.lower() in app_only]
        if not given and c.get("splatted"):
            found.append(Finding(
                "login", path, line,
                f"{name} takes its parameters from a splatted variable, so pdt cannot see "
                f"them. Make sure it signs in without a person, with -{shown}.", False))
        elif not given:
            found.append(Finding(
                "login", path, line,
                f"{name} opens a browser sign-in, and a scheduled job has no person to sign "
                f"in. Sign in as an app instead, with -{shown} and a certificate or "
                f"secret.", True))
    if key in INSTALL_COMMANDS:
        found.append(Finding(
            "install-at-run-time", path, line,
            f"{name} installs a module every run. pdt installs the modules the scripts need "
            "when it deploys, so remove it.", False))
    if key in ("invoke-expression", "iex"):
        found.append(Finding(
            "dynamic", path, line,
            "Invoke-Expression runs text as code, so pdt cannot check what it runs.", False))
    if key == "write-progress":
        found.append(Finding(
            "progress", path, line,
            "Write-Progress draws a progress bar, which a scheduled job cannot show. The "
            "job still runs.", False))
    if on_windows:
        return found
    if is_windows_command(name):
        found.append(Finding(
            "windows-command", path, line,
            f"{name} only exists on Windows, {LINUX}", True))
    if key in WINDOWS_EXES or re.search(r"\.(exe|bat|cmd|msi|vbs)$", key):
        found.append(Finding(
            "windows-program", path, line,
            f"{name} is a Windows program, {LINUX}", True))
    return found


def is_windows_command(name: str) -> bool:
    return name.lower() in WINDOWS_ONLY_COMMANDS or bool(WINDOWS_ONLY_COMMAND_PATTERN.match(name))


def windows_findings(f: dict) -> list[Finding]:
    path = f["file"]
    found = []
    for s in f["strings"]:
        if s["kind"] == "registry":
            reason = f"the registry path {s['text']} only exists on Windows, {LINUX}"
        elif s["kind"] == "cert-drive":
            reason = f"the certificate store {s['text']} only exists on Windows, {LINUX}"
        else:
            found.append(Finding(
                "windows-path", path, s["line"],
                f"{s['text']} is a Windows path, which does not exist on Linux. If the script "
                "reads or writes there, use a path inside the job's folder instead.", False))
            continue
        found.append(Finding("windows-drive", path, s["line"], reason, True))
    for t in f["types"]:
        if t["name"] and WINDOWS_TYPE_PATTERN.match(t["name"]):
            found.append(Finding(
                "windows-type", path, t["line"],
                f"the .NET type [{t['name']}] only exists on Windows, {LINUX}", True))
    for o in f["newObjects"]:
        if o["comObject"]:
            found.append(Finding(
                "com-object", path, o["line"],
                f"New-Object -ComObject {o['comObject']} needs a Windows program, {LINUX}", True))
        elif o["typeName"] and WINDOWS_TYPE_PATTERN.match(o["typeName"]):
            found.append(Finding(
                "windows-type", path, o["line"],
                f"the .NET type [{o['typeName']}] only exists on Windows, {LINUX}", True))
    for a in f["assemblies"]:
        if a["name"].lower() in WINDOWS_ASSEMBLIES:
            found.append(Finding(
                "windows-assembly", path, a["line"],
                f"the assembly {a['name']} only exists on Windows, {LINUX}", True))
    for r in f["remoting"]:
        found.append(Finding(
            "remoting", path, r["line"],
            "Invoke-Command -ComputerName uses Windows remoting (WinRM). The job's computer "
            "must be allowed to reach that computer.", False))
    for d in f["dynamic"]:
        if d["kind"] != "Invoke-Expression":
            found.append(Finding(
                "dynamic", path, d["line"],
                f"pdt cannot see which command `{d['text']}` runs.", False))
    return found


def module_for(name: str, known: str | None, declared: set[str],
               found: dict[str, list[dict]] | None) -> tuple[str | None, Finding | None] | None:
    """The module that provides `name`, and a warning when pdt had to guess. `found` holds
    the gallery answers; with None, a command that needs one returns None."""
    key = name.lower()
    if key in COMMAND_MODULES:
        return COMMAND_MODULES[key], None
    for pattern, module in COMMAND_PREFIX_MODULES:
        if pattern.match(name):
            if module not in FAMILIES or module.lower() in declared:
                return module, None
            if found is None:
                return None
            return family_module(name, module, found.get(name, []))
    if is_windows_command(name) or is_path(name):
        return None, None
    if key in WINDOWS_EXES or key in AZURE_AUTOMATION_COMMANDS or key in INTERACTIVE_COMMANDS:
        return None, None
    if known is not None:
        if known == "" or known.lower() in BUILT_IN_MODULES:
            return None, None
        return known, Finding(
            "guessed-module", "", 0,
            f"pdt found {name} in the module {known} installed on this computer; add "
            f"`#Requires -Modules {known}` to the script to be sure.", False)
    if found is None:
        return None
    microsoft = [c for c in found.get(name, []) if c["author"].startswith(MICROSOFT_AUTHORS)]
    if len(microsoft) == 1:
        module = microsoft[0]["name"]
        return module, Finding(
            "guessed-module", "", 0,
            f"pdt guessed the module {module} for {name} from the PowerShell Gallery; add "
            f"`#Requires -Modules {module}` to the script to be sure.", False)
    return None, Finding(
        "unknown-command", "", 0,
        f"pdt does not know which module provides {name}. If it comes from a module, add "
        "`#Requires -Modules <module>` to the script.", False)


def family_module(name: str, family: str, candidates: list[dict]) -> tuple[str, Finding | None]:
    hits = [c["name"] for c in candidates if c["name"].startswith(family + ".")]
    if len(hits) == 1:
        return hits[0], None
    return family, Finding(
        "guessed-module", "", 0,
        f"pdt could not find which {family} module provides {name} on the PowerShell Gallery, "
        f"so it installs all of {family}. Add `#Requires -Modules {family}.<Part>` to the "
        "script to install less.", False)


def requires_version(spec: dict) -> str | None:
    return version_range(spec.get("requiredVersion"), spec.get("version"), spec.get("maximumVersion"))


def import_version(imp: dict) -> str | None:
    return version_range(imp.get("requiredVersion"), imp.get("minimumVersion"), imp.get("maximumVersion"))


def requirements_version(spec) -> str | None:
    """A requirements.psd1 value: a version string, a `10.*` wildcard, or a hashtable."""
    if isinstance(spec, dict):
        return version_range(spec.get("RequiredVersion") or spec.get("Version"),
                             spec.get("MinimumVersion") or spec.get("ModuleVersion"),
                             spec.get("MaximumVersion"))
    text = str(spec).strip()
    if text in ("", "latest", "*"):
        return None
    if text.endswith(".*"):
        parts = text[:-2].split(".")
        upper = parts[:-1] + [str(int(parts[-1]) + 1)]
        return f"[{'.'.join(parts)},{'.'.join(upper)})"
    return text


def version_range(exact, minimum, maximum) -> str | None:
    if exact:
        return str(exact)
    if minimum and maximum:
        return f"[{minimum},{maximum}]"
    if minimum:
        return f"[{minimum},)"
    if maximum:
        return f"(,{maximum}]"
    return None


def find_in_gallery(commands: list[str]) -> dict[str, list[dict]]:
    """The gallery modules that export each command, as {name, author}, cached on disk.
    One pwsh looks up every command the cache does not hold yet."""
    cache = {}
    if GALLERY_CACHE.is_file():
        cache = json.loads(GALLERY_CACHE.read_text())
    new = [command for command in commands if command not in cache]
    if new:
        script = ("$found = [ordered]@{}; "
                  f"foreach ($name in @({', '.join(map(quoted, new))})) {{ "
                  "$found[$name] = @(Find-PSResource -CommandName $name -Repository PSGallery "
                  "-ErrorAction SilentlyContinue | ForEach-Object { $_.ParentResource } | "
                  "Select-Object Name, Author | Sort-Object Name -Unique) }; "
                  "$found | ConvertTo-Json -Depth 4")
        proc = subprocess.run([pwsh.ensure_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip() != "":
            for command, hits in json.loads(proc.stdout).items():
                cache[command] = [{"name": h["Name"], "author": h["Author"] or ""} for h in hits]
            GALLERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with locked(GALLERY_CACHE):
                write_text_atomically(GALLERY_CACHE, json.dumps(cache, indent=1))
    return {command: cache.get(command, []) for command in commands}


def latest_versions(names: list[str], lookup=None) -> dict[str, str]:
    """The newest version of each module on the PowerShell Gallery, or `latest` for a name
    the gallery does not answer. `lookup` replaces `gallery_versions`."""
    found = lower((lookup or gallery_versions)(names))
    return {name: found.get(name.lower(), "latest") for name in names}


def gallery_versions(names: list[str]) -> dict[str, str]:
    """{name: newest version} for the modules the gallery holds, from one pwsh."""
    if not names:
        return {}
    script = (f"ConvertTo-Json -InputObject @(Find-PSResource -Name {', '.join(map(quoted, names))} "
              "-Repository PSGallery -ErrorAction SilentlyContinue | "
              "Select-Object Name, @{Name = 'Version'; Expression = { \"$($_.Version)\" }})")
    proc = subprocess.run([pwsh.ensure_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)
    if proc.returncode != 0 or proc.stdout.strip() == "":
        return {}
    return {hit["Name"]: hit["Version"] for hit in json.loads(proc.stdout)}


def quoted(value: str) -> str:
    """`value` as a PowerShell single-quoted string."""
    return "'" + value.replace("'", "''") + "'"


def install_command(modules: list[ModuleNeed], folder: Path | None = None) -> str:
    """One pwsh command that installs the modules with PSResourceGet, for all users or into
    `folder`, then imports and probes them."""
    if not modules:
        return ""
    parts = ["$ErrorActionPreference = 'Stop'"]
    for m in modules:
        version = f" -Version {quoted(m.version)}" if m.version else ""
        if folder is None:
            parts.append(f"Install-PSResource -Name {quoted(m.name)}{version} -Repository PSGallery "
                         "-TrustRepository -Scope AllUsers -Quiet")
        else:
            parts.append(f"Save-PSResource -Name {quoted(m.name)}{version} -Repository PSGallery "
                         f"-TrustRepository -Path {quoted(str(folder))} -Quiet")
    for m in modules:
        parts.append(f"Import-Module {quoted(m.name)} -ErrorAction Stop")
    for prefix, probe in PROBES:
        if any(m.name == prefix or m.name.startswith(prefix + ".") for m in modules):
            parts.append(probe)
    return "; ".join(parts)


def install_for_run(app: dict, folder: Path = MODULE_DIR) -> dict[str, str]:
    """Install the modules a deploy installs, for a run on this computer, and return the env
    the run needs. A module stays as it is when the run loads it at the version asked for;
    pdt saves each other module into `folder`, which no other pwsh session reads, and puts
    `folder` first on the run's PSModulePath, so the run loads that copy."""
    modules = scan(app, app["platform"].get("provider", "")).modules
    if not modules:
        return {}
    module_path, loaded = loaded_versions([m.name for m in modules], folder)
    env = {**os.environ, "PSModulePath": module_path, "NO_COLOR": "1"}
    for m in modules:
        have = loaded.get(m.name.lower())
        if have is not None and satisfies(have, m.version):
            continue
        version = f" -Version {quoted(m.version)}" if m.version else ""
        found = "does not have it" if have is None else f"has version {have}"
        console.status(f"Installing the PowerShell module {m.name} {m.version or '(latest)'} "
                       f"into {folder}, because this computer {found}...")
        shutil.rmtree(folder / m.name, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [pwsh.ensure_pwsh(), "-NoProfile", "-NonInteractive", "-Command", install_command([m], folder)],
            capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise PowerShellError(
                f"pdt could not install the PowerShell module {m.name} from the PowerShell Gallery. "
                "Make sure this computer reaches www.powershellgallery.com and run the command again, "
                f"or install the module yourself in pwsh: Install-PSResource -Name {quoted(m.name)}"
                f"{version} -Repository PSGallery -Scope CurrentUser. pwsh said: {proc.stderr.strip()}")
    return {"PSModulePath": module_path}


def loaded_versions(names: list[str], folder: Path) -> tuple[str, dict[str, str]]:
    """pwsh's own PSModulePath with `folder` first, and {lower name: version} of the module
    Import-Module NAME loads from it: the newest version in the first path entry that holds
    it. pwsh keeps an inherited PSModulePath as it is when it already holds pwsh's own entries."""
    script = (f"$env:PSModulePath = {quoted(str(folder))} + [IO.Path]::PathSeparator + $env:PSModulePath; "
              "ConvertTo-Json -Depth 3 -InputObject ([ordered]@{ path = $env:PSModulePath; modules = @("
              f"Get-Module -ListAvailable -Name {', '.join(map(quoted, names))} "
              "-ErrorAction SilentlyContinue | Select-Object Name, ModuleBase, "
              "@{Name = 'Version'; Expression = { \"$($_.Version)\" }}) })")
    proc = subprocess.run([pwsh.ensure_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)
    if proc.returncode != 0 or proc.stdout.strip() == "":
        raise PowerShellError(f"pdt could not list the PowerShell modules on this computer: "
                              f"{proc.stderr.strip()}")
    found = json.loads(proc.stdout)
    entries = [Path(entry) for entry in found["path"].split(os.pathsep) if entry]
    best: dict[str, tuple] = {}
    for module in found["modules"]:
        base = Path(module["ModuleBase"])
        place = next((i for i, entry in enumerate(entries) if base.is_relative_to(entry)), len(entries))
        rank = (place, *(-part for part in version_key(module["Version"])))
        key = module["Name"].lower()
        if key not in best or rank < best[key][0]:
            best[key] = (rank, module["Version"])
    return found["path"], {key: version for key, (_rank, version) in best.items()}


def version_key(text: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", text.split("-")[0])]
    return tuple(parts + [0] * (4 - len(parts)))


def satisfies(version: str, wanted: str | None) -> bool:
    """Whether `version` meets `wanted`: None, an exact version, or a NuGet range such as
    `[7.0,8.0)`, as Install-PSResource -Version reads it."""
    if wanted is None:
        return True
    have = version_key(version)
    if wanted[0] not in "[(":
        return have == version_key(wanted)
    inner = wanted[1:-1]
    if "," not in inner:
        return have == version_key(inner)
    low, high = (part.strip() for part in inner.split(",", 1))
    if low and (have < version_key(low) or (wanted[0] == "(" and have == version_key(low))):
        return False
    if high and (have > version_key(high) or (wanted[-1] == ")" and have == version_key(high))):
        return False
    return True


def report(scan: ScriptScan) -> tuple[list[str], list[str]]:
    """The certain findings as problems and the rest as warnings, each as `file:line: reason`."""
    problems = []
    warnings = []
    for f in scan.findings:
        where = f.file
        if f.line:
            where += f":{f.line}"
        text = f"{where}: {f.reason}" if where else f.reason
        (problems if f.certain else warnings).append(text)
    return problems, warnings


def summary_lines(scan: ScriptScan) -> list[str]:
    """What the scan found, with where each finding came from, one fact per line."""
    lines = script_lines(scan.entries, scan.helpers, scan.entry_rule)
    if scan.from_requirements:
        heading = f"modules to install, as {console.value('requirements.psd1')} lists them:"
    else:
        heading = "modules to install, found in the scripts:"
    modules = []
    for m in scan.modules:
        version = console.value(m.version) if m.version else "latest"
        if scan.from_requirements:
            modules.append((m.name, version))
            continue
        how, _, file = m.source.rpartition(" in ")
        if how.startswith("command "):
            how = f"command {console.value(how.removeprefix('command '))}"
        modules.append((m.name, f"{version}, {how} in {console.value(file)}"))
    return lines + (console.rows(heading, modules) if modules else [f"{heading} none"])
