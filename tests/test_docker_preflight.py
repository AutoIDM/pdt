import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text

from pdt import (deploy, deploy_aws_batch, deploy_azure_container_apps, deploy_common,
                 deploy_google_cloud, docker_setup)

# Each provider's deploy, the name its message uses, and its first call that reaches the cloud.
PROVIDERS = [
    (deploy_aws_batch, "AWS", "ensure_session"),
    (deploy_azure_container_apps, "Azure", "azure_settings"),
    (deploy_google_cloud, "Google Cloud", "project_region"),
]
RDCTL = "C:/Program Files/Rancher Desktop/resources/resources/win32/bin/rdctl.exe"
MAC_DOCKER = "/Applications/Docker.app/Contents/Resources/bin"
MAC_RANCHER = "/Applications/Rancher Desktop.app/Contents/Resources/resources/darwin/bin"
DESKTOPS = {("Darwin", "Docker Desktop"): ["open", "-a", "/Applications/Docker.app"],
            ("Darwin", "Rancher Desktop"): [f"{MAC_RANCHER}/rdctl", *docker_setup.RANCHER_START],
            ("Windows", "Docker Desktop"): ["C:/Program Files/Docker/Docker/Docker Desktop.exe"],
            ("Windows", "Rancher Desktop"): [RDCTL, *docker_setup.RANCHER_START]}
WINGET = ("winget install -e --id SUSE.RancherDesktop --silent --accept-source-agreements "
          "--accept-package-agreements --disable-interactivity")


class Machine:
    """A computer as docker_setup sees it: the tools on the PATH and what docker answers."""

    def __init__(self, monkeypatch, system, tools=(), running=False, desktop=False,
                 context="default", group=True, starts=True, cpu=None, buildx=True,
                 macos="15.5", unit=True, apps=()):
        self.system = system
        self.tools = set(tools)
        self.running = running
        self.desktop = "Docker Desktop" if desktop is True else desktop
        self.context = context
        self.group = group
        self.starts = starts
        self.buildx = buildx
        self.unit = unit
        self.hangs = False
        self.brew_prefix = "/opt/homebrew"
        self.chown_works = True
        self.restart = False
        self.profile = False
        self.welcome_skipped = False
        self.wsl_status = 0
        self.commands = []
        self.now = 0.0
        monkeypatch.setattr(docker_setup.platform, "system", lambda: self.system)
        monkeypatch.setattr(docker_setup.platform, "machine",
                            lambda: cpu or {"Windows": "AMD64", "Darwin": "arm64"}.get(system, "aarch64"))
        monkeypatch.setattr(docker_setup.platform, "mac_ver",
                            lambda: (macos if system == "Darwin" else "", ("", "", ""), ""))
        monkeypatch.setattr(docker_setup.shutil, "which",
                            lambda name: f"/bin/{name}" if name in self.tools else None)
        monkeypatch.setattr(docker_setup, "desktop", self.desktop_app)
        monkeypatch.setattr(docker_setup, "engine_apps", lambda: list(apps))
        monkeypatch.setattr(docker_setup, "restart_pending", lambda: self.restart)
        monkeypatch.setattr(docker_setup, "rancher_profile", lambda: setattr(self, "profile", True))
        monkeypatch.setattr(docker_setup, "skip_wsl_welcome",
                            lambda: setattr(self, "welcome_skipped", True))
        monkeypatch.setattr(docker_setup.subprocess, "run", self.run)
        monkeypatch.setattr(docker_setup.subprocess, "Popen", self.popen)
        monkeypatch.setattr(docker_setup.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(docker_setup.time, "sleep", self.sleep)
        monkeypatch.setattr(docker_setup.getpass, "getuser", lambda: "ann")

    def desktop_app(self):
        if not self.desktop:
            return None
        self.tools.add("docker")
        return self.desktop, DESKTOPS[self.system, self.desktop]

    def sleep(self, seconds):
        self.now += seconds

    def run(self, command, **kwargs):
        if command == ["docker", "info"]:
            if self.hangs:
                raise docker_setup.subprocess.TimeoutExpired(command, kwargs["timeout"])
            if not self.running:
                return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to the "
                                       "Docker daemon. Is the docker daemon running?")
            if not self.group:
                return SimpleNamespace(returncode=1, stdout="", stderr="permission denied while "
                                       "trying to connect to the docker API")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["docker", "buildx", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="Platforms: linux/amd64, linux/arm64\n",
                                   stderr="")
        if command == ["docker", "buildx", "version"]:
            return SimpleNamespace(returncode=0 if self.buildx else 1, stdout="", stderr="")
        if command == ["systemctl", "cat", "docker"]:
            return SimpleNamespace(returncode=0 if self.unit else 1, stdout="", stderr="")
        self.commands.append(command)
        if command == ["wsl", "--status"]:
            return SimpleNamespace(returncode=self.wsl_status, stdout=b"", stderr=b"")
        if command == ["docker", "context", "show"]:
            return SimpleNamespace(returncode=0, stdout=f"{self.context}\n", stderr="")
        if command == ["brew", "--prefix"]:
            return SimpleNamespace(returncode=0, stdout=f"{self.brew_prefix}\n", stderr="")
        if command[0] in ("open", f"{MAC_RANCHER}/rdctl") or command in (
                ["colima", "start"], ["brew", "services", "start", "colima"],
                ["sudo", "systemctl", "start", "docker"]):
            self.running = self.starts
        if command == "brew services start colima":
            service = docker_setup.Path.home() / docker_setup.BREW_COLIMA_SERVICE
            service.parent.mkdir(parents=True, exist_ok=True)
            service.touch()
        if command[:2] == ["sudo", "chown"]:
            self.group = self.chown_works
        if isinstance(command, str):
            self.tools |= {"docker", "colima"} if command.startswith("brew ") else {"docker"}
            self.desktop = "Rancher Desktop" if command.startswith("winget ") else False
            self.running = command == "sudo systemctl enable --now docker"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def popen(self, command, **kwargs):
        self.commands.append(command)
        self.running = self.starts


