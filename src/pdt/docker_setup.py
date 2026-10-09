"""Get Docker ready for a cloud deploy, with no step the user must look up.

Every cloud provider builds its image with docker on this computer, with
`docker build --ssh`, which needs the buildx plugin.

- `docker info` answers: nothing to do, and nothing is printed.
- Docker is installed but not running: start it and wait until it answers.
- Docker is not installed: show the commands that install it, ask once
  (`--yes` skips the question), and run them. macOS with Homebrew gets
  Colima, which needs no administrator password, no license, and no window.
  Windows gets Docker Desktop from winget. Linux gets Docker Engine from
  get.docker.com.
- On Linux, a user outside the docker group joins it.
- On Linux, Docker Engine builds for another CPU type (AWS's arm64 on an
  x86_64 computer) only through a QEMU handler, which a restart removes.
  When the handler is missing, pdt registers it with tonistiigi/binfmt,
  Docker's documented way. Docker Desktop and Colima register it themselves.
- Anything else: one line with Docker's install page for this system.
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from pdt import console

INSTALL_PAGES = {
    "Darwin": "https://docs.docker.com/desktop/setup/install/mac-install/",
    "Windows": "https://docs.docker.com/desktop/setup/install/windows-install/",
    "Linux": "https://docs.docker.com/engine/install/",
}
INSTALLERS = {
    "Darwin": ("brew", ["brew install colima docker docker-buildx"]),
    "Windows": ("winget", ["winget install -e --id Docker.DockerDesktop "
                           "--accept-source-agreements --accept-package-agreements"]),
    "Linux": ("curl", ["curl -fsSL https://get.docker.com | sudo sh",
                       "sudo systemctl enable --now docker"]),
}
DESKTOP_LICENSE = ("Docker Desktop needs a paid plan at a company with more than 250 "
                   "employees or more than $10 million in yearly revenue.")
LINUX_SOCKET = "/var/run/docker.sock"
CPU_TYPES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
QEMU_NAMES = {"amd64": "x86_64", "arm64": "aarch64"}
BINFMT_DIR = Path("/proc/sys/fs/binfmt_misc")
RESTART_WINDOWS = "Restart Windows to finish the Docker install, then run the same command again."
START_SECONDS = 300


def install_page() -> str:
    return INSTALL_PAGES.get(platform.system(), INSTALL_PAGES["Linux"])


def docker_info() -> str | None:
    """None when no docker is on the PATH, "" when Docker answers, else what docker said."""
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    return "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip() or "no answer"


def denied(said: str | None) -> bool:
    return platform.system() == "Linux" and "permission denied" in (said or "").lower()


def restart_pending() -> bool:
    """Windows waits for a restart, for example to turn on the WSL features Docker Desktop needs."""
    if platform.system() != "Windows":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                            r"\Component Based Servicing\RebootPending"):
            return True
    except OSError:
        return False


def desktop() -> Path | None:
    """Docker Desktop's program, when it is installed. Its docker joins this run's PATH,
    because a new install reaches only the terminals opened after it."""
    if platform.system() == "Darwin":
        app = Path("/Applications/Docker.app")
        bin_dir = app / "Contents" / "Resources" / "bin"
    elif platform.system() == "Windows":
        app = Path(os.environ.get("ProgramFiles", r"C:\Program Files"),
                   "Docker", "Docker", "Docker Desktop.exe")
        bin_dir = app.parent / "resources" / "bin"
    else:
        return None
    if not app.exists():
        return None
    if shutil.which("docker") is None:
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return app


def starter() -> tuple[str, list[str]] | None:
    """The installed Docker's name and the command that starts it."""
    app = desktop()
    if platform.system() == "Darwin" and shutil.which("colima"):
        context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True,
                                 check=False).stdout if shutil.which("docker") else ""
        if app is None or context.startswith("colima"):
            return "Colima", ["colima", "start"]
    if app is not None:
        return "Docker Desktop", ["open", "-a", str(app)] if app.suffix == ".app" else [str(app)]
    if platform.system() == "Linux" and shutil.which("docker") and shutil.which("systemctl"):
        return "the Docker service", ["sudo", "systemctl", "start", "docker"]
    return None


