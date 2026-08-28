from __future__ import annotations

import getpass
import hashlib
import html
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from pdt import config, console
from pdt.deploy import confirm
from pdt.deploy_common import CostEstimate, gather_secrets, stage_build_context
from pdt.deploy_windows import WindowsDeployError, _task_name, schedule_trigger


UV_VERSION = "0.12.4"
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.12.4/uv-x86_64-pc-windows-msvc.zip"
UV_SHA256 = "4f3b7d63cd81fca0da5a655d973d20affca89ce6e5f9a29fd0183cc4204a7639"


@dataclass(frozen=True)
class RemoteWindowsDeployment:
    host: str
    app_name: str
    task_name: str
    app_root: PureWindowsPath
    release_root: PureWindowsPath
    uv_path: PureWindowsPath
    artifact_digest: str
    schedule_description: str
    trigger_xml: str


@dataclass(frozen=True)
class RemoteWindowsState:
    task: str
    app_directory: str
    active_release: PureWindowsPath | None
    running_release: PureWindowsPath | None
    files_match: bool
    secrets_match: bool
    uv_matches: bool
    other_managed_apps: int


@dataclass(frozen=True)
class AdministratorCredentials:
    username: str
    password: str = field(repr=False)


class RemoteWindowsHost:
    def __init__(self, client, pool) -> None:
        self.client = client
        self.pool = pool

    @classmethod
    def connect(cls, host: str, credentials: AdministratorCredentials) -> "RemoteWindowsHost":
        try:
            from pypsrp.client import Client
            from pypsrp.powershell import RunspacePool
        except ImportError as exc:
            raise WindowsDeployError(
                "remote Windows deployment needs pypsrp==0.9.1; run pdt through uv") from exc
        if "://" in host:
            raise WindowsDeployError(
                "platform.host must be a bare Windows host address, without http:// or https://")
        address = host
        client = None
        pool = None
        try:
            client = Client(
                address, username=credentials.username, password=credentials.password,
                ssl=False, auth="ntlm", encryption="always", cert_validation=True,
            )
            pool = RunspacePool(client.wsman)
            pool.open()
        except Exception as exc:
            if pool is not None:
                pool.close()
            if client is not None:
                client.wsman.close()
            raise WindowsDeployError(
                f"cannot connect to Windows host {host!r} with WinRM NTLM: {exc}. "
                "Enable-PSRemoting -Force must run in an elevated PowerShell, and "
                "Windows Remote Management must allow the connection.") from exc
        return cls(client, pool)

    def _run(self, script: str, **parameters):
        try:
            from pypsrp.powershell import PowerShell
            with PowerShell(self.pool) as powershell:
                powershell.add_script(script)
                powershell.add_parameters(parameters)
                output = powershell.invoke()
                errors = list(powershell.streams.error)
        except Exception as exc:
            raise WindowsDeployError(f"remote Windows command failed: {exc}") from exc
        if errors:
            detail = str(errors[0])
            raise WindowsDeployError(f"remote Windows command failed: {detail}")
        return output

    def close(self) -> None:
        try:
            self.pool.close()
        finally:
            self.client.wsman.close()

    def _copy(self, source: Path, destination: PureWindowsPath) -> None:
        try:
            self.client.copy(str(source), str(destination))
        except Exception as exc:
            raise WindowsDeployError(f"could not upload {source.name}: {exc}") from exc

    def desired(self, host: str, app: dict, stage: Path) -> RemoteWindowsDeployment:
        digest = _tree_digest(stage)
        description, trigger = _remote_trigger(app)
        task_name = _task_name(app["name"])
        app_root = PureWindowsPath(r"C:\ProgramData\pdt\apps") / task_name
        return RemoteWindowsDeployment(
            host=host, app_name=app["name"], task_name=task_name,
            app_root=app_root, release_root=app_root / "releases" / digest,
            uv_path=PureWindowsPath(r"C:\ProgramData\pdt\bin\uv.exe"),
            artifact_digest=digest, schedule_description=description, trigger_xml=trigger,
        )

    def inspect(self, desired: RemoteWindowsDeployment, secret_json: str) -> RemoteWindowsState:
        result = self._run(_INSPECT_SCRIPT, app_root=str(desired.app_root),
                           app_name=desired.app_name, task_name=desired.task_name,
                           artifact_digest=desired.artifact_digest,
                           secret_digest=_digest(secret_json), uv_version=UV_VERSION)
        if not result:
            raise WindowsDeployError("remote Windows host did not return deployment state")
        raw = result[-1] if isinstance(result, list) else result
        state = json.loads(str(raw))
        return RemoteWindowsState(
            task=state["task"], app_directory=state["app_directory"],
            active_release=_path_or_none(state.get("active_release")),
            running_release=_path_or_none(state.get("running_release")),
            files_match=bool(state["files_match"]), secrets_match=bool(state["secrets_match"]),
            uv_matches=bool(state["uv_matches"]),
            other_managed_apps=int(state["other_managed_apps"]),
        )

    def reconcile(self, desired: RemoteWindowsDeployment, state: RemoteWindowsState,
                  stage: Path, secret_json: str) -> None:
        _require_managed(state, desired)
        if not state.files_match and desired.release_root in (
                state.active_release, state.running_release):
            raise WindowsDeployError(
                f"release {desired.release_root} is active or running and cannot be replaced")
        if not state.files_match:
            incoming = desired.app_root / f"incoming-{desired.artifact_digest}"
            self._run(_PREPARE_INCOMING_SCRIPT, app_root=str(desired.app_root),
                      incoming=str(incoming), release_root=str(desired.release_root),
                      app_name=desired.app_name, task_name=desired.task_name)
        if not state.uv_matches:
            self._run(_UV_SCRIPT, uv_url=UV_URL, uv_sha256=UV_SHA256,
                      uv_path=str(desired.uv_path), uv_version=UV_VERSION)
        if not state.files_match:
            archive = _archive(stage)
            try:
                self._copy(archive, incoming / "context.zip")
            finally:
                archive.unlink(missing_ok=True)
            self._run(_ACTIVATE_RELEASE_SCRIPT, incoming=str(incoming),
                      release_root=str(desired.release_root), app_name=desired.app_name,
                      task_name=desired.task_name, secret_json=secret_json,
                      uv_path=str(desired.uv_path))
        elif not state.secrets_match:
            self._run(_WRITE_SECRET_SCRIPT, release_root=str(desired.release_root),
                      secret_json=secret_json)
        self._run(_REGISTER_TASK_SCRIPT, task_name=desired.task_name,
                  task_xml=_task_xml(desired))
        self._remove_stale(desired)

    def _remove_stale(self, desired: RemoteWindowsDeployment) -> None:
        self._run(_REMOVE_STALE_SCRIPT, app_root=str(desired.app_root),
                  release_root=str(desired.release_root))

    def remove(self, desired: RemoteWindowsDeployment, state: RemoteWindowsState) -> None:
        _require_managed(state, desired)
        self._run(_REMOVE_SCRIPT, app_root=str(desired.app_root), app_name=desired.app_name,
                  task_name=desired.task_name)