def said(capsys):
    return " ".join(capsys.readouterr().out.split())


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    monkeypatch.setenv("CI", "true")


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_setup.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(docker_setup, "DOCKER_DIR", tmp_path / "pdt data" / "docker")
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return tmp_path / "home"


@pytest.mark.parametrize("module, provider, first_cloud_call", PROVIDERS)
def test_deploy_stops_before_the_cloud_without_docker(
        monkeypatch, capsys, module, provider, first_cloud_call):
    asked = []

    def ensure(name, assume_yes):
        asked.append((name, assume_yes))
        return "Docker is not installed."

    monkeypatch.setattr(docker_setup, "ensure", ensure)
    monkeypatch.setattr(module, first_cloud_call, lambda *args: pytest.fail("reached the cloud"))
    with pytest.raises(SystemExit):
        module.deploy({"name": "report"}, assume_yes=True)
    assert asked == [(provider, True)]
    assert "Docker is not installed." in said(capsys)


@pytest.mark.parametrize("module, provider, image_platform", [
    (deploy_aws_batch, "AWS", "linux/arm64"),
    (deploy_azure_container_apps, "Azure", "linux/amd64"),
    (deploy_google_cloud, "Google Cloud", "linux/amd64"),
])
def test_deploy_checks_that_docker_can_build_its_cpu_type(
        monkeypatch, capsys, module, provider, image_platform):
    asked = []

    def emulate(name):
        asked.append(name)
        return "Docker cannot build the image."

    monkeypatch.setattr(docker_setup, "ensure", lambda name, assume_yes: "")
    monkeypatch.setattr(docker_setup, "emulate", emulate)
    with pytest.raises(SystemExit):
        module.deploy({"name": "report"}, assume_yes=True)
    assert asked == [image_platform]


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
def test_a_running_docker_needs_nothing_and_prints_nothing(monkeypatch, capsys, system):
    machine = Machine(monkeypatch, system, tools={"docker"}, running=True)
    deploy_common.docker_preflight("AWS", "linux/arm64", assume_yes=False)
    assert machine.commands == []
    assert capsys.readouterr().out == ""


def test_a_running_desktop_whose_docker_is_off_the_path_needs_nothing(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Windows", running=True, desktop=True)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert machine.commands == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("system, tools, desktop, context, command", [
    ("Darwin", {"docker"}, True, "desktop-linux", DESKTOPS["Darwin", "Docker Desktop"]),
    ("Darwin", {"docker", "colima"}, False, "colima", ["colima", "start"]),
    ("Darwin", {"docker", "colima"}, True, "colima", ["colima", "start"]),
    ("Darwin", {"docker", "colima"}, True, "desktop-linux", DESKTOPS["Darwin", "Docker Desktop"]),
    ("Darwin", {"docker"}, "Rancher Desktop", "rancher-desktop",
     DESKTOPS["Darwin", "Rancher Desktop"]),
    ("Windows", {"docker"}, True, "default", DESKTOPS["Windows", "Docker Desktop"]),
    ("Windows", {"docker"}, "Rancher Desktop", "default", DESKTOPS["Windows", "Rancher Desktop"]),
    ("Linux", {"docker", "systemctl"}, False, "default", ["sudo", "systemctl", "start", "docker"]),
])
def test_an_installed_docker_that_is_not_running_starts(
        monkeypatch, capsys, system, tools, desktop, context, command):
    machine = Machine(monkeypatch, system, tools=tools, desktop=desktop, context=context)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert command in machine.commands
    out = said(capsys)
    assert "Starting" in out
    assert "Docker is running." in out


