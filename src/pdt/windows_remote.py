"""Windows gives every administrator an elevated token over SSH. Over WinRM,
Windows gives a local account other than the built-in Administrator a filtered
token (UAC remote restrictions); pdt never changes that policy, so such an
account works only over SSH.

Over HTTP the messages are encrypted, but NTLM does not prove the PC's
identity; naming the PC in `platform.host` is the trust Microsoft asks for
with TrustedHosts.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Callable

from pdt import console, windows_uv
from pdt.powershell import quoted
from pdt.utils.email_auth import can_prompt

SSH_PORT = 22
WINRM_PORTS = ((5986, True), (5985, False))
CONNECT_TIMEOUT = 10
PROBE_MARK = "pdt-probe"
PROBE_SCRIPT = (
    "$p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(); "
    "$uv = Join-Path $env:ProgramData 'pdt\\bin\\uv.exe'; $v = '-'; "
    "if (Test-Path $uv) { $v = ((& $uv --version) -split ' ')[1] }; "
    f"'{PROBE_MARK}', $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator), "
    "$p.Identity.Name, $env:COMPUTERNAME, $env:ProgramData, $env:PROCESSOR_ARCHITECTURE, $v "
    "-join '|'"
)


class RemoteError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    user: str
    name: str

    @classmethod
    def parse(cls, host: str) -> "Target":
        user, _, name = host.strip().rpartition("@")
        return cls(user, name)


@dataclass(frozen=True)
class Connection:
    target: Target
    transport: str
    account: str
    computer: str
    program_data: str
    processor: str
    uv_version: str
    notes: tuple[str, ...] = ()
    kind: str = "ssh"
    port: int = SSH_PORT
    ssl: bool = False
    password: str | None = field(default=None, repr=False)


def encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell_args(script: str) -> list[str]:
    return ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded(script)]


def ssh_args(target: Target, script: str, terminal: bool) -> list[str]:
    args = ["ssh", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if not terminal:
        args += ["-o", "BatchMode=yes"]
    if target.user:
        args += ["-l", target.user]
    return args + ["--", target.name] + powershell_args(script)


def _probe_result(target: Target, transport: str, output: str) -> Connection | str:
    line = next((line for line in output.splitlines() if line.startswith(PROBE_MARK + "|")), None)
    fields = line.strip().split("|") if line is not None else []
    if len(fields) != 7 or fields[1] not in ("True", "False") or "" in fields:
        last = output.strip().splitlines()[-1:] or ["no output"]
        return f"the PC answered, but PowerShell did not run ({last[0]})"
    _mark, admin, account, computer, program_data, processor, uv_version = fields
    if admin != "True":
        return (f"{account} signed in, but Windows did not give the session administrator "
                "rights. An administrator account is needed")
    return Connection(target, transport, account, computer, program_data, processor, uv_version)


SSH_REASONS = (
    ("could not resolve hostname", "this computer cannot find a PC named {name}"),
    ("connection refused", "port 22 refused the connection, so OpenSSH Server is off or blocked"),
    ("timed out", "port 22 did not answer within 10 seconds"),
    ("permission denied", "the PC refused the sign-in for {user}"),
    ("remote host identification has changed",
     "the PC's SSH host key changed since the last connection; ask your IT team why"),
    ("host key verification failed", "this computer could not check the PC's SSH host key"),
)


def probe_ssh(target: Target, run: Callable = subprocess.run) -> Connection | str:
    if shutil.which("ssh") is None:
        return "this computer has no ssh command"
    try:
        proc = run(ssh_args(target, PROBE_SCRIPT, can_prompt(None)), stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "the PC did not finish the sign-in within 2 minutes"
    if proc.returncode == 0:
        return _probe_result(target, f"SSH (port {SSH_PORT})", proc.stdout)
    error = proc.stderr.strip()
    for found, reason in SSH_REASONS:
        if found in error.lower():
            return reason.format(name=target.name, user=target.user or "your user name")
    return (error.splitlines() or [f"ssh exit {proc.returncode}"])[-1]


def port_problem(name: str, port: int) -> str:
    try:
        with socket.create_connection((name, port), timeout=CONNECT_TIMEOUT):
            return ""
    except socket.gaierror:
        return f"this computer cannot find a PC named {name}"
    except OSError:
        return f"port {port} did not answer"


def winrm_client(name: str, port: int, ssl: bool, user: str, password: str | None):
    """Negotiate over HTTPS. Kerberos only over HTTP, because NTLM over HTTP does not prove
    the PC's identity."""
    from pypsrp.client import Client

    return Client(name, port=port, ssl=ssl, auth="negotiate" if ssl else "kerberos",
                  username=user or None, password=password, cert_validation=True,
                  connection_timeout=CONNECT_TIMEOUT)