def deploy_remote(app: dict, host: str, assume_yes: bool) -> int:
    remote = None
    stage = None
    try:
        credentials = _credentials(host)
        remote = RemoteWindowsHost.connect(host, credentials)
        secret_json = json.dumps(gather_secrets(app), sort_keys=True)
        stage = stage_build_context(app)
        desired = remote.desired(host, app, stage)
        state = remote.inspect(desired, secret_json)
        _require_managed(state, desired)
        actions = deployment_plan(desired, state)
        cost = CostEstimate([(f"Task Scheduler on Windows host {host}", 0.0)],
                            "no cloud charges")
        if not confirm(actions, assume_yes, cost):
            console.warn("Aborted; nothing was changed.")
            return 1
        remote.reconcile(desired, state, stage, secret_json)
    except (config.ConfigError, WindowsDeployError) as exc:
        console.error(str(exc))
        return 1
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        if remote is not None:
            remote.close()
    console.done(f"Deployed {app['name']} to Windows host {host} as task {desired.task_name}.")
    return 0


def destroy_remote(app: dict, host: str, assume_yes: bool) -> int:
    remote = None
    try:
        credentials = _credentials(host)
        remote = RemoteWindowsHost.connect(host, credentials)
        desired = remote_identity(host, app["name"])
        state = remote.inspect(desired, "")
        _require_managed(state, desired)
        if state.task == "absent" and state.app_directory == "absent":
            console.done(f"Nothing to remove for {app['name']} on Windows host {host}.")
            return 0
        if not confirm(destroy_plan(desired, state), assume_yes):
            console.warn("Aborted; nothing was changed.")
            return 1
        remote.remove(desired, state)
    except (config.ConfigError, WindowsDeployError) as exc:
        console.error(str(exc))
        return 1
    finally:
        if remote is not None:
            remote.close()
    console.done(f"Removed {app['name']} from Windows host {host}.")
    return 0