def test_a_start_that_never_answers_stops_with_one_step(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Darwin", tools={"docker"}, desktop=True, starts=False)
    problem = docker_setup.ensure("AWS", assume_yes=False)
    assert problem.startswith("Docker Desktop did not start.")
    assert machine.now >= docker_setup.START_SECONDS


@pytest.mark.parametrize("system, logs", [
    ("Windows", ["AppData", "Local", "rancher-desktop", "logs"]),
    ("Darwin", ["Library", "Logs", "rancher-desktop"])])
def test_a_rancher_desktop_that_does_not_start_shows_the_end_of_its_log(
        monkeypatch, tmp_path, system, logs):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(docker_setup.Path, "home", lambda: tmp_path)
    log = tmp_path.joinpath(*logs, "background.log")
    log.parent.mkdir(parents=True)
    log.write_text("\n".join([
        "2026-10-12T14:20:50.000Z: UnhandledRejectionWarning: Error: ENOENT: no such file, watch 'C:\\.kube\\config'",
        "2026-10-12T14:20:51.000Z: Error: first",
        "2026-10-12T14:20:52.000Z: Starting WSL",
        "2026-10-12T14:20:59.001Z: Rancher Desktop was unable to start: l [Error]: wsl.exe exited with code 4294967295",
        "2026-10-12T14:21:00.003Z: Error Starting Rancher Desktop",
        "2026-10-12T14:21:01.003Z: Error [x] stays plain",
    ]), encoding="utf-8")
    Machine(monkeypatch, system, tools={"docker"}, desktop="Rancher Desktop", starts=False)
    problem = docker_setup.ensure("AWS", assume_yes=False)
    lines = Text.from_markup(problem).plain.splitlines()
    assert lines == [
        "Rancher Desktop did not start. Start it, wait until Docker is running, then run the "
        "same command again.",
        f"Rancher Desktop's log {log} ends with:",
        "  2026-10-12T14:20:59.001Z: Rancher Desktop was unable to start: l [Error]: wsl.exe "
        "exited with code 4294967295",
        "  2026-10-12T14:21:00.003Z: Error Starting Rancher Desktop",
        "  2026-10-12T14:21:01.003Z: Error [x] stays plain",
    ]
    assert "[dim]Rancher Desktop's log [bold not dim]" in problem


@pytest.mark.parametrize("system, settings", [
    ("Windows", ["AppData", "Roaming", "rancher-desktop", "settings.json"]),
    ("Darwin", ["Library", "Preferences", "rancher-desktop", "settings.json"])])
@pytest.mark.parametrize("started_before", [False, True])
def test_a_rancher_desktop_that_never_started_gets_the_first_start_time(
        monkeypatch, capsys, tmp_path, system, settings, started_before):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(docker_setup.Path, "home", lambda: tmp_path)
    if started_before:
        tmp_path.joinpath(*settings).parent.mkdir(parents=True)
        tmp_path.joinpath(*settings).write_text("{}")
    machine = Machine(monkeypatch, system, tools={"docker"}, desktop="Rancher Desktop",
                      starts=False)
    assert docker_setup.ensure("AWS", assume_yes=False).startswith("Rancher Desktop did not start.")
    assert ("The first start takes several minutes." in said(capsys)) != started_before
    limit = docker_setup.START_SECONDS if started_before else docker_setup.FIRST_START_SECONDS
    assert limit <= machine.now < limit + 5


def test_a_rancher_desktop_with_no_log_stops_with_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_setup.Path, "home", lambda: tmp_path)
    Machine(monkeypatch, "Darwin", tools={"docker"}, desktop="Rancher Desktop", starts=False)
    problem = docker_setup.ensure("AWS", assume_yes=False)
    assert problem == ("Rancher Desktop did not start. Start it, wait until Docker is running, "
                       "then run the same command again.")


@pytest.mark.parametrize("system, tools, commands", [
    ("Darwin", {"brew"}, ["brew install colima docker docker-buildx", "brew services start colima",
                          ["brew", "services", "start", "colima"]]),
    ("Windows", {"winget"}, [WINGET, DESKTOPS["Windows", "Rancher Desktop"]]),
    ("Linux", {"sudo"}, ["sudo sh SCRIPT", "sudo systemctl enable --now docker"]),
])
def test_a_missing_docker_installs_and_starts_after_yes(
        monkeypatch, capsys, tmp_path, system, tools, commands):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    machine = Machine(monkeypatch, system, tools=tools)
    fetched = fake_downloads(monkeypatch)
    commands = [c.replace("SCRIPT", shlex.quote(str(docker_setup.DOCKER_DIR / "install.sh")))
                if isinstance(c, str) else c for c in commands]
    assert docker_setup.ensure("Azure", assume_yes=True) == ""
    assert [c for c in machine.commands if c in commands] == commands
    out = said(capsys)
    assert "Docker is not installed. pdt builds the image for Azure" in out
    assert "Docker is running." in out
    assert machine.profile == (system == "Windows")
    assert machine.welcome_skipped == (system == "Windows")
    assert fetched == ([docker_setup.DOCKER_INSTALL[:2]] if system == "Linux" else [])


@pytest.mark.parametrize("wsl_status, command", [(0, f'{WINGET} --custom "MSIINSTALLPERUSER=1"'),
                                                 (50, WINGET)])
def test_with_wsl_rancher_desktop_installs_for_this_user_only(monkeypatch, capsys, wsl_status,
                                                              command):
    machine = Machine(monkeypatch, "Windows", tools={"winget", "wsl"})
    machine.wsl_status = wsl_status
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    assert command in machine.commands
    out = capsys.readouterr().out.splitlines()
    assert f"  {command}" in out
    assert f"==> {command}" in out


def install_lines(system):
    """What an install prints on `system` when every step works."""
    if system == "Windows":
        return [f"  {WINGET}", f"==> {WINGET}", "Starting Rancher Desktop...",
                "The first start takes several minutes."]
    if system == "Darwin":
        return ["  brew install colima docker docker-buildx", "  brew services start colima",
                "==> brew install colima docker docker-buildx", "==> brew services start colima",
                "Starting Colima...", "The first start takes several minutes."]
    script = docker_setup.DOCKER_DIR / "install.sh"
    return [f"  Download {docker_setup.DOCKER_INSTALL[0]} to {script}, and check its sha256.",
            f"  sudo sh {shlex.quote(str(script))}", "  sudo systemctl enable --now docker",
            "  sudo usermod -aG docker ann", "  sudo chown ann /var/run/docker.sock",
            "  Add the QEMU emulator with tonistiigi/binfmt when the image is for another CPU "
            "type.",
            f"==> sudo sh {shlex.quote(str(script))}", "==> sudo systemctl enable --now docker"]