NO_CREDENTIALS = ("NoCredentialError", "CredentialsExpiredError", "BadMechanismError",
                  "SpnegoError")
REJECTED = ("AuthenticationError", "InvalidCredentialError")


def _winrm_failure(exc: Exception, target: Target, port: int, ssl: bool) -> tuple[str, str]:
    """(kind, reason): kind is "missing" or "rejected" when a password could help, else ""."""
    kind = type(exc).__name__
    if kind == "SSLError":
        if "certificate verify failed" in str(exc).lower():
            return "", f"port {port} uses an HTTPS certificate that this computer does not trust"
        return "", f"port {port}: HTTPS failed ({exc})"
    if kind in REJECTED or (kind == "WinRMTransportError" and getattr(exc, "code", None) == 401):
        if target.user:
            return "rejected", f"the PC refused the sign-in for {target.user}"
        return "rejected", ("the PC refused a sign-in with this computer's Kerberos ticket; put "
                            f"the user name in platform.host, for example admin@{target.name}")
    if kind in NO_CREDENTIALS:
        if ssl:
            return "missing", (f"port {port}: this computer has no Kerberos ticket for the PC, "
                               "and no password was given")
        return "missing", (f"port {port} is HTTP, where pdt signs in only with Kerberos, because "
                           "NTLM over HTTP does not prove the PC's identity; this computer has no "
                           "Kerberos ticket for the PC")
    return "", f"port {port}: {exc}"


def probe_winrm(target: Target, check_port: Callable = port_problem,
                client: Callable = winrm_client,
                ask_password: Callable[[str], str] = getpass.getpass) -> Connection | str:
    reasons = []
    password = None
    for port, ssl in WINRM_PORTS:
        problem = check_port(target.name, port)
        if problem != "":
            reasons.append(problem)
            continue
        transport = (f"WinRM (HTTPS, port {port})" if ssl else
                     f"WinRM (HTTP, port {port}, Kerberos, encrypted messages)")
        while True:
            try:
                output, streams, had_errors = client(
                    target.name, port, ssl, target.user, password).execute_ps(PROBE_SCRIPT)
            except Exception as exc:
                kind, reason = _winrm_failure(exc, target, port, ssl)
                if kind != "" and password is None and target.user and can_prompt(None):
                    password = ask_password(f"Password for {target.user} on {target.name}: ")
                    continue
                reasons.append(reason)
                if kind == "rejected" and password is not None:
                    return "; ".join(dict.fromkeys(reasons))
                break
            if had_errors:
                output += "\n" + "\n".join(str(error) for error in streams.error)
            result = _probe_result(target, transport, output)
            if isinstance(result, Connection):
                return replace(result, notes=tuple(dict.fromkeys(reasons)), kind="winrm",
                               port=port, ssl=ssl, password=password)
            return result
    return "; ".join(dict.fromkeys(reasons))


PROBES = (("SSH", probe_ssh), ("WinRM", probe_winrm))


