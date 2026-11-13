"""Deploy a web app to this Windows PC as two Windows services.

A web app (`app_profile: web`) is an HTTP server that runs all the time
behind a sign-in proxy. On this PC it gets, under `%ProgramData%\\pdt\\<app>\\`:

- `bin\\`: WinSW (one copy per service, named after it) and oauth2-proxy,
  pinned downloads kept in the pdt data folder and copied here.
- `proxy\\oauth2-proxy.cfg`: the proxy settings. No secret is in it; the
  runner passes the secrets from `.env` as environment variables.
- `logs\\`: one log per start of the server (`pdt runs` lists them), and
  `logs\\proxy\\` for the proxy.
- `storage\\`: the app's files, kept after destroy.

And two services, both set to start at boot and restart after a failure:

- `pdt-<app>` runs `run_windows_service.py app ...`, which runs `run.py`
  with PDT_PORT. The app listens on 127.0.0.1 only.
- `pdt-<app>-proxy` runs `run_windows_service.py proxy ...`, which runs
  oauth2-proxy on TCP 443 with the certificate in `web.tls_cert`. It sends
  every visitor to Google, lets in the `web.auth.allowed_domains` and
  `web.auth.allowed_groups`, and forwards to the app with X-Forwarded-Email.
  It depends on `pdt-<app>`.

A firewall rule `pdt-<app>` opens TCP 443. Each service runs as its virtual
account (`NT SERVICE\\<service>`), which gets read rights on the app folder,
modify on `storage\\` and `logs\\`, and read on the storage of each app in
`storage.read`. Deploy stages every file in the pdt data folder and one
elevated PowerShell script creates the folders, copies the files, installs
or refreshes both services, grants the rights, sets the firewall rule, and
restarts the services. Deploy then asks `https://<hostname><health_path>`
until it answers.

Unverified on a real PC, to confirm in a prototype: WinSW with a virtual
service account, and the proxy's TLS listener on 443 with a PEM pair.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from pdt import config, console, deploy_windows as win, deploy_windows_job as job, regions, runs_cli
from pdt.deploy_common import CostEstimate, warn_if_locked
from pdt.download import fetch_verified
from pdt.profiles import web

# WinSW publishes no checksum for its releases, so only the version is pinned.
WINSW_VERSION = "2.12.0"
WINSW_URL = f"https://github.com/winsw/winsw/releases/download/v{WINSW_VERSION}/WinSW-x64.exe"
PROXY_VERSION = "7.15.6"
PROXY_ARCHIVE = f"oauth2-proxy-v{PROXY_VERSION}.windows-amd64.tar.gz"
PROXY_URL = ("https://github.com/oauth2-proxy/oauth2-proxy/releases/download/"
             f"v{PROXY_VERSION}/{PROXY_ARCHIVE}")
PROXY_SUMS_URL = PROXY_URL + "-sha256sum.txt"
PROXY_EXE = "oauth2-proxy.exe"
DOWNLOADS = config.data_home() / "pdt" / "downloads"
RUNNER = Path(__file__).resolve().with_name("run_windows_service.py")
HEALTH_WAIT_SECONDS = 60
HEALTH_POLL_SECONDS = 2
RESTART_DELAY = "10 sec"


def service_name(app_name: str) -> str:
    return win._task_name(app_name)


def proxy_service_name(app_name: str) -> str:
    return f"{service_name(app_name)}-proxy"


def account(service: str) -> str:
    """The virtual account Windows gives a service, which needs no password."""
    return f"NT SERVICE\\{service}"


def bin_folder(app_name: str) -> Path:
    return win.app_folder(app_name) / "bin"


def proxy_folder(app_name: str) -> Path:
    return win.app_folder(app_name) / "proxy"


def config_path(app_name: str) -> Path:
    return proxy_folder(app_name) / "oauth2-proxy.cfg"


def stage_folder(app_name: str) -> Path:
    return config.data_home() / "pdt" / "stage" / app_name


def read_urls(app: dict) -> dict[str, str]:
    return {other: win.storage_url(other) for other in app["storage_read"]}


def tls_path(app: dict, key: str) -> Path:
    path = Path(app["web"][key])
    return path if path.is_absolute() else (Path(app["dir"]) / path).resolve()


def app_arguments(app: dict) -> list[str]:
    return ["run", "--script", str(RUNNER), "app", str(Path(app["dir"]).resolve()),
            str(win.logs_folder(app["name"])), win.storage_url(app["name"]),
            json.dumps(read_urls(app), sort_keys=True), str(web.port(app)), web.url(app)]


def proxy_arguments(app: dict) -> list[str]:
    return ["run", "--script", str(RUNNER), "proxy", str(Path(app["dir"]).resolve()),
            str(win.logs_folder(app["name"])), str(bin_folder(app["name"]) / PROXY_EXE),
            str(config_path(app["name"]))]


def service_xml(app: dict, uv: str, service: str, what: str, arguments: list[str],
                depends_on: str = "") -> str:
    """A WinSW configuration. The runner writes the log, so WinSW keeps none."""
    depend = f"\n  <depend>{html.escape(depends_on)}</depend>" if depends_on else ""
    restart = f'\n  <onfailure action="restart" delay="{RESTART_DELAY}"/>' * 3
    return f"""<service>
  <id>{html.escape(service)}</id>
  <name>{html.escape(service)}</name>
  <description>Managed by pdt; {html.escape(what)} {html.escape(app['name'])} from {html.escape(str(Path(app['dir']).resolve()))}</description>
  <executable>{html.escape(uv)}</executable>
  <arguments>{html.escape(subprocess.list2cmdline(arguments))}</arguments>
  <workingdirectory>{html.escape(str(Path(app['dir']).resolve()))}</workingdirectory>
  <startmode>Automatic</startmode>{restart}{depend}
  <log mode="none"/>
  <serviceaccount>
    <username>{html.escape(account(service))}</username>
    <allowservicelogon>true</allowservicelogon>
  </serviceaccount>