@pytest.mark.parametrize("system, tool", [("Windows", "winget"), ("Darwin", "brew"),
                                          ("Linux", "sudo")])
def test_an_install_prints_only_pdt_lines(monkeypatch, capsys, system, tool):
    machine = Machine(monkeypatch, system, tools={tool})
    fake_downloads(monkeypatch)
    quiet = []
    run, popen = machine.run, machine.popen
    monkeypatch.setattr(docker_setup.subprocess, "run", lambda command, **kwargs: (
        quiet.append(kwargs.get("capture_output")) or run(command, **kwargs)))
    monkeypatch.setattr(docker_setup.subprocess, "Popen", lambda command, **kwargs: (
        quiet.append(kwargs.get("stdout") == kwargs.get("stderr") == docker_setup.subprocess.DEVNULL)
        or popen(command, **kwargs)))
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    assert quiet and all(quiet)
    assert capsys.readouterr().out.splitlines() == [
        "Docker is not installed. pdt builds the image for AWS with Docker on this computer.",
        "pdt will install Docker with these steps:",
        *install_lines(system),
        "Docker is running.",
    ]


@pytest.mark.parametrize("system, tools, command", [
    ("Linux", {"docker", "sudo", "systemctl"}, "sudo systemctl enable --now docker"),
    ("Windows", {"docker", "winget"}, WINGET),
])
def test_a_docker_cli_with_no_engine_installs_one(monkeypatch, capsys, system, tools, command):
    machine = Machine(monkeypatch, system, tools=tools, unit=False)
    fake_downloads(monkeypatch)
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    assert command in machine.commands
    assert ["sudo", "systemctl", "start", "docker"] not in machine.commands
    out = said(capsys)
    assert "Docker has no engine on this computer. pdt builds the image for AWS" in out
    assert "Docker is not installed." not in out
    assert "Docker is running." in out


REMOTE = "tcp://build.example.com:2376"


@pytest.mark.parametrize("system, tools, setup, problem", [
    ("Darwin", {"docker", "brew"}, {"host": REMOTE},
     f"pdt cannot reach the Docker engine that DOCKER_HOST names, {REMOTE}. Start it, then run "
     "the same command again."),
    ("Darwin", set(), {"host": REMOTE},
     f"pdt cannot reach the Docker engine that DOCKER_HOST names, {REMOTE}. Start it, then run "
     "the same command again."),
    ("Linux", {"docker", "sudo", "systemctl"}, {"context": "build-server"},
     "pdt cannot reach the Docker engine that the docker context build-server names. Start it, "
     "then run the same command again."),
    ("Linux", {"docker", "sudo", "systemctl"}, {"context": "desktop-linux"},
     "pdt cannot reach the Docker engine that the docker context desktop-linux names. Start it, "
     "then run the same command again."),
    ("Linux", {"docker", "sudo", "systemctl"}, {"hangs": True},
     "The Docker engine does not answer docker info in 20 seconds. Restart it, then run the same "
     "command again."),
    ("Darwin", {"docker", "brew"}, {"apps": ["OrbStack"]},
     "pdt found OrbStack but cannot start it; start it, then run the same command again."),
    ("Darwin", {"docker", "brew"}, {"apps": ["Podman Desktop"], "context": "colima"},
     "pdt found Podman Desktop but cannot start it; start it, then run the same command again."),
    ("Windows", {"docker", "winget"}, {"apps": ["Docker Desktop"]},
     "pdt found Docker Desktop but cannot start it; start it, then run the same command again."),
])
def test_an_engine_that_pdt_cannot_start_is_never_replaced(
        monkeypatch, capsys, system, tools, setup, problem):
    if "host" in setup:
        monkeypatch.setenv("DOCKER_HOST", setup["host"])
    machine = Machine(monkeypatch, system, tools=tools, unit=False,
                      context=setup.get("context", "default"), apps=setup.get("apps", ()))
    machine.hangs = setup.get("hangs", False)
    fetched = fake_downloads(monkeypatch)
    assert Text.from_markup(docker_setup.ensure("AWS", assume_yes=True)).plain == problem
    assert [c for c in machine.commands if c != ["docker", "context", "show"]] == []
    assert fetched == []
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name == "nt", reason="macOS app paths use / separators")
def test_a_mac_finds_each_engine_app_in_either_applications_folder(monkeypatch, home):
    installed = {"/Applications/OrbStack.app", str(home / "Applications" / "Docker.app")}
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(docker_setup.Path, "exists", lambda path: str(path) in installed)
    assert docker_setup.engine_apps() == ["Docker Desktop", "OrbStack"]


def test_windows_finds_each_engine_app_in_the_installed_programs(monkeypatch):
    programs = {"HKLM": {"Docker Desktop": "Docker Desktop", "{1A2B}": "7-Zip"},
                "HKCU": {"{3C4D}": "Rancher Desktop", "{5E6F}": None}}

    class Key:
        def __init__(self, parent, path):
            self.names = programs[parent] if parent in programs else parent.names
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def query(key, name):
        if key.names[key.path] is None:
            raise FileNotFoundError(name)
        return key.names[key.path], 1

    winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU", OpenKey=Key, QueryValueEx=query,
        QueryInfoKey=lambda key: (len(key.names), 0, 0),
        EnumKey=lambda key, i: list(key.names)[i])
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Windows")
    assert docker_setup.engine_apps() == ["Docker Desktop", "Rancher Desktop"]


