"""The web profile on Windows: two WinSW services, oauth2-proxy, a firewall rule, and rights.

No test here needs Windows or the network: the deploy scripts are text, and the downloads
are patched.
"""

import io
import json
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import config, deploy, deploy_windows, deploy_windows_web as web_win, run_windows_service
from pdt.profiles import web

WEB_APP = """\
app_profile: web
web:
  port: 8050
  health_path: /healthz
  hostname: sales.example.com
  tls_cert: certs/sales.pem
  tls_key: certs/sales.key
  auth:
    provider: google
    allowed_domains: [example.com]
storage:
  read: [warehouse]
"""


@pytest.fixture
def windows(project, monkeypatch):
    monkeypatch.setenv("ProgramData", str(project / "ProgramData"))
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "warehouse", "schedule: hourly\n")
    return project


def web_app(project, config_text=WEB_APP):
    folder = add_app(project, "dashboard", config_text)
    (folder / "certs").mkdir(exist_ok=True)
    (folder / "certs" / "sales.pem").write_text("cert")
    (folder / "certs" / "sales.key").write_text("key")
    return config.merged_app("dashboard")


def test_the_app_service_runs_the_runner_with_the_ports_and_storage(windows):
    app = web_app(windows)
    xml = web_win.app_service_xml(app, "uv")
    assert "<id>pdt-dashboard</id>" in xml
    assert "<description>Managed by pdt; serves dashboard from" in xml
    assert "<executable>uv</executable>" in xml
    assert "run_windows_service.py" in xml and " app " in xml
    assert "8050" in xml and "https://sales.example.com/" in xml
    assert "<startmode>Automatic</startmode>" in xml
    assert xml.count('<onfailure action="restart" delay="10 sec"/>') == 3
    assert '<log mode="none"/>' in xml
    assert "<username>NT SERVICE\\pdt-dashboard</username>" in xml
    assert "<allowservicelogon>true</allowservicelogon>" in xml
    assert "<depend>" not in xml
    read = json.loads(web_win.app_arguments(app)[7])
    assert set(read) == {"warehouse"} and read["warehouse"].endswith("/warehouse/storage/")


def test_the_proxy_service_depends_on_the_app_service(windows):
    app = web_app(windows)
    xml = web_win.proxy_service_xml(app, r"C:\Users\jon\.local\bin\uv.exe")
    assert "<id>pdt-dashboard-proxy</id>" in xml
    assert "<depend>pdt-dashboard</depend>" in xml
    assert "<username>NT SERVICE\\pdt-dashboard-proxy</username>" in xml
    assert r"<executable>C:\Users\jon\.local\bin\uv.exe</executable>" in xml
    assert " proxy " in xml and "oauth2-proxy.exe" in xml and "oauth2-proxy.cfg" in xml


def test_the_proxy_config_gates_on_domains_and_holds_no_secret(windows):
    app = web_app(windows)
    text = web_win.proxy_config(app)
    assert 'provider = "google"\n' in text
    assert 'https_address = ":443"\n' in text
    assert 'redirect_url = "https://sales.example.com/oauth2/callback"\n' in text
    assert 'upstreams = ["http://127.0.0.1:8050"]\n' in text
    assert 'email_domains = ["example.com"]\n' in text
    assert "google_group" not in text
    assert 'skip_auth_routes = ["^/healthz$"]\n' in text
    assert "cookie_secure = true\n" in text and 'cookie_refresh = "1h"\n' in text
    assert "sales.pem" in text and "sales.key" in text
    assert "client_secret" not in text and "cookie_secret" not in text


def test_the_proxy_config_gates_on_groups_when_set(windows):
    app = web_app(windows, WEB_APP.replace("  allowed_domains: [example.com]\n",
                                           "  allowed_groups: [sales@example.com]\n"))
    text = web_win.proxy_config(app)
    assert 'email_domains = ["*"]\n' in text
    assert 'google_group = ["sales@example.com"]\n' in text
    # oauth2-proxy does not expand ${VAR} in its file; the admin email travels as an env var.
    assert "google_admin_email" not in text


