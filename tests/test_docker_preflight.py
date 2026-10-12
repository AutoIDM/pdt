import json
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
                 context="default", group=True, starts=True, cpu=None):
        self.system = system
        self.tools = set(tools)
        self.running = running
        self.desktop = "Docker Desktop" if desktop is True else desktop
        self.context = context
        self.group = group
        self.starts = starts
        self.chown_works = True
        self.restart = False
        self.profile = False
        self.welcome_skipped = False
        self.wsl_status = 0
        self.commands = []
        self.now = 0.0
        monkeypatch.setattr(docker_setup.platform, "system", lambda: self.system)
        monkeypatch.setattr(docker_setup.platform, "machine",
                            lambda: cpu or ("AMD64" if system == "Windows" else "aarch64"))
        monkeypatch.setattr(docker_setup.shutil, "which",
                            lambda name: f"/bin/{name}" if name in self.tools else None)
        monkeypatch.setattr(docker_setup, "desktop", self.desktop_app)
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
        self.commands.append(command)
        if command == ["wsl", "--status"]:
            return SimpleNamespace(returncode=self.wsl_status, stdout=b"", stderr=b"")
        if command == ["docker", "context", "show"]:
            return SimpleNamespace(returncode=0, stdout=f"{self.context}\n", stderr="")
        if command == ["brew", "--prefix"]:
            return SimpleNamespace(returncode=0, stdout="/opt/homebrew\n", stderr="")
        if command[0] in ("open", f"{MAC_RANCHER}/rdctl") or command in (["colima", "start"],
                                               ["sudo", "systemctl", "start", "docker"]):
            self.running = self.starts
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


def test_a_rancher_desktop_with_no_log_stops_with_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_setup.Path, "home", lambda: tmp_path)
    Machine(monkeypatch, "Darwin", tools={"docker"}, desktop="Rancher Desktop", starts=False)
    problem = docker_setup.ensure("AWS", assume_yes=False)
    assert problem == ("Rancher Desktop did not start. Start it, wait until Docker is running, "
                       "then run the same command again.")


@pytest.mark.parametrize("system, tools, commands", [
    ("Darwin", {"brew"}, ["brew install colima docker docker-buildx", ["colima", "start"]]),
    ("Windows", {"winget"}, [WINGET, DESKTOPS["Windows", "Rancher Desktop"]]),
    ("Linux", {"curl"}, ["curl -fsSL https://get.docker.com | sudo sh",
                         "sudo systemctl enable --now docker"]),
])
def test_a_missing_docker_installs_and_starts_after_yes(
        monkeypatch, capsys, tmp_path, system, tools, commands):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    machine = Machine(monkeypatch, system, tools=tools)
    assert docker_setup.ensure("Azure", assume_yes=True) == ""
    assert [c for c in machine.commands if c in commands] == commands
    out = said(capsys)
    assert "Docker is not installed. pdt builds the image for Azure" in out
    assert "Docker is running." in out
    assert machine.profile == (system == "Windows")
    assert machine.welcome_skipped == (system == "Windows")


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


def test_a_windows_install_prints_only_pdt_lines(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Windows", tools={"winget"})
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
        "pdt will install Docker with these commands:",
        f"  {WINGET}",
        f"==> {WINGET}",
        "Starting Rancher Desktop...",
        "Docker is running.",
    ]


def test_windows_arm64_installs_nothing_and_names_the_install_page(monkeypatch):
    machine = Machine(monkeypatch, "Windows", tools={"winget"}, cpu="ARM64")
    problem = docker_setup.ensure("AWS", assume_yes=True)
    assert problem.startswith("Docker is not installed. pdt builds the image for AWS")
    assert docker_setup.INSTALL_PAGES["Windows"] in problem
    assert machine.commands == []
    assert not machine.profile


def test_a_homebrew_install_links_buildx_where_docker_looks(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    Machine(monkeypatch, "Darwin", tools={"brew"})
    docker_setup.ensure("AWS", assume_yes=True)
    link = tmp_path / "cli-plugins" / "docker-buildx"
    assert link.readlink() == Path("/opt/homebrew/lib/docker/cli-plugins/docker-buildx")


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


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
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
    machine = Machine(monkeypatch, "Linux", tools={"curl"})
    monkeypatch.setattr(deploy, "proceed", lambda assume_yes: False)
    assert "https://docs.docker.com/engine/install/" in docker_setup.ensure("AWS", False)
    assert machine.commands == []


def test_a_failed_install_names_the_command_and_the_page(monkeypatch):
    machine = Machine(monkeypatch, "Windows", tools={"winget"})
    monkeypatch.setattr(docker_setup.subprocess, "run",
                        lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    problem = Text.from_markup(docker_setup.ensure("AWS", assume_yes=True)).plain
    assert problem.startswith(f"{WINGET} failed.")
    assert docker_setup.INSTALL_PAGES["Windows"] in problem
    assert machine.commands == []


def test_a_linux_user_outside_the_docker_group_joins_it_with_no_new_login(monkeypatch, capsys):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    assert docker_setup.ensure("AWS", assume_yes=False) == ""
    assert machine.commands == [["sudo", "usermod", "-aG", "docker", "ann"],
                                ["sudo", "chown", "ann", "/var/run/docker.sock"]]
    assert "Adding ann to the docker group" in said(capsys)


def test_when_the_socket_stays_closed_the_new_group_needs_a_new_login(monkeypatch):
    machine = Machine(monkeypatch, "Linux", tools={"docker"}, running=True, group=False)
    machine.chown_works = False
    assert docker_setup.ensure("AWS", assume_yes=False).startswith("Log out and log in again")


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
    assert commands[1:] == [["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt",
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
])
def test_a_failed_emulator_install_names_the_command_and_its_error(
        monkeypatch, capsys, binfmt, reason):
    builder(monkeypatch, "linux/amd64", binfmt=binfmt)
    problem = Text.from_markup(docker_setup.emulate("linux/arm64")).plain
    assert problem.startswith("docker run --privileged --rm tonistiigi/binfmt --install arm64 failed")
    assert reason in problem
    assert capsys.readouterr().out == ""