@pytest.mark.parametrize("tools", [{"docker"}, {"brew", "docker"}])
def test_a_docker_cli_with_nothing_to_start_installs_docker(monkeypatch, capsys, tools):
    (docker_setup.DOCKER_DIR / "bin").mkdir(parents=True)
    (docker_setup.DOCKER_DIR / "bin" / "docker").write_text("docker")
    machine = Machine(monkeypatch, "Darwin", tools=tools)
    fetched = fake_downloads(monkeypatch, machine)
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    if "brew" in tools:
        assert "brew install colima docker docker-buildx" in machine.commands
        assert fetched == []
    else:
        assert fetched == [(url, sha256) for url, sha256, *_ in docker_setup.COLIMA_DOWNLOADS["arm64"]]
        assert (docker_setup.DOCKER_DIR / "bin" / "colima").exists()
    assert "Docker is running." in said(capsys)


def test_windows_arm64_installs_nothing_and_names_the_install_page(monkeypatch):
    machine = Machine(monkeypatch, "Windows", tools={"winget"}, cpu="ARM64")
    problem = docker_setup.ensure("AWS", assume_yes=True)
    assert problem.startswith("Docker is not installed. pdt builds the image for AWS")
    assert docker_setup.INSTALL_PAGES["Windows"] in problem
    assert machine.commands == []
    assert not machine.profile


@pytest.mark.parametrize("tools", [{"brew"}, set()])
def test_macos_12_installs_nothing_and_says_why(monkeypatch, tools):
    machine = Machine(monkeypatch, "Darwin", tools=tools, macos="12.7.6")
    fetched = fake_downloads(monkeypatch)
    problem = docker_setup.ensure("AWS", assume_yes=True)
    assert Text.from_markup(problem).plain == (
        "Docker is not installed. pdt builds the image for AWS with Docker on this computer. "
        "The Colima that pdt installs needs macOS 13 or later, and this Mac runs macOS 12.7.6. "
        f"Install Docker from {docker_setup.INSTALL_PAGES['Darwin']}, then run the same "
        "command again.")
    assert machine.commands == []
    assert fetched == []


def fake_downloads(monkeypatch, machine=None):
    """fetch_verified that writes each file the real archive or binary holds, and records
    each (url, sha256). A finished colima download puts colima and docker on the PATH."""
    fetched = []

    def fetch(url, sha256, stage, error):
        fetched.append((url, sha256))
        name = Path(url).name
        if name.endswith(".tar.gz"):
            files = ["bin/limactl", "share/lima/lima-guestagent.Linux-aarch64.gz"]
        elif name.endswith(".tgz"):
            files = ["docker/docker"]
        else:
            files = [name]
        for file in files:
            (stage / file).parent.mkdir(parents=True, exist_ok=True)
            (stage / file).write_text(url)
        if machine is not None and name.startswith("colima-"):
            machine.tools |= {"colima", "docker"}
        return stage

    monkeypatch.setattr(docker_setup, "fetch_verified", fetch)
    return fetched


def test_a_mac_without_homebrew_downloads_colima_and_starts_it(monkeypatch, capsys, home):
    machine = Machine(monkeypatch, "Darwin", buildx=False)
    fetched = fake_downloads(monkeypatch, machine)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    downloads = docker_setup.COLIMA_DOWNLOADS["arm64"]
    assert fetched == [(url, sha256) for url, sha256, *_ in downloads]
    bin_dir = docker_setup.DOCKER_DIR / "bin"
    assert docker_setup.os.environ["PATH"] == f"/usr/bin{docker_setup.os.pathsep}{bin_dir}"
    for tool in ("limactl", "docker", "docker-buildx", "colima"):
        assert (bin_dir / tool).exists()
    assert os.name == "nt" or (bin_dir / "colima").stat().st_mode & 0o777 == 0o755
    assert (docker_setup.DOCKER_DIR / "share" / "lima" / "lima-guestagent.Linux-aarch64.gz").exists()
    assert machine.commands == [["docker", "context", "show"], ["colima", "start"]]
    link = home / ".docker" / "cli-plugins" / "docker-buildx"
    assert link.resolve() == (bin_dir / "docker-buildx").resolve()
    lines = capsys.readouterr().out.splitlines()
    assert lines[:8] == [
        "Docker is not installed. pdt builds the image for AWS with Docker on this computer.",
        "pdt will install Docker with these steps:",
        f"  Download Colima, Lima, the docker CLI, and docker-buildx to {docker_setup.DOCKER_DIR}, "
        "and check each file's sha256:",
        *(f"    {url}" for url, *_ in downloads),
        f"  Link docker-buildx into {home / '.docker' / 'cli-plugins'}.",
    ]
    assert lines[8:] == [
        "  Run colima start, now and on each deploy that finds Colima stopped.",
        "Starting Colima...",
        "The first start takes several minutes.",
        "Docker is running.",
        f"Linked {bin_dir / 'docker-buildx'} into {home / '.docker' / 'cli-plugins'}, so docker "
        "has the buildx plugin.",
    ]