def connect(host: str, app_name: str, probes=PROBES) -> Connection:
    target = Target.parse(host)
    console.status(f"Connecting to {console.value(target.name)}...")
    failed = []
    for name, probe in probes:
        result = probe(target)
        if isinstance(result, Connection):
            for other, reason in failed:
                console.note(f"{console.escape(other)} did not work: {console.escape(reason)}.")
            for reason in result.notes:
                console.note(f"{console.escape(name)}: {console.escape(reason)}.")
            console.done(f"Connected to {console.value(result.computer)} over "
                         f"{console.escape(result.transport)} as {console.value(result.account)} "
                         "(administrator).")
            return result
        failed.append((name, result))
    reasons = "\n".join(f"  {name}: {reason}." for name, reason in failed)
    raise RemoteError(
        f"pdt could not sign in to {target.name} as an administrator.\n{reasons}\n"
        f"Ask the PC's IT team to allow OpenSSH Server or WinRM for an administrator account "
        f"on {target.name}, then run pdt login {app_name} again.")


SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
SET_ACL = (
    "function Set-PdtAcl([string]$Path, [bool]$UsersRead) { "
    "$item = Get-Item -LiteralPath $Path -Force; "
    "$acl = if ($item.PSIsContainer) { New-Object Security.AccessControl.DirectorySecurity } "
    "else { New-Object Security.AccessControl.FileSecurity }; "
    "$inherit = if ($item.PSIsContainer) { 'ContainerInherit,ObjectInherit' } else { 'None' }; "
    "$acl.SetAccessRuleProtection($true, $false); "
    f"$acl.SetOwner([Security.Principal.SecurityIdentifier]'{ADMINISTRATORS}'); "
    f"$rules = @(@('{SYSTEM}', 'FullControl'), @('{ADMINISTRATORS}', 'FullControl')); "
    f"if ($UsersRead) {{ $rules += ,@('{USERS}', 'ReadAndExecute') }}; "
    "foreach ($rule in $rules) { $acl.AddAccessRule((New-Object "
    "Security.AccessControl.FileSystemAccessRule([Security.Principal.SecurityIdentifier]$rule[0], "
    "$rule[1], $inherit, 'None', 'Allow'))) }; "
    "Set-Acl -LiteralPath $Path -AclObject $acl }; "
)


def set_acl(path: str, users_read: bool) -> str:
    """PowerShell that gives `path` exactly one ACL in one write: owner Administrators, full
    control for SYSTEM and Administrators, and read for Users when `users_read`. The script
    must start with SET_ACL."""
    return f"Set-PdtAcl {path} ${'true' if users_read else 'false'}; "


def replace_file(source: str, target, users_read: bool) -> str:
    """PowerShell that puts the file at the PowerShell path `source` at `target` in one rename,
    so a task never finds `target` missing or half written, then gives it the exact ACL.
    File.Replace keeps the old file's ACL, and [NullString] because PowerShell passes $null
    to a string parameter as an empty string, which File.Replace rejects."""
    return (f"$target = {quoted(str(target))}; $new = \"$target.new\"; "
            f"Copy-Item -Force -Path {source} -Destination $new; "
            "if (Test-Path $target) { [IO.File]::Replace($new, $target, [NullString]::Value) } "
            "else { Move-Item -Path $new -Destination $target }; "
            f"{set_acl('$target', users_read)}")


UV_IN_UPLOAD = "(Join-Path $in 'uv.exe')"
EXIT_MARK = "pdt-exit|"
OUT_MARK = "pdt-out|"
ERR_MARK = "pdt-err|"


def incoming_root(connection: Connection) -> PureWindowsPath:
    return PureWindowsPath(connection.program_data) / "pdt" / ".incoming"


def prepare_script(connection: Connection, folder: PureWindowsPath) -> str:
    return ("$ErrorActionPreference = 'Stop'; " + SET_ACL
            + f"$root = {quoted(str(incoming_root(connection)))}; "
            "New-Item -ItemType Directory -Force -Path $root | Out-Null; "
            f"{set_acl('$root', False)}"
            f"$in = {quoted(str(folder))}; New-Item -ItemType Directory -Path $in | Out-Null; "
            f"{set_acl('$in', False)}"
            "New-Item -ItemType Directory -Path (Join-Path $in 'download') | Out-Null")