def deployment_plan(desired: RemoteWindowsDeployment, state: RemoteWindowsState) -> list[str]:
    actions = [f"Windows host: {desired.host}"]
    if not state.uv_matches:
        actions.append(f"install uv {UV_VERSION} at {desired.uv_path}")
    if not state.files_match:
        actions.append(f"upload release {desired.release_root}")
    if not state.secrets_match:
        actions.append(f"write protected secrets at {desired.release_root / 'pdt-env.bin'}")
    actions.extend([
        f"register Windows scheduled task {desired.task_name} (runs as SYSTEM)",
        f"run {desired.app_name} {desired.schedule_description} (machine local time)",
    ])
    return actions


def destroy_plan(desired: RemoteWindowsDeployment, state: RemoteWindowsState) -> list[str]:
    actions = [f"Windows host: {desired.host}"]
    if state.task == "managed":
        actions.append(f"stop and delete Windows scheduled task {desired.task_name}")
    if state.app_directory == "managed":
        actions.append(f"delete app directory {desired.app_root}")
    if state.other_managed_apps == 0:
        actions.append(r"delete shared directory C:\ProgramData\pdt\bin")
    return actions


def remote_identity(host: str, app_name: str) -> RemoteWindowsDeployment:
    task_name = _task_name(app_name)
    app_root = PureWindowsPath(r"C:\ProgramData\pdt\apps") / task_name
    return RemoteWindowsDeployment(
        host=host, app_name=app_name, task_name=task_name, app_root=app_root,
        release_root=app_root / "releases", uv_path=PureWindowsPath(
            r"C:\ProgramData\pdt\bin\uv.exe"), artifact_digest="",
        schedule_description="", trigger_xml="")


def _credentials(host: str) -> AdministratorCredentials:
    username = console.ask(f"Administrator username for {host}").strip()
    if username == "":
        raise WindowsDeployError("an administrator username is needed")
    password = getpass.getpass(f"Administrator password for {host}: ")
    if password == "":
        raise WindowsDeployError("an administrator password is needed")
    return AdministratorCredentials(username, password)


def _remote_trigger(app: dict) -> tuple[str, str]:
    timezone = str(app.get("timezone") or "").strip().lower()
    if timezone != "local":
        raise WindowsDeployError(
            "the Windows provider uses the machine's local timezone; set timezone: local for this app")
    return schedule_trigger(config.cron_expression(app["schedule"]))


def _task_xml(desired: RemoteWindowsDeployment) -> str:
    wrapper = desired.release_root / "run.ps1"
    description = html.escape(
        f"Managed by pdt remote; app={desired.app_name}; root={desired.app_root}; "
        f"release={desired.release_root}")
    wrapper_text = html.escape(str(wrapper))
    release_text = html.escape(str(desired.release_root))
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{description}</Description></RegistrationInfo>
  <Triggers>{desired.trigger_xml}</Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File &quot;{wrapper_text}&quot;</Arguments><WorkingDirectory>{release_text}</WorkingDirectory></Exec></Actions>