def test_a_mac_download_that_fails_starts_nothing(monkeypatch):
    machine = Machine(monkeypatch, "Darwin")

    def fetch(url, sha256, stage, error):
        raise error(f"checksum mismatch for {url}")

    monkeypatch.setattr(docker_setup, "fetch_verified", fetch)
    problem = docker_setup.ensure("AWS", assume_yes=True)
    assert problem.startswith("checksum mismatch for https://github.com/lima-vm/lima/")
    assert machine.commands == []
    assert not (docker_setup.DOCKER_DIR / "bin" / "colima").exists()


def test_a_later_run_finds_the_downloaded_colima_on_the_path(monkeypatch):
    (docker_setup.DOCKER_DIR / "bin").mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    Machine(monkeypatch, "Darwin", tools={"docker"}, running=True)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    bin_dir = docker_setup.DOCKER_DIR / "bin"
    assert docker_setup.os.environ["PATH"] == f"/usr/bin{docker_setup.os.pathsep}{bin_dir}"


def test_a_docker_without_buildx_links_homebrews_buildx(monkeypatch, capsys, tmp_path, home):
    machine = Machine(monkeypatch, "Darwin", tools={"brew", "docker"}, running=True, buildx=False)
    machine.brew_prefix = str(tmp_path / "brew")
    brewed = tmp_path / "brew" / "lib" / "docker" / "cli-plugins" / "docker-buildx"
    brewed.parent.mkdir(parents=True)
    brewed.touch()
    fetched = fake_downloads(monkeypatch)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert (home / ".docker" / "cli-plugins" / "docker-buildx").resolve() == brewed.resolve()
    assert fetched == []
    assert "so docker has the buildx plugin." in said(capsys)


@pytest.mark.parametrize("system, cpu", [("Darwin", "arm64"), ("Darwin", "x86_64"),
                                         ("Linux", "x86_64"), ("Linux", "aarch64")])
def test_a_docker_without_buildx_gets_the_pinned_buildx(monkeypatch, tmp_path, system, cpu):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "config"))
    Machine(monkeypatch, system, tools={"docker"}, running=True, cpu=cpu, buildx=False)
    fetched = fake_downloads(monkeypatch)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert fetched == [docker_setup.BUILDX_DOWNLOADS[system, cpu]]
    link = tmp_path / "config" / "cli-plugins" / "docker-buildx"
    assert link.resolve() == (docker_setup.DOCKER_DIR / "bin" / "docker-buildx").resolve()
    assert link.read_text() == fetched[0][0]


def test_a_docker_without_buildx_and_no_pinned_one_names_the_page(monkeypatch):
    Machine(monkeypatch, "Windows", tools={"docker"}, running=True, buildx=False)
    problem = docker_setup.ensure("AWS", assume_yes=False)
    assert problem.startswith("Docker has no buildx plugin")
    assert docker_setup.BUILDX_PAGE in problem


def test_a_windows_install_that_waits_for_a_restart_says_so_and_starts_nothing(monkeypatch):
    machine = Machine(monkeypatch, "Windows", tools={"winget"})
    machine.restart = True
    assert docker_setup.ensure("AWS", assume_yes=True) == docker_setup.RESTART_WINDOWS
    assert DESKTOPS["Windows", "Rancher Desktop"] not in machine.commands
    assert machine.profile


def test_a_desktop_that_does_not_start_before_a_restart_asks_for_the_restart(monkeypatch):
    machine = Machine(monkeypatch, "Windows", tools={"docker"}, desktop=True, starts=False)
    machine.restart = True
    assert docker_setup.ensure("AWS", assume_yes=False) == docker_setup.RESTART_WINDOWS


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_without_an_installer_pdt_names_the_install_page(monkeypatch, system):
    machine = Machine(monkeypatch, system)
    problem = docker_setup.ensure("Google Cloud", assume_yes=True)
    assert problem.startswith("Docker is not installed. pdt builds the image for Google Cloud")
    assert docker_setup.INSTALL_PAGES[system] in problem
    assert machine.commands == []


def test_with_no_one_to_answer_and_no_yes_nothing_installs(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Darwin", tools={"brew"})
    problem = docker_setup.ensure("AWS", assume_yes=False)
    assert docker_setup.INSTALL_PAGES["Darwin"] in problem
    assert machine.commands == []
    assert "brew install colima docker docker-buildx" in said(capsys)


def test_a_no_answer_installs_nothing(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"sudo"})
    monkeypatch.setattr(deploy, "proceed", lambda assume_yes: False)
    assert "https://docs.docker.com/engine/install/" in docker_setup.ensure("AWS", False)
    assert machine.commands == []


@pytest.mark.parametrize("restart", [False, True])
def test_a_failed_install_names_the_command_the_page_and_its_last_lines(monkeypatch, restart):
    machine = Machine(monkeypatch, "Windows", tools={"winget"})
    machine.restart = restart
    commands = []
    monkeypatch.setattr(docker_setup.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(
            returncode=1, stderr="", stdout="first\nsecond\nthird\nfourth [x]\n")))
    problem = docker_setup.ensure("AWS", assume_yes=True)
    assert commands == [WINGET]
    if restart:
        assert problem == docker_setup.RESTART_WINDOWS
        return
    assert Text.from_markup(problem).plain.splitlines() == [
        f"{WINGET} failed. Install Docker from {docker_setup.INSTALL_PAGES['Windows']}, then run "
        "the same command again.",
        "  second", "  third", "  fourth [x]"]
    assert "[dim]  fourth \\[x][/]" in problem


