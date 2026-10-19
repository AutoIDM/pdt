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
import shutil
import socket
import subprocess
from dataclasses import dataclass, replace
from typing import Callable

from pdt import console
from pdt.utils.email_auth import can_prompt

SSH_PORT = 22
WINRM_PORTS = ((5986, True), (5985, False))
CONNECT_TIMEOUT = 10
PROBE_MARK = "pdt-probe"
PROBE_SCRIPT = (
    "$p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(); "
    f"'{PROBE_MARK}', $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator), "
    "$p.Identity.Name, $env:COMPUTERNAME, $env:ProgramData -join '|'"
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
    notes: tuple[str, ...] = ()


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
    if len(fields) != 5 or fields[1] not in ("True", "False") or "" in fields:
        last = output.strip().splitlines()[-1:] or ["no output"]
        return f"the PC answered, but PowerShell did not run ({last[0]})"
    _mark, admin, account, computer, program_data = fields
    if admin != "True":
        return (f"{account} signed in, but Windows did not give the session administrator "
                "rights. An administrator account is needed")
    return Connection(target, transport, account, computer, program_data)


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
                return replace(result, notes=tuple(dict.fromkeys(reasons)))
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