def install(provider: str, assume_yes: bool) -> str:
    """Install Docker once the user agrees. Returns "" when it is installed, else what to do."""
    from pdt.deploy import proceed

    missing = (f"Docker is not installed. pdt builds the image for {provider} with Docker "
               "on this computer.")
    tool, commands = INSTALLERS.get(platform.system(), ("", []))
    if not commands or shutil.which(tool) is None:
        return f"{missing} Install Docker from {console.value(install_page())}, then run the same command again."
    console.warn(missing)
    console.say("pdt will install Docker with these commands:")
    for command in commands:
        console.bullet(console.value(command))
    if platform.system() == "Windows":
        console.say(DESKTOP_LICENSE)
    if not proceed(assume_yes):
        return f"Docker is not installed. Install Docker from {console.value(install_page())}, then run the same command again."
    for command in commands:
        console.step(console.value(command))
        if subprocess.run(command, shell=True, check=False).returncode:
            return f"{console.value(command)} failed. Install Docker from {console.value(install_page())}, then run the same command again."
    if restart_pending():
        return RESTART_WINDOWS
    if platform.system() == "Darwin":
        plugins = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "cli-plugins"
        if not (plugins / "docker-buildx").exists():
            prefix = subprocess.run(["brew", "--prefix"], capture_output=True, text=True,
                                    check=False).stdout.strip()
            plugins.mkdir(parents=True, exist_ok=True)
            (plugins / "docker-buildx").unlink(missing_ok=True)
            (plugins / "docker-buildx").symlink_to(
                Path(prefix, "lib", "docker", "cli-plugins", "docker-buildx"))
    return ""


def start() -> str:
    """Start the installed Docker and wait until it answers. Returns "" when it does."""
    found = starter()
    if found is None:
        return "Docker is installed but not running. Start Docker, then run the same command again."
    name, command = found
    console.status(f"Starting {name}...")
    if platform.system() == "Windows":
        subprocess.Popen(command)
    elif subprocess.run(command, check=False).returncode:
        return f"{console.value(' '.join(command))} failed. Start Docker, then run the same command again."
    if name == "Docker Desktop":
        console.status("If Docker Desktop opens a window, answer it. pdt waits until Docker runs.")
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        said = docker_info()
        if said == "" or denied(said):
            return ""
        time.sleep(2)
    if restart_pending():
        return RESTART_WINDOWS
    return f"{name} did not start. Start it, wait until Docker is running, then run the same command again."


def join_docker_group() -> str:
    """Add this user to the docker group. A new group reaches only a new login, so the user
    also owns the socket until the Docker service restarts."""
    user = getpass.getuser()
    console.status(f"Adding {console.value(user)} to the docker group, so that docker works without sudo...")
    subprocess.run(["sudo", "usermod", "-aG", "docker", user], check=False)
    subprocess.run(["sudo", "chown", user, LINUX_SOCKET], check=False)
    if docker_info() == "":
        return ""
    return "Log out and log in again so that you can use Docker, then run the same command again."


def ensure(provider: str, assume_yes: bool) -> str:
    """Get Docker ready. Returns "" when docker answers, else the one step left for the user."""
    said = docker_info()
    if said is None and desktop() is not None:
        said = docker_info()
    if said == "":
        return ""
    if said is None and starter() is None:
        problem = install(provider, assume_yes)
        if problem:
            return problem
        said = docker_info()
    if said != "" and not denied(said):
        problem = start()
        if problem:
            return problem
        said = docker_info()
    if denied(said):
        problem = join_docker_group()
        if problem:
            return problem
    console.done("Docker is running.")
    return ""


def emulate(image_platform: str) -> str:
    """Let Docker on Linux build `image_platform` ("linux/arm64") on a computer of another
    CPU type. Returns "" when it can, else the one step left for the user."""
    cpu = image_platform.split("/")[1]
    if (platform.system() != "Linux" or CPU_TYPES.get(platform.machine().lower()) == cpu
            or (BINFMT_DIR / f"qemu-{QEMU_NAMES[cpu]}").exists()):
        return ""
    command = ["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", cpu]
    console.status(f"Docker on this computer cannot build for {console.value(cpu)}. "
                   f"Adding the {console.value(cpu)} emulator with {console.value(' '.join(command))}...")
    if subprocess.run(command, capture_output=True, text=True, check=False).returncode:
        return (f"{console.value(' '.join(command))} failed, so Docker cannot build the "
                f"{console.value(cpu)} image. Run it yourself, then run the same command again.")
    return ""