</service>
"""


def app_service_xml(app: dict, uv: str) -> str:
    return service_xml(app, uv, service_name(app["name"]), "serves", app_arguments(app))


def proxy_service_xml(app: dict, uv: str) -> str:
    return service_xml(app, uv, proxy_service_name(app["name"]), "signs visitors in to",
                       proxy_arguments(app), depends_on=service_name(app["name"]))


def proxy_config(app: dict) -> str:
    """The oauth2-proxy settings file. json.dumps writes each value as TOML reads it."""
    auth = app["auth"]
    domains = auth.get("allowed_domains") or ["*"]
    settings: list[tuple[str, object]] = [
        ("provider", "google"),
        ("https_address", f":{web.PROXY_PORT}"),
        ("tls_cert_file", str(tls_path(app, "tls_cert"))),
        ("tls_key_file", str(tls_path(app, "tls_key"))),
        ("redirect_url", f"{web.url(app)}oauth2/callback"),
        ("upstreams", [f"http://127.0.0.1:{web.port(app)}"]),
        ("email_domains", domains),
    ]
    # The admin email and the service account file come from the env (OAUTH2_PROXY_*), which
    # oauth2-proxy reads over the file; the file does not expand ${VAR}.
    if auth.get("allowed_groups"):
        settings += [("google_group", list(auth["allowed_groups"]))]
    settings += [
        ("cookie_secure", True),
        ("cookie_refresh", "1h"),
        ("cookie_expire", "8h"),
        ("skip_auth_routes", [f"^{re.escape(web.health_path(app))}$"]),
        ("set_xauthrequest", True),
        ("pass_user_headers", True),
        ("skip_provider_button", True),
        ("request_logging", True),
    ]
    return "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings)


def firewall_script(app_name: str) -> str:
    name = win._ps_string(service_name(app_name))
    return (f"Remove-NetFirewallRule -DisplayName {name} -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName {name} -Direction Inbound -Protocol TCP "
            f"-LocalPort {web.PROXY_PORT} -Action Allow | Out-Null; ")


def remove_firewall_script(app_name: str) -> str:
    name = win._ps_string(service_name(app_name))
    return f"Remove-NetFirewallRule -DisplayName {name} -ErrorAction SilentlyContinue; "


def folder_script(app: dict, user: str) -> str:
    """Create the folders and give the deploying user modify rights, before the services
    exist. The services' own rights come after `install`, when their accounts exist."""
    name = app["name"]
    folders = [win.app_folder(name), win.logs_folder(name), win.logs_folder(name) / "proxy",
               bin_folder(name), proxy_folder(name)]
    if app["storage"]:
        folders.append(win.storage_folder(name))
    paths = ", ".join(win._ps_string(str(folder)) for folder in folders)
    return (
        f"$folder = {win._ps_string(str(win.app_folder(name)))}; "
        f"New-Item -ItemType Directory -Force -Path {paths} | Out-Null; "
        f"icacls $folder /grant {win._ps_string(user + ':(OI)(CI)M')} | Out-Null; "
        "if ($LASTEXITCODE -ne 0) { throw \"icacls failed with exit $LASTEXITCODE\" }; "
    )