def cleanup_script(folder: PureWindowsPath) -> str:
    return (f"$in = {quoted(str(folder))}; "
            "if (Test-Path $in) { Remove-Item -Recurse -Force $in }")


def sends_secrets(command: str, rest: list[str]) -> bool:
    return command == "deploy" or (command == "secrets" and rest[:1] in (["diff"], ["save"]))


def run_script(connection: Connection, folder: PureWindowsPath, app_name: str, command: str,
               rest: list[str], assume_yes: bool, *, new_uv: bool, tag_lines: bool,
               ask_on_client: bool, stdin: bool) -> str:
    uv = windows_uv.machine_uv(connection.program_data)
    script = (f"$ErrorActionPreference = 'Stop'; {SET_ACL}$in = {quoted(str(folder))}; "
              "$zip = Join-Path $in 'bundle.zip'; "
              "if (Test-Path $zip) { Expand-Archive -Force -Path $zip -DestinationPath $in; "
              "Remove-Item $zip }; ")
    if new_uv:
        script += (f"$bin = {quoted(str(uv.parent))}; "
                   "New-Item -ItemType Directory -Force -Path $bin | Out-Null; "
                   f"{set_acl('$bin', True)}{replace_file(UV_IN_UPLOAD, uv, True)}")
    env = {"PDT_PROJECT": str(folder / "project"), "PDT_CLI_VERSION": pdt_version(),
           "UV_CACHE_DIR": str(windows_uv.machine_cache(connection.program_data))}
    if os.environ.get("PDT_JSON_OUTPUT"):
        env["PDT_JSON_OUTPUT"] = "1"
    if ask_on_client:
        env["PDT_ASK_ON_CLIENT"] = "1"
    script += "".join(f"$env:{name} = {quoted(value)}; " for name, value in env.items())
    script += ("$secret = Join-Path $in 'secrets.json'; "
               "if (Test-Path $secret) { $env:PDT_ENV_JSON = Get-Content -Raw -Encoding UTF8 $secret }; "
               "$ErrorActionPreference = 'Continue'; ")
    args = [command, app_name] + (["--yes"] if assume_yes else []) + (["--", *rest] if rest else [])
    call = (f"& {quoted(str(uv))} run --script {quoted(str(folder / 'pdt' / 'deploy_windows.py'))} "
            + " ".join(map(quoted, args)))
    if stdin:
        call = ("$OutputEncoding = [Text.UTF8Encoding]::new($false); $env:PYTHONIOENCODING = 'utf-8'; "
                f"Get-Content -Raw -Encoding UTF8 (Join-Path $in 'stdin.txt') | {call}")
    if tag_lines:
        return (script + f"{call} 2>&1 | ForEach-Object {{ if ($_ -is "
                "[Management.Automation.ErrorRecord]) "
                f"{{ '{ERR_MARK}' + $_ }} else {{ '{OUT_MARK}' + $_ }} }}; '{EXIT_MARK}' + $LASTEXITCODE")
    return script + f"{call}; exit $LASTEXITCODE"


def pdt_version() -> str:
    from pdt import __version__

    return os.environ.get("PDT_CLI_VERSION") or __version__


