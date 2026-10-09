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
DESKTOP_APPS = {"Darwin": Path("/Applications/Docker.app"),
                "Windows": Path("C:/Program Files/Docker/Docker/Docker Desktop.exe")}


class Machine:
    """A computer as docker_setup sees it: the tools on the PATH and what docker answers."""

    def __init__(self, monkeypatch, system, tools=(), running=False, desktop=False,
                 context="default", group=True, starts=True):
        self.system = system
        self.tools = set(tools)
        self.running = running
        self.desktop = desktop
        self.context = context
        self.group = group
        self.starts = starts
        self.chown_works = True
        self.restart = False
        self.commands = []
        self.now = 0.0
        monkeypatch.setattr(docker_setup.platform, "system", lambda: self.system)
        monkeypatch.setattr(docker_setup.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(docker_setup.shutil, "which",
                            lambda name: f"/bin/{name}" if name in self.tools else None)
        monkeypatch.setattr(docker_setup, "desktop", self.desktop_app)
        monkeypatch.setattr(docker_setup, "restart_pending", lambda: self.restart)
        monkeypatch.setattr(docker_setup.subprocess, "run", self.run)
        monkeypatch.setattr(docker_setup.subprocess, "Popen", self.popen)
        monkeypatch.setattr(docker_setup.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(docker_setup.time, "sleep", self.sleep)
        monkeypatch.setattr(docker_setup.getpass, "getuser", lambda: "ann")

    def desktop_app(self):
        if not self.desktop:
            return None
        self.tools.add("docker")
        return DESKTOP_APPS[self.system]

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
        self.commands.append(command)
        if command == ["docker", "context", "show"]:
            return SimpleNamespace(returncode=0, stdout=f"{self.context}\n", stderr="")
        if command == ["brew", "--prefix"]:
            return SimpleNamespace(returncode=0, stdout="/opt/homebrew\n", stderr="")
        if command[0] == "open" or command in (["colima", "start"],
                                               ["sudo", "systemctl", "start", "docker"]):
            self.running = self.starts
        if command[:2] == ["sudo", "chown"]:
            self.group = self.chown_works
        if isinstance(command, str):
            self.tools |= {"docker", "colima"} if command.startswith("brew ") else {"docker"}
            self.desktop = command.startswith("winget ")
            self.running = command == "sudo systemctl enable --now docker"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def popen(self, command):
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
    ("Darwin", {"docker"}, True, "desktop-linux", ["open", "-a", str(DESKTOP_APPS["Darwin"])]),
    ("Darwin", {"docker", "colima"}, False, "colima", ["colima", "start"]),
    ("Darwin", {"docker", "colima"}, True, "colima", ["colima", "start"]),
    ("Darwin", {"docker", "colima"}, True, "desktop-linux",
     ["open", "-a", str(DESKTOP_APPS["Darwin"])]),
    ("Windows", {"docker"}, True, "default",
     [str(DESKTOP_APPS["Windows"])]),
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


@pytest.mark.parametrize("system, tools, commands", [
    ("Darwin", {"brew"}, ["brew install colima docker docker-buildx", ["colima", "start"]]),
    ("Windows", {"winget"}, ["winget install -e --id Docker.DockerDesktop "
                             "--accept-source-agreements --accept-package-agreements",
                             [str(DESKTOP_APPS["Windows"])]]),
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
    assert ("paid plan" in out) == (system == "Windows")
    assert "Docker is running." in out


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
    assert [str(DESKTOP_APPS["Windows"])] not in machine.commands


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
    assert problem.startswith("winget install -e --id Docker.DockerDesktop")
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


def test_a_new_desktop_puts_its_docker_on_this_runs_path(monkeypatch, tmp_path):
    exe = tmp_path / "Docker" / "Docker" / "Docker Desktop.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(docker_setup.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\Windows")
    assert docker_setup.desktop() == exe
    assert docker_setup.os.environ["PATH"].startswith(str(exe.parent / "resources" / "bin"))


def linux(monkeypatch, tmp_path, machine, returncode=0):
    commands = []
    monkeypatch.setattr(docker_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(docker_setup.platform, "machine", lambda: machine)
    monkeypatch.setattr(docker_setup, "BINFMT_DIR", tmp_path)
    monkeypatch.setattr(docker_setup.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(returncode=returncode, stdout="", stderr="")))
    return commands


@pytest.mark.parametrize("machine, image_platform", [
    ("x86_64", "linux/arm64"), ("aarch64", "linux/amd64")])
def test_linux_registers_the_emulator_for_the_other_cpu_type(
        monkeypatch, capsys, tmp_path, machine, image_platform):
    commands = linux(monkeypatch, tmp_path, machine)
    cpu = image_platform.split("/")[1]
    assert docker_setup.emulate(image_platform) == ""
    assert commands == [["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt",
                         "--install", cpu]]
    assert f"Docker on this computer cannot build for {cpu}." in said(capsys)


@pytest.mark.parametrize("system, machine, handler, image_platform", [
    ("Linux", "x86_64", "", "linux/amd64"),
    ("Linux", "aarch64", "", "linux/arm64"),
    ("Linux", "x86_64", "qemu-aarch64", "linux/arm64"),
    ("Darwin", "arm64", "", "linux/amd64"),
    ("Windows", "AMD64", "", "linux/arm64"),
])
def test_a_native_cpu_a_registered_handler_or_a_desktop_needs_nothing(
        monkeypatch, capsys, tmp_path, system, machine, handler, image_platform):
    commands = linux(monkeypatch, tmp_path, machine)
    monkeypatch.setattr(docker_setup.platform, "system", lambda: system)
    if handler:
        (tmp_path / handler).touch()
    assert docker_setup.emulate(image_platform) == ""
    assert commands == []
    assert capsys.readouterr().out == ""


def test_a_failed_emulator_install_names_the_command(monkeypatch, tmp_path):
    linux(monkeypatch, tmp_path, "x86_64", returncode=1)
    problem = Text.from_markup(docker_setup.emulate("linux/arm64")).plain
    assert problem.startswith("docker run --privileged --rm tonistiigi/binfmt --install arm64 failed")