def _grant(folder: Path, who: str, rights: str) -> str:
    return (f"icacls {win._ps_string(str(folder))} /grant "
            f"{win._ps_string(f'{who}:(OI)(CI){rights}')} | Out-Null; "
            "if ($LASTEXITCODE -ne 0) { throw \"icacls failed with exit $LASTEXITCODE\" }; ")


def rights_script(app: dict) -> str:
    """What each service account may touch: read the app folder, write its own logs and
    storage, read the storage of each app in storage.read."""
    name = app["name"]
    server, proxy = account(service_name(name)), account(proxy_service_name(name))
    script = _grant(win.app_folder(name), server, "RX") + _grant(win.app_folder(name), proxy, "RX")
    script += _grant(win.logs_folder(name), server, "M")
    script += _grant(win.logs_folder(name) / "proxy", proxy, "M")
    if app["storage"]:
        script += _grant(win.storage_folder(name), server, "M")
    for other in app["storage_read"]:
        script += _grant(win.storage_folder(other), server, "RX")
    return script


def install_script(app: dict) -> str:
    """Install each service when it is absent, else refresh it from its XML."""
    script = ""
    for service in (service_name(app["name"]), proxy_service_name(app["name"])):
        exe = win._ps_string(str(bin_folder(app["name"]) / f"{service}.exe"))
        script += (f"if (Get-Service -Name {win._ps_string(service)} -ErrorAction SilentlyContinue) "
                   f"{{ & {exe} refresh | Out-Null }} else {{ & {exe} install | Out-Null }}; "
                   f"if ($LASTEXITCODE -ne 0) {{ throw \"WinSW failed for {service} with exit "
                   "$LASTEXITCODE\" }; ")
    return script


def start_script(app: dict) -> str:
    server, proxy = service_name(app["name"]), proxy_service_name(app["name"])
    if app["pause"]:
        return (f"Stop-Service -Name {win._ps_string(proxy)}, {win._ps_string(server)} -Force "
                "-ErrorAction SilentlyContinue; "
                f"Set-Service -Name {win._ps_string(server)} -StartupType Disabled; "
                f"Set-Service -Name {win._ps_string(proxy)} -StartupType Disabled; ")
    return (f"Set-Service -Name {win._ps_string(server)} -StartupType Automatic; "
            f"Set-Service -Name {win._ps_string(proxy)} -StartupType Automatic; "
            f"Restart-Service -Name {win._ps_string(server)} -Force -ErrorAction Stop; "
            f"Start-Service -Name {win._ps_string(proxy)} -ErrorAction Stop; ")


def service_state(powershell: str, service: str) -> str:
    """`absent`, `unmanaged`, `running`, or `stopped` (any state but Running)."""
    script = (f"$s = Get-CimInstance Win32_Service -Filter \"Name='{service}'\"; "
              "if ($null -eq $s) { exit 3 }; "
              "if (-not \"$($s.Description)\".StartsWith('Managed by pdt;')) { exit 4 }; "
              "if ($s.State -eq 'Running') { exit 0 }; exit 5")
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", win._encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    states = {0: "running", 3: "absent", 4: "unmanaged", 5: "stopped"}
    if proc.returncode in states:
        return states[proc.returncode]
    detail = (proc.stderr or proc.stdout).strip()
    raise win.WindowsDeployError(
        "Windows service command failed" + (f": {detail}" if detail else f" (exit {proc.returncode})"))