</Task>'''


def _require_managed(state: RemoteWindowsState, desired: RemoteWindowsDeployment) -> None:
    if state.task == "unmanaged":
        raise WindowsDeployError(
            f"Windows scheduled task {desired.task_name} exists but is not managed by PDT")
    if state.app_directory == "unmanaged":
        raise WindowsDeployError(
            f"remote directory {desired.app_root} exists but is not managed by PDT")


def _archive(stage: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix="pdt-windows-", suffix=".zip")
    os.close(fd)
    archive = Path(name)
    archive.unlink()
    shutil.make_archive(str(archive.with_suffix("")), "zip", stage)
    return archive


def _tree_digest(stage: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in stage.rglob("*") if path.is_file()):
        digest.update(path.relative_to(stage).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _path_or_none(value: str | None) -> PureWindowsPath | None:
    return PureWindowsPath(value) if value else None


_UV_SCRIPT = r'''param($uv_url, $uv_sha256, $uv_path, $uv_version)
$architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE')
if ($architecture -notin @('AMD64', 'x86_64')) { throw "PDT supports only x86_64 Windows hosts; this host reports $architecture" }
if ((Test-Path $uv_path) -and ((& $uv_path --version) -eq ('uv ' + $uv_version))) { return }
$bin = Split-Path $uv_path -Parent
$incoming = Join-Path $bin 'incoming-uv'
$archive = Join-Path $incoming 'uv.zip'
Remove-Item $incoming -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $incoming | Out-Null
Invoke-WebRequest -Uri $uv_url -OutFile $archive -UseBasicParsing
$actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $uv_sha256) { throw "uv download SHA-256 mismatch: expected $uv_sha256, got $actual" }
Expand-Archive $archive -DestinationPath $incoming -Force
$downloaded = Get-ChildItem $incoming -Filter uv.exe -Recurse | Select-Object -First 1 -ExpandProperty FullName
if ($null -eq $downloaded) { throw 'uv archive did not contain uv.exe' }
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Move-Item $downloaded $uv_path -Force
Remove-Item $incoming -Recurse -Force
if ((& $uv_path --version) -ne ('uv ' + $uv_version)) { throw "installed uv does not report version $uv_version" }
'''

_INSPECT_SCRIPT = r'''param($app_root, $app_name, $task_name, $artifact_digest, $secret_digest, $uv_version)
$root = Split-Path $app_root -Parent | Split-Path -Parent
$rootMarker = Join-Path $root 'pdt.json'
$appMarker = Join-Path $app_root 'pdt.json'
$task = Get-ScheduledTask -TaskName $task_name -TaskPath '\' -ErrorAction SilentlyContinue
$prefix = "Managed by pdt remote; app=$app_name; root=$app_root; release="
$taskState = if ($null -eq $task) { 'absent' } elseif ($task.Description.StartsWith($prefix)) { 'managed' } else { 'unmanaged' }
$appState = 'absent'
if (Test-Path $app_root) { $appState = 'unmanaged'; if ((Test-Path $rootMarker) -and (Test-Path $appMarker)) { $rootInfo = Get-Content $rootMarker -Raw | ConvertFrom-Json; $appInfo = Get-Content $appMarker -Raw | ConvertFrom-Json; if ($rootInfo.owner -eq 'pdt' -and $rootInfo.schema -eq 1 -and $appInfo.owner -eq 'pdt' -and $appInfo.app -eq $app_name -and $appInfo.task -eq $task_name -and $appInfo.path -eq $app_root) { $appState = 'managed' } } }
$release = Join-Path $app_root ('releases\' + $artifact_digest)
$active = if ($taskState -eq 'managed' -and $task.Description -match 'release=([^;]+)$') { $matches[1] } else { $null }
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match [regex]::Escape($app_root) + '\\releases\\([^\\]+)\\run.ps1' } | Select-Object -First 1 -ExpandProperty CommandLine
if ($running -match [regex]::Escape($app_root) + '\\releases\\([^\\]+)\\run.ps1') { $running = Join-Path (Join-Path $app_root 'releases') $matches[1] } else { $running = $null }
$manifest = Join-Path $release 'manifest.json'
$files = (Test-Path $manifest) -and ((Get-Content $manifest -Raw | ConvertFrom-Json).artifact_digest -eq $artifact_digest)
$secrets = $false
if ($files -and (Test-Path (Join-Path $release 'pdt-env.bin'))) { $encrypted = [IO.File]::ReadAllBytes((Join-Path $release 'pdt-env.bin')); $plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine); $sha256 = New-Object Security.Cryptography.SHA256Managed; try { $actual = ($sha256.ComputeHash($plain) | ForEach-Object { $_.ToString('x2') }) -join ''; $secrets = $actual -eq $secret_digest } finally { $sha256.Dispose() } }
$uv = (Test-Path (Join-Path $root 'bin\uv.exe')) -and ((& (Join-Path $root 'bin\uv.exe') --version) -eq ('uv ' + $uv_version))
$other = @(Get-ChildItem (Join-Path $root 'apps') -Directory -ErrorAction SilentlyContinue | Where-Object { $_.FullName -ne $app_root -and (Test-Path (Join-Path $_.FullName 'pdt.json')) }).Count
@{task=$taskState;app_directory=$appState;active_release=$active;running_release=$running;files_match=$files;secrets_match=$secrets;uv_matches=$uv;other_managed_apps=$other}|ConvertTo-Json -Compress
'''

_PREPARE_INCOMING_SCRIPT = r'''param($app_root, $incoming, $release_root, $app_name, $task_name)
$root = Split-Path $app_root -Parent | Split-Path -Parent
$rootMarker = Join-Path $root 'pdt.json'
if (Test-Path $root) { if (-not (Test-Path $rootMarker)) { throw 'PDT root ownership marker is missing' }; $rootInfo = Get-Content $rootMarker -Raw | ConvertFrom-Json; if ($rootInfo.owner -ne 'pdt' -or $rootInfo.schema -ne 1) { throw 'PDT root ownership marker does not match' } } else { New-Item -ItemType Directory -Force -Path $root | Out-Null; @{owner='pdt';schema=1}|ConvertTo-Json -Compress|Set-Content $rootMarker -NoNewline }
New-Item -ItemType Directory -Force -Path (Join-Path $root 'apps'), $app_root | Out-Null
@{owner='pdt';app=$app_name;task=$task_name;path=$app_root}|ConvertTo-Json -Compress|Set-Content (Join-Path $app_root 'pdt.json') -NoNewline
Remove-Item $incoming -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $release_root -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $incoming | Out-Null
'''

_ACTIVATE_RELEASE_SCRIPT = r'''param($incoming, $release_root, $app_name, $task_name, $secret_json, $uv_path)
if (-not (Test-Path $release_root)) { Expand-Archive (Join-Path $incoming 'context.zip') -DestinationPath $incoming -Force; Remove-Item (Join-Path $incoming 'context.zip') -Force; Move-Item $incoming $release_root }
$bytes = [Text.Encoding]::UTF8.GetBytes($secret_json)
$encrypted = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
$secret = Join-Path $release_root 'pdt-env.bin'
[IO.File]::WriteAllBytes($secret, $encrypted)
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($identity in @('SYSTEM', 'Administrators')) { $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow'))) }
Set-Acl $secret $acl
@{owner='pdt';schema=1;app=$app_name;task=$task_name;artifact_digest=(Split-Path $release_root -Leaf)}|ConvertTo-Json -Compress|Set-Content (Join-Path $release_root 'manifest.json') -NoNewline
@'
$release = Split-Path $PSCommandPath -Parent
$encrypted = [IO.File]::ReadAllBytes((Join-Path $release 'pdt-env.bin'))
$plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
$env:PDT_ENV_JSON = [Text.Encoding]::UTF8.GetString($plain)
$env:PDT_PROJECT = $release
Set-Location (Join-Path $release 'APP_NAME')
& 'UV_PATH' run --script run.py
exit $LASTEXITCODE
'@.Replace('UV_PATH', $uv_path.Replace("'", "''")).Replace('APP_NAME', $app_name.Replace("'", "''")) | Set-Content (Join-Path $release 'run.ps1') -NoNewline
'''

_WRITE_SECRET_SCRIPT = r'''param($release_root, $secret_json)
$encrypted = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($secret_json), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
$secret = Join-Path $release_root 'pdt-env.bin'; [IO.File]::WriteAllBytes($secret, $encrypted)
$acl = New-Object Security.AccessControl.FileSecurity; $acl.SetAccessRuleProtection($true, $false)
foreach ($identity in @('SYSTEM', 'Administrators')) { $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow'))) }; Set-Acl $secret $acl
'''

_REGISTER_TASK_SCRIPT = r'''param($task_name, $task_xml)
Register-ScheduledTask -TaskName $task_name -Xml $task_xml -Force -ErrorAction Stop | Out-Null
'''

_REMOVE_STALE_SCRIPT = r'''param($app_root, $release_root)
$releases = Join-Path $app_root 'releases'
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match [regex]::Escape($app_root) + '\\releases\\([^\\]+)\\run.ps1' } | Select-Object -First 1 -ExpandProperty CommandLine
if ($running -match [regex]::Escape($app_root) + '\\releases\\([^\\]+)\\run.ps1') { $runningRelease = Join-Path $releases $matches[1] } else { $runningRelease = '' }
Get-ChildItem $releases -Directory -ErrorAction SilentlyContinue | Where-Object { $_.FullName -ne $release_root -and $_.FullName -ne $runningRelease } | Remove-Item -Recurse -Force
'''

_REMOVE_SCRIPT = r'''param($app_root, $app_name, $task_name)
$root = Split-Path $app_root -Parent | Split-Path -Parent
$task = Get-ScheduledTask -TaskName $task_name -TaskPath '\' -ErrorAction SilentlyContinue
$prefix = "Managed by pdt remote; app=$app_name; root=$app_root; release="
if ($null -ne $task -and -not $task.Description.StartsWith($prefix)) { throw 'PDT task ownership does not match this app' }
if (Test-Path $root) { $rootMarker = Join-Path $root 'pdt.json'; if (-not (Test-Path $rootMarker)) { throw 'PDT root ownership marker is missing' }; $rootInfo = Get-Content $rootMarker -Raw | ConvertFrom-Json; if ($rootInfo.owner -ne 'pdt' -or $rootInfo.schema -ne 1) { throw 'PDT root ownership marker does not match' } }
if (Test-Path $app_root) { $appInfo = Get-Content (Join-Path $app_root 'pdt.json') -Raw | ConvertFrom-Json; if ($appInfo.owner -ne 'pdt' -or $appInfo.app -ne $app_name -or $appInfo.task -ne $task_name -or $appInfo.path -ne $app_root) { throw 'PDT ownership markers do not match this app' } }
if ($null -ne $task) { Stop-ScheduledTask -TaskName $task_name -ErrorAction SilentlyContinue; $wait = [Diagnostics.Stopwatch]::StartNew(); do { Start-Sleep -Milliseconds 250; $task = Get-ScheduledTask -TaskName $task_name -TaskPath '\' -ErrorAction SilentlyContinue; if ($wait.Elapsed.TotalSeconds -ge 60 -and $null -ne $task -and $task.State -eq 'Running') { throw "Windows task $task_name did not stop within 60 seconds" } } while ($null -ne $task -and $task.State -eq 'Running'); Unregister-ScheduledTask -TaskName $task_name -Confirm:$false -ErrorAction Stop }
if (Test-Path $app_root) { Remove-Item $app_root -Recurse -Force -ErrorAction Stop }
$apps = Join-Path $root 'apps'
$other = 0
if (Test-Path $apps) { $other = @(Get-ChildItem $apps -Directory | Where-Object { $marker = Join-Path $_.FullName 'pdt.json'; if (-not (Test-Path $marker)) { return $false }; (Get-Content $marker -Raw | ConvertFrom-Json).owner -eq 'pdt' }).Count }
if ($other -eq 0) { Remove-Item (Join-Path $root 'bin') -Recurse -Force -ErrorAction SilentlyContinue; if ((Test-Path $apps) -and -not @(Get-ChildItem $apps -Force).Count) { Remove-Item $apps -Force }; Remove-Item (Join-Path $root 'pdt.json') -Force -ErrorAction SilentlyContinue; if ((Test-Path $root) -and -not @(Get-ChildItem $root -Force).Count) { Remove-Item $root -Force } }
'''
