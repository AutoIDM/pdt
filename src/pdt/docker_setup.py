"""Get Docker ready for a cloud deploy, with no step the user must look up.

Every cloud provider builds its image with docker on this computer, with
`docker build --ssh`, which needs the buildx plugin.

- `docker info` answers: nothing to do, and nothing is printed.
- Docker is installed but not running: start it and wait until it answers.
- Docker is not installed: show the commands that install it, ask once
  (`--yes` skips the question), and run them. macOS with Homebrew gets
  Colima, which needs no administrator password, no license, and no window.
  Windows x64 gets Rancher Desktop from winget, with the Moby engine and no
  Kubernetes, started with no window; winget prints nothing. When
  `wsl --status` answers, WSL is installed and the MSI installs for this
  user only (MSIINSTALLPERUSER=1, under %LOCALAPPDATA%\\Programs), which
  needs no administrator; without WSL, it installs for every user. Before
  Rancher Desktop starts and WSL has no distribution, pdt marks the WSL
  welcome window as seen, because WSL opens it when the first distribution
  registers. An installed Docker Desktop is started instead. Linux gets
  Docker Engine from get.docker.com.
- On Linux, a user outside the docker group joins it.
- Docker builds for another CPU type (AWS's arm64 on an x86_64 computer)
  only through a QEMU handler in the kernel that runs Docker, which a
  restart removes. When `docker buildx inspect` does not list the image's
  platform, pdt runs tonistiigi/binfmt, Docker's documented way, which
  registers a missing handler and lists the platforms that Docker can run.
  Docker Engine on Linux and Rancher Desktop on Windows need it. Docker
  Desktop and Colima register it themselves. The Rancher Desktop builder
  never lists an emulated platform, so pdt trusts the binfmt list, and
  prints a line only when binfmt adds the handler.
- When Rancher Desktop does not start, the last error lines of its
  background.log follow pdt's error.
- Anything else, Windows ARM64 included: one line with the install page for
  this system.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from pdt import console

INSTALL_PAGES = {
    "Darwin": "https://docs.rancherdesktop.io/getting-started/installation/#macos",
    "Windows": "https://docs.rancherdesktop.io/getting-started/installation/#windows",
    "Linux": "https://docs.docker.com/engine/install/",
}
INSTALLERS = {
    "Darwin": ("brew", ["brew install colima docker docker-buildx"]),
    "Windows": ("winget", ["winget install -e --id SUSE.RancherDesktop --silent "
                           "--accept-source-agreements --accept-package-agreements "
                           "--disable-interactivity"]),
    "Linux": ("curl", ["curl -fsSL https://get.docker.com | sudo sh",
                       "sudo systemctl enable --now docker"]),
}
RANCHER_START = ["start", "--container-engine.name=moby", "--kubernetes.enabled=false",
                 "--application.start-in-background", "--no-modal-dialogs"]
RANCHER_PER_USER = ' --custom "MSIINSTALLPERUSER=1"'
RANCHER_PROFILE = r"Software\Rancher Desktop\Profile\Defaults"
WSL_USER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
RANCHER_DEFAULTS = [("", "version", 19), ("containerEngine", "name", "moby"),
                    ("kubernetes", "enabled", 0)]
LINUX_SOCKET = "/var/run/docker.sock"
RESTART_WINDOWS = "Restart Windows to finish the Docker install, then run the same command again."
START_SECONDS = 300
RANCHER_ERROR_LINES = 3


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
    """Windows waits for a restart, for example to turn on the WSL features Rancher Desktop needs."""
    if platform.system() != "Windows":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                            r"\Component Based Servicing\RebootPending"):
            return True
    except OSError:
        return False


def desktop() -> tuple[str, list[str]] | None:
    """The installed desktop app that runs Docker, and the command that starts it. Its docker
    joins this run's PATH, because a new install reaches only the terminals opened after it."""
    programs = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    rancher = [root / "Rancher Desktop" / "resources" / "resources" / "win32" / "bin"
               for root in (programs, local / "Programs")]
    mac_rancher = Path("/Applications/Rancher Desktop.app/Contents/Resources/resources/darwin/bin")
    apps = {
        "Darwin": [("Docker Desktop", Path("/Applications/Docker.app/Contents/Resources/bin"),
                    ["open", "-a", "/Applications/Docker.app"]),
                   ("Rancher Desktop", mac_rancher, [str(mac_rancher / "rdctl"), *RANCHER_START])],
        "Windows": [("Docker Desktop", programs / "Docker" / "Docker" / "resources" / "bin",
                     [str(programs / "Docker" / "Docker" / "Docker Desktop.exe")]),
                    *(("Rancher Desktop", bin_dir, [str(bin_dir / "rdctl.exe"), *RANCHER_START])
                      for bin_dir in rancher)],
    }
    for name, bin_dir, command in apps.get(platform.system(), []):
        if bin_dir.exists():
            if shutil.which("docker") is None:
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            return name, command
    return None