def checksum_from(text: str, name: str) -> str:
    """The sha256 for `name` in a `sha256sum` listing; the only hash when the file lists one."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    hashes = re.findall(r"\b[0-9a-f]{64}\b", text.lower())
    if len(hashes) == 1:
        return hashes[0]
    raise win.WindowsDeployError(f"no sha256 for {name} in {PROXY_SUMS_URL}")


def fetch_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise win.WindowsDeployError(f"could not download {url}: {e}")


def fetch_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    console.status(f"downloading {console.value(url)}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            target.write_bytes(response.read())
    except urllib.error.URLError as e:
        raise win.WindowsDeployError(f"could not download {url}: {e}")


def winsw_exe() -> Path:
    """The pinned WinSW, downloaded once into the pdt data folder."""
    exe = DOWNLOADS / f"winsw-{WINSW_VERSION}" / "WinSW-x64.exe"
    if not exe.is_file():
        fetch_file(WINSW_URL, exe)
    return exe


def proxy_exe() -> Path:
    """The pinned oauth2-proxy, downloaded and checked against its published sha256 once."""
    exe = DOWNLOADS / f"oauth2-proxy-{PROXY_VERSION}" / PROXY_EXE
    if exe.is_file():
        return exe
    sha256 = checksum_from(fetch_text(PROXY_SUMS_URL), PROXY_ARCHIVE)
    stage = exe.parent / "unpacked"
    shutil.rmtree(stage, ignore_errors=True)
    fetch_verified(PROXY_URL, sha256, stage, win.WindowsDeployError)
    found = next(stage.rglob(PROXY_EXE), None)
    if found is None:
        raise win.WindowsDeployError(f"{PROXY_ARCHIVE} holds no {PROXY_EXE}")
    shutil.copy2(found, exe)
    shutil.rmtree(stage, ignore_errors=True)
    return exe


def stage_files(app: dict, uv: str) -> Path:
    """Write every file the elevated script copies into the app folder: the two WinSW
    copies with their XML, the proxy, and its config."""
    name = app["name"]
    stage = stage_folder(name)
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "bin").mkdir(parents=True)
    (stage / "proxy").mkdir(parents=True)
    winsw = winsw_exe()
    for service, xml in ((service_name(name), app_service_xml(app, uv)),
                         (proxy_service_name(name), proxy_service_xml(app, uv))):
        shutil.copy2(winsw, stage / "bin" / f"{service}.exe")
        (stage / "bin" / f"{service}.xml").write_text(xml, encoding="utf-8")
    shutil.copy2(proxy_exe(), stage / "bin" / PROXY_EXE)
    (stage / "proxy" / "oauth2-proxy.cfg").write_text(proxy_config(app), encoding="utf-8")
    return stage


def plan(app: dict, verb: str, user: str, uv: str, on_machine_path: bool) -> list[console.Markup]:
    name = app["name"]
    server, proxy = service_name(name), proxy_service_name(name)
    auth = app["auth"]
    allowed = ""
    if auth.get("allowed_domains"):
        allowed += "domains " + ", ".join(console.value(d) for d in auth["allowed_domains"])
    if auth.get("allowed_groups"):
        allowed += ("; " if allowed else "") + "groups " + ", ".join(
            console.value(g) for g in auth["allowed_groups"])
    actions = [
        console.Markup(f"{verb} Windows services {console.value(server)} (the app) and "
                       f"{console.value(proxy)} (oauth2-proxy {console.value(PROXY_VERSION)}) "
                       f"through WinSW {console.value(WINSW_VERSION)}; both start at boot and "
                       "restart after a failure" + (" (paused: not started)" if app["pause"] else "")),
        console.Markup(f"run {console.value(name)} on "
                       f"{console.value(f'http://127.0.0.1:{web.port(app)}')} (this PC only)"),
        console.Markup(f"listen on {console.value(web.url(app))} (TCP "
                       f"{console.value(str(web.PROXY_PORT))}) with the certificate "
                       f"{console.value(tls_path(app, 'tls_cert'))}; firewall rule "
                       f"{console.value(server)} opens the port"),
        console.Markup(f"sign visitors in with {console.value(auth.get('provider'))}; allow "
                       f"{allowed}"),
        console.Markup(f"working directory: {console.value(app['dir'])}"),
        console.Markup("run uv from the system PATH" if on_machine_path else
                       f"run uv from {console.value(Path(uv).resolve())}"),
        console.Markup(f"run each service as its own account, {console.value(account(server))} and "
                       f"{console.value(account(proxy))} (read the app folder; write its logs)"),
        console.Markup(f"keep the app's files in {console.value(win.app_folder(name))} "
                       f"({console.value(user)}: modify)"),
        console.Markup(f"write one log per start under {console.value(win.logs_folder(name))} "
                       "(removed on destroy)"),
    ]
    dockerfile = Path(app["dir"]) / "Dockerfile"
    if dockerfile.is_file():
        actions.append(console.Markup(
            f"ignore {console.value(dockerfile)} "
            f"({console.escape(config.dockerfile_problem(app['platform']))})"))
    if app["storage"]:
        actions.append(console.Markup(f"use folder {console.value(win.storage_folder(name))} for "
                                      "the app's files (kept after destroy)"))
    for other in app["storage_read"]:
        actions.append(console.Markup(f"read the files of {console.value(other)} in "
                                      f"{console.value(win.storage_folder(other))}"))
    return actions


def health_url(app: dict) -> str:
    return web.url(app).rstrip("/") + web.health_path(app)


def wait_healthy(url: str, seconds: float = HEALTH_WAIT_SECONDS) -> str:
    """Ask `url` until it answers 2xx or `seconds` pass; the problem, or ""."""
    deadline = time.monotonic() + seconds
    problem = "no answer yet"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 300:
                    return ""
                problem = f"HTTP {response.status}"
        except urllib.error.HTTPError as e:
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            problem = str(e.reason if isinstance(e, urllib.error.URLError) else e)
        if time.monotonic() >= deadline:
            return problem
        time.sleep(HEALTH_POLL_SECONDS)


def deploy(app: dict, assume_yes: bool, plan_only: bool = False) -> int:
    name = app["name"]
    try:
        powershell, uv = win._preflight()
        assert uv is not None
        on_machine_path = win.uv_on_machine_path()
        server = service_name(name)
        console.status(f"Checking Windows service {console.value(server)}...")
        user = win._deploying_user()
        state = service_state(powershell, server)
        if state == "unmanaged":
            raise win.WindowsDeployError(
                f"Windows service {server} exists but is not managed by pdt")
        exists = state != "absent"
        for key in ("tls_cert", "tls_key"):
            if not tls_path(app, key).is_file():
                raise win.WindowsDeployError(
                    f"web.{key} names {tls_path(app, key)}, which is not a file")
    except (config.ConfigError, win.WindowsDeployError) as exc:
        console.error(console.escape(str(exc)))
        return 1

    actions = plan(app, "update" if exists else "create", user, uv, on_machine_path)
    cost = CostEstimate([("Windows services on this Windows computer", 0.0)],
                        "no cloud charges", currency=regions.local_currency())
    if plan_only:
        return win.show_plan(actions, cost)
    if not win.confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return win.DECLINED

    try:
        stage = stage_files(app, "uv" if on_machine_path else str(Path(uv).resolve()))
        script = (
            folder_script(app, user)
            + f"Copy-Item -Path {win._ps_string(str(stage) + chr(92) + '*')} -Destination "
              f"{win._ps_string(str(win.app_folder(name)))} -Recurse -Force -ErrorAction Stop; "
            + install_script(app)
            + rights_script(app)
            + firewall_script(name)
            + start_script(app)
        )
        with console.waiting(f"{'updating' if exists else 'creating'}: Windows services "
                             f"{console.value(server)} and "
                             f"{console.value(proxy_service_name(name))}"):
            win._run(powershell, script, elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Deployed {console.value(name)} as Windows services {console.value(server)} and "
                 f"{console.value(proxy_service_name(name))}.")
    if app["pause"]:
        console.note(f"{console.value(name)} is paused; {console.value(f'pdt unpause {name}')} "
                     "starts it")
        return 0
    url = health_url(app)
    with console.waiting(f"waiting: {console.value(url)} answers"):
        problem = wait_healthy(url)
    if problem != "":
        console.error(f"{console.value(url)} did not answer within "
                      f"{console.value(str(HEALTH_WAIT_SECONDS))} seconds: "
                      f"{console.escape(problem)}. {console.value(f'pdt logs {name}')} shows the "
                      f"server's log; {console.value(web.url(app))} must resolve to this PC.")
        return 1
    console.done(f"{console.value(url)} answers.")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    server, proxy = service_name(name), proxy_service_name(name)
    try:
        powershell, _uv = win._preflight(require_uv=False)
        console.status(f"Checking Windows service {console.value(server)}...")
        states = {service: service_state(powershell, service) for service in (server, proxy)}
        if "unmanaged" in states.values():
            raise win.WindowsDeployError(
                f"Windows service {server} exists but is not managed by pdt")
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    folders = [folder for folder in (win.logs_folder(name), bin_folder(name), proxy_folder(name))
               if folder.is_dir()]
    present = [service for service, state in states.items() if state != "absent"]
    if not present and not folders:
        console.done(f"Nothing to remove for {console.value(name)}; service "
                     f"{console.value(server)} does not exist.")
        if app["storage"]:
            console.say(win._kept_storage_line(name))
        return 0
    actions = []
    script = ""
    for service in present:
        actions.append(console.Markup(f"stop and delete Windows service {console.value(service)}"))
        exe = win._ps_string(str(bin_folder(name) / f"{service}.exe"))
        script += (f"Stop-Service -Name {win._ps_string(service)} -Force -ErrorAction "
                   f"SilentlyContinue; if (Test-Path {exe}) {{ & {exe} uninstall | Out-Null }} "
                   f"else {{ sc.exe delete {win._ps_string(service)} | Out-Null }}; ")
    actions.append(console.Markup(f"delete firewall rule {console.value(server)}"))
    script += remove_firewall_script(name)
    for folder in folders:
        actions.append(console.Markup(f"delete folder {console.value(folder)}"))
        script += (f"Remove-Item -Recurse -Force -Path {win._ps_string(str(folder))} "
                   "-ErrorAction Stop; ")
    if app["storage"]:
        actions.append(console.Markup(
            f"keep folder {console.value(win.storage_folder(name))} (the app's files)"))
        warn_if_locked(win._store(name), name)
    if not win.confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return win.DECLINED
    try:
        with console.waiting(f"removing: Windows services {console.value(server)} and "
                             f"{console.value(proxy)}"):
            win._run(powershell, script, elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Removed Windows services {console.value(server)} and {console.value(proxy)}.")
    for folder in folders:
        console.say(f"removed: folder {console.value(folder)}")
    if app["storage"]:
        console.say(win._kept_storage_line(name))
    return 0


def _managed(app: dict) -> str:
    """The PowerShell path, once the app's service exists and is pdt's."""
    powershell, _uv = win._preflight(require_uv=False)
    state = service_state(powershell, service_name(app["name"]))
    if state == "unmanaged":
        raise win.WindowsDeployError(
            f"Windows service {service_name(app['name'])} exists but is not managed by pdt")
    if state == "absent":
        raise win.WindowsDeployError(
            f"Windows service {service_name(app['name'])} does not exist; run pdt deploy "
            f"{app['name']} first")
    return powershell


def pause(app: dict, paused: bool) -> int:
    try:
        powershell = _managed(app)
        win._run(powershell, start_script({**app, "pause": paused}), elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    state = "stopped and disabled" if paused else "started"
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: Windows "
                 f"services {console.value(service_name(app['name']))} and "
                 f"{console.value(proxy_service_name(app['name']))} are {state}.")
    return 0


def start(app: dict, follow: bool = False) -> int:
    """`pdt run APP --deployed` opens the deployed app in the browser."""
    url = web.url(app)
    console.done(f"Opening {console.value(url)}")
    webbrowser.open(url)
    return 0


def list_runs(app_name: str) -> list[runs_cli.Run]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    return job.runs_from_logs(app_name, lambda: powershell is not None
                              and service_state(powershell, service_name(app_name)) == "running")


def command(app: dict, name: str, rest: list[str], assume_yes: bool, plan_only: bool) -> int:
    """One provider command for a web app; `deploy_windows.main` calls it."""
    if name == "runs":
        return runs_cli.runs(lambda: list_runs(app["name"]), app["name"], rest,
                             page="services.msc",
                             note=f"Find the service {console.value(service_name(app['name']))} "
                                  "in Services.")
    if name == "logs":
        return job.log_command(app, rest, lambda: list_runs(app["name"]))
    if name in ("pause", "unpause"):
        return pause(app, name == "pause")
    if name == "start":
        return start(app, "--follow" in rest)
    if name == "deploy":
        return deploy(app, assume_yes, plan_only)
    return destroy(app, assume_yes)