def test_the_health_path_is_escaped_in_the_proxy_config(windows):
    app = web_app(windows, WEB_APP.replace("/healthz", "/health.check"))
    assert 'skip_auth_routes = ["^/health\\\\.check$"]\n' in web_win.proxy_config(app)


def test_the_proxy_runner_renames_the_pdt_auth_values(tmp_path):
    env = {"PDT_AUTH_CLIENT_ID": "id", "PDT_AUTH_CLIENT_SECRET": "s", "PDT_AUTH_COOKIE_SECRET": "c",
           "PDT_AUTH_GOOGLE_SERVICE_ACCOUNT_PATH": "keys/sa.json",
           "PDT_AUTH_GOOGLE_ADMIN_EMAIL": "admin@example.com", "PATH": "x"}
    out = run_windows_service.proxy_env(env, tmp_path)
    assert out["OAUTH2_PROXY_CLIENT_ID"] == "id"
    assert out["OAUTH2_PROXY_CLIENT_SECRET"] == "s"
    assert out["OAUTH2_PROXY_COOKIE_SECRET"] == "c"
    assert out["OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON"] == str(tmp_path / "keys/sa.json")
    assert out["OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL"] == "admin@example.com"
    assert out["PATH"] == "x"
    assert set(run_windows_service.PROXY_ENV) == {name for name in web.required_env(
        {"auth": {"provider": "google", "allowed_groups": ["g"]},
         "platform": {"provider": "windows"}})}


def test_the_runner_reads_the_env_files_from_the_app_folder_up_to_the_project(tmp_path):
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    (tmp_path / ".env").write_text("A=project\nB=project\n")
    app_dir = tmp_path / "dashboard"
    app_dir.mkdir()
    (app_dir / ".env").write_text("A=app\n")
    env = run_windows_service.load_env(app_dir)
    assert env["A"] == "app" and env["B"] == "project"
    assert run_windows_service.project_folder(app_dir) == tmp_path