def rancher_profile() -> None:
    """Write the profile that Rancher Desktop reads on its first start: the Moby engine, no
    Kubernetes, and no first-run dialog. It is the user's profile, because the profile under
    Software\\Policies needs an administrator."""
    import winreg
    for subkey, name, value in RANCHER_DEFAULTS:
        path = f"{RANCHER_PROFILE}\\{subkey}" if subkey else RANCHER_PROFILE
        kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, name, 0, kind, value)


def wsl_installed() -> bool:
    return shutil.which("wsl") is not None and subprocess.run(
        ["wsl", "--status"], capture_output=True, check=False).returncode == 0


def skip_wsl_welcome() -> None:
    """Mark the WSL welcome window as seen when WSL has no distribution yet, because WSL opens
    it when Rancher Desktop registers the first one. A distribution is a {GUID} subkey."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WSL_USER_KEY) as key:
        try:
            if winreg.QueryValueEx(key, "OOBEComplete")[0]:
                return
        except OSError:
            pass
        if any(winreg.EnumKey(key, i).startswith("{") for i in range(winreg.QueryInfoKey(key)[0])):
            return
        winreg.SetValueEx(key, "OOBEComplete", 0, winreg.REG_DWORD, 1)


def starter() -> tuple[str, list[str]] | None:
    """The installed Docker's name and the command that starts it."""
    app = desktop()
    if platform.system() == "Darwin" and shutil.which("colima"):
        context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True,
                                 check=False).stdout if shutil.which("docker") else ""
        if app is None or context.startswith("colima"):
            return "Colima", ["colima", "start"]
    if app is not None:
        return app
    if platform.system() == "Linux" and shutil.which("docker") and shutil.which("systemctl"):
        return "the Docker service", ["sudo", "systemctl", "start", "docker"]
    return None


def install(provider: str, assume_yes: bool) -> str:
    """Install Docker once the user agrees. Returns "" when it is installed, else what to do."""
    from pdt.deploy import proceed

    missing = (f"Docker is not installed. pdt builds the image for {provider} with Docker "
               "on this computer.")
    tool, commands = INSTALLERS.get(platform.system(), ("", []))
    if (not commands or shutil.which(tool) is None
            or (platform.system(), platform.machine()) == ("Windows", "ARM64")):
        return f"{missing} Install Docker from {console.value(install_page())}, then run the same command again."
    if platform.system() == "Windows" and wsl_installed():
        commands = [commands[0] + RANCHER_PER_USER]
    console.warn(missing)
    console.say("pdt will install Docker with these commands:")
    for command in commands:
        console.bullet(console.value(command))
    if not proceed(assume_yes):
        return f"Docker is not installed. Install Docker from {console.value(install_page())}, then run the same command again."
    for command in commands:
        console.step(console.value(command))
        if subprocess.run(command, shell=True, check=False,
                          capture_output=platform.system() == "Windows").returncode:
            return f"{console.value(command)} failed. Install Docker from {console.value(install_page())}, then run the same command again."
    if platform.system() == "Windows":
        rancher_profile()
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
        if name == "Rancher Desktop":
            skip_wsl_welcome()
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    problem = f"{name} did not start. Start it, wait until Docker is running, then run the same command again."
    if name == "Rancher Desktop":
        problem += rancher_errors()
    return problem


def rancher_errors() -> str:
    """Dim lines under pdt's error: the last error lines of Rancher Desktop's background.log."""
    logs = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "rancher-desktop" / "logs" if platform.system() == "Windows"
            else Path.home() / "Library" / "Logs" / "rancher-desktop")
    try:
        lines = (logs / "background.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    found = [line for line in lines if ("Error" in line or "unable to start" in line)
             and "UnhandledRejectionWarning" not in line][-RANCHER_ERROR_LINES:]
    if not found:
        return ""
    return "".join([f"\n[dim]Rancher Desktop's log {console.value(logs / 'background.log')} ends with:[/]",
                    *(f"\n[dim]  {console.escape(line)}[/]" for line in found)])


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
    """Let Docker build `image_platform` ("linux/arm64") on a computer of another CPU type.
    Returns "" when it can, else the error and the one step left for the user."""
    proc = subprocess.run(["docker", "buildx", "inspect"], capture_output=True, text=True,
                          check=False)
    supported = [name.strip().rstrip("*") for line in proc.stdout.splitlines()
                 if line.startswith("Platforms:") for name in line.split(":", 1)[1].split(",")]
    if proc.returncode or image_platform in supported:
        return ""
    cpu = image_platform.split("/")[1]
    command = ["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", cpu]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    installing = [line for line in proc.stderr.splitlines() if line.startswith("installing:")]
    if not proc.returncode and image_platform in json.loads(proc.stdout)["supported"]:
        if f"installing: {cpu} OK" in installing:
            console.status(f"Added the {console.value(cpu)} emulator with "
                           f"{console.value(' '.join(command))}, so Docker can build the "
                           f"{console.value(cpu)} image.")
        return ""
    reason = (installing or proc.stderr.strip().splitlines() or ["no output"])[-1]
    return (f"{console.value(' '.join(command))} failed, so Docker cannot build the "
            f"{console.value(cpu)} image: {console.value(reason)}. "
            "Run it yourself, then run the same command again.")