def strip_host(stage: Path, app_name: str) -> None:
    import yaml

    for path in (stage / "pdt.yml", stage / app_name / "config.yml"):
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        sections = [data] + [entry for entry in data.get("apps") or [] if isinstance(entry, dict)]
        for section in sections:
            if isinstance(section.get("platform"), dict):
                section["platform"].pop("host", None)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def bundle(app: dict, secrets: dict[str, str] | None, stdin: str | None = None) -> Path:
    from pdt.deploy_common import stage_build_context

    stage = stage_build_context(app)
    try:
        shutil.rmtree(stage / ".pdt-lifecycle", ignore_errors=True)
        strip_host(stage, app["name"])
        archive = Path(tempfile.mkdtemp(prefix="pdt-bundle-")) / "bundle.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    out.write(path, Path("project") / path.relative_to(stage))
            package = Path(__file__).resolve().parent
            for path in sorted(package.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    out.write(path, Path("pdt") / path.relative_to(package))
            if secrets is not None:
                out.writestr("secrets.json", json.dumps(secrets, sort_keys=True))
            if stdin is not None:
                out.writestr("stdin.txt", stdin)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return archive


def sftp_path(path: PureWindowsPath) -> str:
    return "/" + path.as_posix()


@dataclass
class SshSession:
    connection: Connection
    run: Callable = subprocess.run

    def _remote(self, path: PureWindowsPath) -> str:
        name = self.connection.target.name
        return f"[{name}]:{sftp_path(path)}" if ":" in name else f"{name}:{sftp_path(path)}"

    def _scp(self, source: str, destination: str) -> None:
        args = ["scp", "-q", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
        if not can_prompt(None):
            args += ["-o", "BatchMode=yes"]
        if self.connection.target.user:
            args += ["-o", f"User={self.connection.target.user}"]
        proc = self.run(args + ["--", source, destination], stdin=subprocess.DEVNULL,
                        capture_output=True, text=True)
        if proc.returncode != 0:
            raise RemoteError(f"scp failed: {proc.stderr.strip() or f'exit {proc.returncode}'}")

    def script(self, script: str) -> str:
        proc = self.run(ssh_args(self.connection.target, script, can_prompt(None)),
                        stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RemoteError(f"the PC could not run a PowerShell step: "
                              f"{(proc.stderr or proc.stdout).strip() or f'exit {proc.returncode}'}")
        return proc.stdout

    def upload(self, source: Path, destination: PureWindowsPath) -> None:
        self._scp(str(source), self._remote(destination))

    def download(self, source: PureWindowsPath, destination: Path) -> None:
        self._scp(self._remote(source), str(destination))

    def command(self, script: str) -> int:
        as_json = bool(os.environ.get("PDT_JSON_OUTPUT"))
        args = ssh_args(self.connection.target, script, can_prompt(None))
        if can_prompt(None) and not as_json:
            args.insert(1, "-t")
        if as_json:
            proc = self.run(args, stdout=subprocess.PIPE, text=True)
            if proc.stdout.strip():
                console.data(proc.stdout.rstrip("\n"))
            return proc.returncode
        return self.run(args).returncode


@dataclass
class WinRMSession:
    connection: Connection
    client: Callable = winrm_client

    def _client(self):
        c = self.connection
        return self.client(c.target.name, c.port, c.ssl, c.target.user, c.password)

    def script(self, script: str) -> str:
        output, streams, had_errors = self._client().execute_ps(script)
        if had_errors:
            errors = "; ".join(str(error) for error in streams.error)
            raise RemoteError(f"the PC could not run a PowerShell step: {errors}")
        return output

    def upload(self, source: Path, destination: PureWindowsPath) -> None:
        self._client().copy(str(source), str(destination))

    def download(self, source: PureWindowsPath, destination: Path) -> None:
        self._client().fetch(str(source), str(destination))

    def command(self, script: str) -> int:
        from pypsrp.complex_objects import PSInvocationState
        from pypsrp.powershell import PowerShell, RunspacePool

        code = None
        with RunspacePool(self._client().wsman) as pool:
            shell = PowerShell(pool)
            shell.add_script(script)
            shell.begin_invoke()
            shown = 0
            try:
                while True:
                    shell.poll_invoke(timeout=1)
                    for line in shell.output[shown:]:
                        code = show_tagged(str(line), code)
                    shown = len(shell.output)
                    if shell.state != PSInvocationState.RUNNING:
                        break
            except KeyboardInterrupt:
                shell.stop()
                raise
            for error in shell.streams.error:
                console.error(console.escape(str(error)))
            if shell.state != PSInvocationState.COMPLETED or code is None:
                return 1
        return code


def show_tagged(line: str, code: int | None) -> int | None:
    if line.startswith(EXIT_MARK):
        value = line[len(EXIT_MARK):].strip()
        return int(value) if value.lstrip("-").isdigit() else code
    if line.startswith(OUT_MARK):
        text = line[len(OUT_MARK):]
        if os.environ.get("PDT_JSON_OUTPUT"):
            console.data(text)
        else:
            console.say(console.escape(text))
    elif line.startswith(ERR_MARK):
        console.say(console.escape(line[len(ERR_MARK):]))
    return code


def session(connection: Connection):
    return SshSession(connection) if connection.kind == "ssh" else WinRMSession(connection)


def read_secret(connection: Connection, app_name: str, remote) -> str | None:
    secret = PureWindowsPath(connection.program_data) / "pdt" / app_name / "secrets.json"
    output = remote.script(f"$s = {quoted(str(secret))}; "
                           "if (Test-Path $s) { 'pdt-secret|' + (Get-Content -Raw -Encoding UTF8 $s) }")
    marker = output.find("pdt-secret|")
    return None if marker < 0 else output[marker + len("pdt-secret|"):].strip()


def storage_download(rest: list[str], folder: PureWindowsPath):
    """For `storage get <path> [dest]`: the file the PC writes, the local file it becomes,
    and the arguments that make the PC write it there. Flags keep their place."""
    flags = [arg for arg in rest if arg.startswith("--")]
    words = [arg for arg in rest if not arg.startswith("--")]
    name = PureWindowsPath(words[1].replace("/", "\\")).name
    local = Path(words[2]) if len(words) > 2 else Path(name)
    if local.is_dir():
        local = local / name
    source = folder / "download" / "file"
    return source, local, [words[0], words[1], str(source), *flags]


def forward(connection: Connection, app: dict, command: str, rest: list[str],
            assume_yes: bool, remote=None) -> int:
    import uuid

    from pdt.deploy import DECLINED
    from pdt.deploy_common import gather_secrets, run_secrets

    remote = remote or session(connection)
    name = app["name"]
    if command == "secrets" and rest[:1] == ["get"]:
        current = read_secret(connection, name, remote)
        return run_secrets("get", app, current, lambda values: None, assume_yes)
    secrets = gather_secrets(app) if sends_secrets(command, rest) else None
    stdin = sys.stdin.read() if command == "secrets" and rest[:1] == ["set"] else None
    new_uv = connection.uv_version != windows_uv.VERSION
    folder = incoming_root(connection) / f"{name}-{uuid.uuid4().hex[:12]}"
    download = None
    if command == "storage" and rest[:1] == ["get"] and len(
            [arg for arg in rest if not arg.startswith("--")]) >= 2:
        source, local, rest = storage_download(rest, folder)
        download = (source, local)
    tag_lines = connection.kind != "ssh"
    ask = tag_lines and not assume_yes and can_prompt(None)

    def run(yes: bool, ask_on_client: bool) -> int:
        return remote.command(run_script(connection, folder, name, command, rest, yes,
                                         new_uv=new_uv, tag_lines=tag_lines,
                                         ask_on_client=ask_on_client, stdin=stdin is not None))

    archive = bundle(app, secrets, stdin)
    try:
        remote.script(prepare_script(connection, folder))
        console.status(f"Sending {console.value(name)} and pdt {console.value(pdt_version())} "
                       f"to {console.value(connection.computer)}...")
        remote.upload(archive, folder / "bundle.zip")
        if new_uv:
            remote.upload(windows_uv.fetch(connection.processor), folder / "uv.exe")
        shutil.rmtree(archive.parent, ignore_errors=True)
        code = run(assume_yes, ask)
        new_uv = False
        if code == DECLINED and ask:
            if not console.confirm():
                console.warn("Aborted; nothing was changed.")
                return DECLINED
            code = run(True, False)
        if code == 0 and download is not None:
            remote.download(*download)
            console.done(f"Saved {console.value(download[1])} on this computer.")
        return code
    finally:
        shutil.rmtree(archive.parent, ignore_errors=True)
        try:
            remote.script(cleanup_script(folder))
        except RemoteError as exc:
            console.warn(f"could not remove {console.value(folder)} on the PC: "
                         f"{console.escape(str(exc))}")