def test_the_runner_refuses_a_wrong_command_line(capsys):
    assert run_windows_service.main([]) == 2
    assert run_windows_service.main(["app", "dir", "logs"]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_plan_names_every_resource(windows):
    app = web_app(windows)
    lines = plain(web_win.plan(app, "create", r"PC\jon", "uv", True))
    text = "\n".join(lines)
    assert "create Windows services pdt-dashboard (the app) and pdt-dashboard-proxy" in text
    assert "oauth2-proxy 7.15.6" in text and "WinSW 2.12.0" in text
    assert "run dashboard on http://127.0.0.1:8050 (this PC only)" in text
    assert "listen on https://sales.example.com/ (TCP 443)" in text
    assert "firewall rule pdt-dashboard opens the port" in text
    assert "sign visitors in with google; allow domains example.com" in text
    assert "NT SERVICE\\pdt-dashboard and NT SERVICE\\pdt-dashboard-proxy" in text
    assert "run uv from the system PATH" in text
    assert "read the files of warehouse in" in text
    assert "(kept after destroy)" in text


def test_the_firewall_rule_is_replaced_not_added_twice():
    script = web_win.firewall_script("dashboard")
    assert script.index("Remove-NetFirewallRule -DisplayName 'pdt-dashboard'") < script.index(
        "New-NetFirewallRule -DisplayName 'pdt-dashboard' -Direction Inbound -Protocol TCP "
        "-LocalPort 443 -Action Allow")


def test_the_rights_script_gives_each_service_only_what_it_needs(windows):
    app = web_app(windows)
    script = web_win.rights_script(app)
    folder = str(deploy_windows.app_folder("dashboard"))
    assert f"icacls '{folder}' /grant 'NT SERVICE\\pdt-dashboard:(OI)(CI)RX'" in script
    assert f"icacls '{folder}' /grant 'NT SERVICE\\pdt-dashboard-proxy:(OI)(CI)RX'" in script
    logs = str(deploy_windows.logs_folder("dashboard"))
    assert f"icacls '{logs}' /grant 'NT SERVICE\\pdt-dashboard:(OI)(CI)M'" in script
    proxy_logs = str(deploy_windows.logs_folder("dashboard") / "proxy")
    assert f"icacls '{proxy_logs}' /grant 'NT SERVICE\\pdt-dashboard-proxy:(OI)(CI)M'" in script
    storage = str(deploy_windows.storage_folder("dashboard"))
    assert f"icacls '{storage}' /grant 'NT SERVICE\\pdt-dashboard:(OI)(CI)M'" in script
    other = str(deploy_windows.storage_folder("warehouse"))
    assert f"icacls '{other}' /grant 'NT SERVICE\\pdt-dashboard:(OI)(CI)RX'" in script
    assert "S-1-5-18" not in script


def test_the_install_script_installs_once_then_refreshes(windows):
    app = web_app(windows)
    script = web_win.install_script(app)
    for service in ("pdt-dashboard", "pdt-dashboard-proxy"):
        assert f"if (Get-Service -Name '{service}' -ErrorAction SilentlyContinue)" in script
        assert f"{service}.exe' refresh" in script and f"{service}.exe' install" in script


def test_a_paused_app_is_installed_but_not_started(windows):
    app = web_app(windows, WEB_APP + "pause: true\n")
    script = web_win.start_script(app)
    assert "Stop-Service" in script and "-StartupType Disabled" in script
    assert "Restart-Service" not in script
    running = web_win.start_script({**app, "pause": False})
    assert "Restart-Service -Name 'pdt-dashboard' -Force" in running
    assert running.index("Restart-Service -Name 'pdt-dashboard'") < running.index(
        "Start-Service -Name 'pdt-dashboard-proxy'")


def test_deploy_stages_the_files_then_runs_one_elevated_script(windows, monkeypatch, tmp_path):
    app = web_app(windows)
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "uv_on_machine_path", lambda: True)
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append((script, kw)) or True)
    monkeypatch.setattr(web_win, "service_state", lambda powershell, service: "absent")
    monkeypatch.setattr(web_win, "DOWNLOADS", tmp_path / "downloads")
    monkeypatch.setattr(web_win, "stage_folder", lambda name: tmp_path / "stage" / name)
    winsw = tmp_path / "downloads" / "winsw-2.12.0" / "WinSW-x64.exe"
    winsw.parent.mkdir(parents=True)
    winsw.write_bytes(b"winsw")
    proxy = tmp_path / "downloads" / "oauth2-proxy-7.15.6" / "oauth2-proxy.exe"
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"proxy")
    monkeypatch.setattr(web_win, "wait_healthy", lambda url, seconds=60: "")
    assert web_win.deploy(app, assume_yes=True) == 0
    (script, kw), = scripts
    assert kw == {"elevate": True}
    stage = tmp_path / "stage" / "dashboard"
    assert sorted(path.name for path in (stage / "bin").iterdir()) == [
        "oauth2-proxy.exe", "pdt-dashboard-proxy.exe", "pdt-dashboard-proxy.xml",
        "pdt-dashboard.exe", "pdt-dashboard.xml"]
    assert (stage / "proxy" / "oauth2-proxy.cfg").read_text() == web_win.proxy_config(app)
    assert (script.index("New-Item") < script.index("Copy-Item") < script.index(" install")
            < script.index("NT SERVICE") < script.index("New-NetFirewallRule")
            < script.index("Restart-Service"))


def test_a_plan_downloads_nothing_and_runs_nothing(windows, monkeypatch, capsys):
    app = web_app(windows)
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "uv_on_machine_path", lambda: True)
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda *args, **kw: pytest.fail("ran a script"))
    monkeypatch.setattr(deploy_windows, "confirm", lambda *args: pytest.fail("asked to proceed"))
    monkeypatch.setattr(web_win, "service_state", lambda powershell, service: "absent")
    monkeypatch.setattr(web_win, "winsw_exe", lambda: pytest.fail("downloaded WinSW"))
    monkeypatch.setattr(web_win, "proxy_exe", lambda: pytest.fail("downloaded the proxy"))
    assert web_win.deploy(app, assume_yes=False, plan_only=True) == 0
    out = capsys.readouterr().out
    assert "Plan:" in out and "Windows services on this Windows computer" in out