def test_a_failed_start_shows_its_last_lines(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"docker", "systemctl"})
    run = machine.run
    monkeypatch.setattr(docker_setup.subprocess, "run", lambda command, **kwargs: (
        SimpleNamespace(returncode=1, stdout="", stderr="Job for docker.service failed.\n")
        if command[0] == "sudo" else run(command, **kwargs)))
    problem = Text.from_markup(docker_setup.ensure("AWS", assume_yes=False)).plain
    assert problem.splitlines() == [
        "sudo systemctl start docker failed. Start Docker, then run the same command again.",
        "  Job for docker.service failed."]


def test_a_docker_that_never_answers_is_not_running(monkeypatch):
    Machine(monkeypatch, "Linux", tools={"docker"})

    def run(command, **kwargs):
        raise docker_setup.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(docker_setup.subprocess, "run", run)
    assert docker_setup.docker_info() == "no answer"


GROUP = [["sudo", "usermod", "-aG", "docker", "ann"], ["sudo", "chown", "ann", "/var/run/docker.sock"]]


def test_a_linux_user_outside_the_docker_group_joins_it_after_one_question(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    asked = []
    monkeypatch.setattr(deploy, "proceed", lambda assume_yes: asked.append(assume_yes) or True)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert asked == [False]
    assert machine.commands == GROUP
    assert capsys.readouterr().out.splitlines() == [
        "Docker answers ann only through sudo.",
        "pdt will add you to the docker group with these steps:",
        "  sudo usermod -aG docker ann",
        "  sudo chown ann /var/run/docker.sock",
        "Adding ann to the docker group, so that docker works without sudo...",
        "Docker is running.",
    ]


def test_a_no_answer_leaves_the_docker_group_alone(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    monkeypatch.setattr(deploy, "proceed", lambda assume_yes: False)
    problem = Text.from_markup(docker_setup.ensure("AWS", assume_yes=False)).plain
    assert problem.startswith("Add ann to the docker group with sudo usermod -aG docker ann")
    assert machine.commands == []


def test_a_linux_install_joins_the_docker_group_with_no_second_question(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"sudo"}, group=False)
    fake_downloads(monkeypatch)
    asked = []
    monkeypatch.setattr(deploy, "proceed", lambda assume_yes: asked.append(assume_yes) or True)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert asked == [False]
    assert machine.commands[-2:] == GROUP


def test_yes_skips_the_docker_group_question_but_not_its_steps(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    assert docker_setup.ensure("AWS", assume_yes=True) == ""
    assert machine.commands == GROUP
    assert capsys.readouterr().out.splitlines() == [
        "Docker answers ann only through sudo.",
        "pdt will add you to the docker group with these steps:",
        "  sudo usermod -aG docker ann",
        "  sudo chown ann /var/run/docker.sock",
        "Adding ann to the docker group, so that docker works without sudo...",
        "Docker is running.",
    ]


def test_when_the_socket_stays_closed_the_new_group_needs_a_new_login(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    machine.chown_works = False
    assert docker_setup.ensure("AWS", assume_yes=True).startswith("Log out and log in again")


@pytest.mark.parametrize("installed, name, bin_dir", [
    (["Docker/Docker/resources/bin"], "Docker Desktop", "Docker/Docker/resources/bin"),
    (["Rancher Desktop/resources/resources/win32/bin"], "Rancher Desktop",
     "Rancher Desktop/resources/resources/win32/bin"),
    (["Docker/Docker/resources/bin", "Rancher Desktop/resources/resources/win32/bin"],
     "Docker Desktop", "Docker/Docker/resources/bin"),
    (["local/Programs/Rancher Desktop/resources/resources/win32/bin"], "Rancher Desktop",
     "local/Programs/Rancher Desktop/resources/resources/win32/bin"),
])
def test_a_new_desktop_puts_its_docker_on_this_runs_path(monkeypatch, tmp_path, installed, name,
                                                         bin_dir):
    for folder in installed:
        (tmp_path / folder).mkdir(parents=True)
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(docker_setup.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("PATH", "C:\\Windows")
    assert docker_setup.desktop()[0] == name
    assert docker_setup.os.environ["PATH"].startswith(str(tmp_path / bin_dir))


@pytest.mark.skipif(os.name == "nt", reason="macOS app paths use / separators")
@pytest.mark.parametrize("installed, name, bin_dir", [
    ([MAC_RANCHER], "Rancher Desktop", MAC_RANCHER),
    ([MAC_DOCKER, MAC_RANCHER], "Docker Desktop", MAC_DOCKER),
])
def test_a_mac_desktop_puts_its_docker_on_this_runs_path(monkeypatch, installed, name, bin_dir):
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(docker_setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(docker_setup.Path, "exists", lambda path: str(path) in installed)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert docker_setup.desktop() == (name, DESKTOPS["Darwin", name])
    assert docker_setup.os.environ["PATH"].startswith(bin_dir)


def test_the_rancher_profile_chooses_moby_without_kubernetes(monkeypatch):
    written = {}

    class Key:
        def __init__(self, hive, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    winreg = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU", REG_DWORD="REG_DWORD", REG_SZ="REG_SZ", CreateKey=Key,
        SetValueEx=lambda key, name, reserved, kind, value: written.update(
            {f"{key.path}\\{name}": (kind, value)}))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    docker_setup.rancher_profile()
    defaults = "Software\\Rancher Desktop\\Profile\\Defaults"
    assert written == {f"{defaults}\\version": ("REG_DWORD", 19),
                       f"{defaults}\\containerEngine\\name": ("REG_SZ", "moby"),
                       f"{defaults}\\kubernetes\\enabled": ("REG_DWORD", 0)}


@pytest.mark.parametrize("values, subkeys, written", [
    ({}, [], {"OOBEComplete": ("REG_DWORD", 1)}),
    ({"OOBEComplete": 0}, [], {"OOBEComplete": ("REG_DWORD", 1)}),
    ({"OOBEComplete": 1}, [], {}),
    ({}, ["{0b5d9a5c-6f0e-4b5e-9d6b-6a1d2b3c4d5e}"], {}),
    ({}, ["AppxInstallerCache"], {"OOBEComplete": ("REG_DWORD", 1)}),
])
def test_the_wsl_welcome_window_is_marked_seen_only_before_the_first_distribution(
        monkeypatch, values, subkeys, written):
    found = {}

    class Key:
        def __init__(self, hive, path):
            assert (hive, path) == ("HKCU", docker_setup.WSL_USER_KEY)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], "REG_DWORD"

    winreg = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU", REG_DWORD="REG_DWORD", CreateKey=Key, QueryValueEx=query,
        QueryInfoKey=lambda key: (len(subkeys), len(values), 0),
        EnumKey=lambda key, i: subkeys[i],
        SetValueEx=lambda key, name, reserved, kind, value: found.update({name: (kind, value)}))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    docker_setup.skip_wsl_welcome()
    assert found == written


def builder(monkeypatch, platforms, inspect_code=0, binfmt=None):
    """A Docker whose builder lists `platforms` in `docker buildx inspect`, and whose
    tonistiigi/binfmt run returns `binfmt` (returncode, stderr, supported)."""
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command == ["docker", "buildx", "inspect"]:
            return SimpleNamespace(returncode=inspect_code, stderr="", stdout=(
                "Name:   default\nDriver: docker\n\nNodes:\nName:   default\n"
                f"Platforms: {platforms}\nLabels:\n"))
        returncode, stderr, supported = binfmt
        stdout = json.dumps({"supported": supported, "emulators": []}) if supported else ""
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(docker_setup.subprocess, "run", run)
    return commands


@pytest.mark.parametrize("platforms, image_platform", [
    ("linux/amd64, linux/amd64/v2, linux/386", "linux/arm64"),
    ("linux/arm64, linux/arm/v7", "linux/amd64")])
def test_a_builder_without_the_platform_gets_the_emulator(
        monkeypatch, capsys, platforms, image_platform):
    cpu = image_platform.split("/")[1]
    commands = builder(monkeypatch, platforms,
                       binfmt=(0, f"installing: {cpu} OK\n", ["linux/riscv64", image_platform]))
    assert docker_setup.emulate(image_platform) == ""
    assert commands[1:] == [["docker", "run", "--privileged", "--rm", docker_setup.BINFMT,
                             "--install", cpu]]
    assert f"Added the {cpu} emulator with docker run" in said(capsys)


def test_a_registered_emulator_that_the_builder_does_not_list_prints_nothing(
        monkeypatch, capsys):
    builder(monkeypatch, "linux/amd64, linux/amd64/v2, linux/amd64/v3",
            binfmt=(0, "installing: arm64 qemu-aarch64 already registered\n",
                    ["linux/amd64", "linux/arm64", "linux/386"]))
    assert docker_setup.emulate("linux/arm64") == ""
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("platforms, image_platform", [
    ("linux/amd64, linux/amd64/v2, linux/386", "linux/amd64"),
    ("linux/amd64*, linux/arm64*", "linux/arm64"),
    ("linux/arm64, linux/amd64, linux/amd64/v2, linux/riscv64", "linux/amd64"),
])
def test_a_builder_that_lists_the_platform_needs_nothing(
        monkeypatch, capsys, platforms, image_platform):
    commands = builder(monkeypatch, platforms)
    assert docker_setup.emulate(image_platform) == ""
    assert commands == [["docker", "buildx", "inspect"]]
    assert capsys.readouterr().out == ""


def test_a_builder_that_does_not_answer_is_left_to_the_build(monkeypatch, capsys):
    commands = builder(monkeypatch, "", inspect_code=1)
    assert docker_setup.emulate("linux/arm64") == ""
    assert commands == [["docker", "buildx", "inspect"]]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("binfmt, reason", [
    ((0, "installing: arm64 cannot register \"/usr/bin/qemu-aarch64\" to "
         "/proc/sys/fs/binfmt_misc/register: write /proc/sys/fs/binfmt_misc/register: "
         "invalid argument\n", ["linux/amd64"]),
     "installing: arm64 cannot register"),
    ((125, "docker: Error response from daemon: pull access denied.\n", None),
     "docker: Error response from daemon: pull access denied."),
    ((0, "installing: arm64 OK\n", None), "installing: arm64 OK"),
])
def test_a_failed_emulator_install_names_the_command_and_its_error(
        monkeypatch, capsys, binfmt, reason):
    builder(monkeypatch, "linux/amd64", binfmt=binfmt)
    problem = Text.from_markup(docker_setup.emulate("linux/arm64")).plain
    assert problem.startswith(f"docker run --privileged --rm {docker_setup.BINFMT} --install arm64 failed")
    assert reason in problem
    assert capsys.readouterr().out == ""