def test_deploy_stops_when_a_certificate_file_is_missing(windows, monkeypatch, capsys):
    app = web_app(windows)
    (Path(app["dir"]) / "certs" / "sales.key").unlink()
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "uv_on_machine_path", lambda: True)
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(web_win, "service_state", lambda powershell, service: "absent")
    assert web_win.deploy(app, assume_yes=True) == 1
    assert "web.tls_key names" in capsys.readouterr().out


def test_the_proxy_download_is_checked_against_the_published_sha256(monkeypatch, tmp_path):
    monkeypatch.setattr(web_win, "DOWNLOADS", tmp_path)
    fetched = []
    monkeypatch.setattr(web_win, "fetch_text",
                        lambda url: fetched.append(url) or f"{'ab' * 32}  {web_win.PROXY_ARCHIVE}\n")

    def unpack(url, sha256, stage, error):
        fetched.append((url, sha256))
        (stage / "oauth2-proxy-v7.15.6.windows-amd64").mkdir(parents=True)
        (stage / "oauth2-proxy-v7.15.6.windows-amd64" / "oauth2-proxy.exe").write_bytes(b"exe")
        return stage

    monkeypatch.setattr(web_win, "fetch_verified", unpack)
    exe = web_win.proxy_exe()
    assert exe.read_bytes() == b"exe"
    assert fetched == [web_win.PROXY_SUMS_URL, (web_win.PROXY_URL, "ab" * 32)]
    assert web_win.PROXY_URL.endswith("/v7.15.6/oauth2-proxy-v7.15.6.windows-amd64.tar.gz")
    assert web_win.WINSW_URL == "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
    assert web_win.proxy_exe() == exe and len(fetched) == 2


def test_a_sha256_listing_without_the_name_still_gives_its_only_hash():
    assert web_win.checksum_from(f"{'cd' * 32}\n", "x.tar.gz") == "cd" * 32
    with pytest.raises(deploy_windows.WindowsDeployError):
        web_win.checksum_from("nothing here\n", "x.tar.gz")


def test_main_hands_a_web_app_to_the_web_module(windows, monkeypatch):
    web_app(windows)
    calls = []
    monkeypatch.setattr(deploy_windows.sys, "platform", "win32")
    monkeypatch.setattr(deploy_windows.sys, "argv", ["deploy_windows.py", "pause", "dashboard"])
    monkeypatch.setattr(web_win, "command",
                        lambda app, name, rest, yes, plan: calls.append((app["name"], name)) or 0)
    assert deploy_windows.main() == 0
    assert calls == [("dashboard", "pause")]


def test_run_deployed_opens_the_deployed_url(windows, monkeypatch):
    opened = []
    monkeypatch.setattr(web_win.webbrowser, "open", opened.append)
    app = web_app(windows)
    assert web_win.start(app) == 0
    assert opened == [web.url(app)]


def test_run_deployed_refuses_a_web_app(windows, monkeypatch, capsys):
    web_app(windows)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kw: pytest.fail("dispatched"))
    assert deploy.start("dashboard") == 1
    assert "pdt deploy dashboard" in capsys.readouterr().out


def test_wait_healthy_returns_the_problem_after_the_deadline(monkeypatch):
    monkeypatch.setattr(web_win, "HEALTH_POLL_SECONDS", 0)
    clock = [0.0]
    monkeypatch.setattr(web_win.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(web_win.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 1))

    def refuse(url, timeout):
        raise web_win.urllib.error.URLError("connection refused")

    monkeypatch.setattr(web_win.urllib.request, "urlopen", refuse)
    assert web_win.wait_healthy("https://sales.example.com/healthz", 3) == "connection refused"

    class Answer(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(web_win.urllib.request, "urlopen", lambda url, timeout: Answer())
    assert web_win.wait_healthy("https://sales.example.com/healthz", 3) == ""
