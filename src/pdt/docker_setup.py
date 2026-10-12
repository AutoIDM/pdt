"""Get Docker ready for a cloud deploy, with no step the user must look up.

Every cloud provider builds its image with docker on this computer, with
`docker build --ssh`, which needs the buildx plugin.

- `docker info` answers: nothing to do, and nothing is printed.
- Docker is installed but not running: start it and wait until it answers.
- Docker is not installed: show the steps that install it, ask once
  (`--yes` skips the question), and run them. macOS with Homebrew gets
  Colima from Homebrew, and `brew services start colima` starts it now and
  at each login. macOS without Homebrew gets the release binaries of Colima,
  Lima, the docker CLI, and docker-buildx in DOCKER_DIR, each checked against
  its pinned sha256; the folder joins this run's PATH and each deploy starts
  Colima when it is stopped. Colima runs its Linux VM on Apple's
  Virtualization framework (macOS 13 or later), so it needs no QEMU, no
  administrator password, and no window. `colima delete` removes the VM,
  and deleting DOCKER_DIR removes the rest. On macOS 12 and older, Colima
  uses QEMU (abiosoft/colima `environment.DefaultVMType`), which pdt does
  not install, so pdt installs nothing there.
  Windows x64 gets Rancher Desktop from winget, with the Moby engine and no
  Kubernetes, started with no window; winget prints nothing. When
  `wsl --status` answers, WSL is installed and the MSI installs for this
  user only (MSIINSTALLPERUSER=1, under %LOCALAPPDATA%\\Programs), which
  needs no administrator; without WSL, it installs for every user. Before
  Rancher Desktop starts and WSL has no distribution, pdt marks the WSL
  welcome window as seen, because WSL opens it when the first distribution
  registers. An installed Docker Desktop is started instead. Linux gets
  Docker Engine from Docker's install script, pinned to a docker/docker-install
  commit and checked against its sha256.
- A docker CLI with no Docker to start (a stopped download, Homebrew's
  docker without Colima, a Linux docker with no docker.service unit) gets
  the same install, unless an engine may exist that pdt cannot start or that
  the user chose: DOCKER_HOST or a docker context of their own, a Docker
  that does not answer, or an engine app that pdt cannot start (OrbStack,
  Podman Desktop, a Docker Desktop outside /Applications, a desktop app that
  Windows lists in its installed programs). pdt then says what it found.
- On Linux, a user outside the docker group joins it. An install lists those
  commands in its steps; otherwise pdt shows them and asks once.
- A docker with no buildx plugin gets the pinned docker-buildx in
  DOCKER_DIR, linked into ~/.docker/cli-plugins, with no question.
- Docker builds for another CPU type (AWS's arm64 on an x86_64 computer)
  only through a QEMU handler in the kernel that runs Docker, which a
  restart removes. When `docker buildx inspect` does not list the image's
  platform, pdt runs tonistiigi/binfmt at a pinned digest, Docker's
  documented way, which registers a missing handler and lists the platforms
  that Docker can run. Docker Engine on Linux and Rancher Desktop on Windows
  need it. Docker Desktop and Colima register it themselves. The Rancher
  Desktop builder never lists an emulated platform, so pdt trusts the binfmt
  list, and prints a line only when binfmt adds the handler.
- When Rancher Desktop does not start, the last error lines of its
  background.log follow pdt's error.
- Anything else, Windows ARM64 and macOS 12 or older included: one line
  with the install page for this system.

To bump a pin, take the asset and its sha256 from the project's release:
abiosoft/colima (`colima-Darwin-<cpu>.sha256sum`), lima-vm/lima
(`SHA256SUMS`), and docker/buildx (`checksums.txt`, and
`checksums-signed.txt` for darwin). download.docker.com publishes no
checksum for the static docker CLI, so hash the downloaded tgz. For the
Linux install script, take the newest docker/docker-install commit and hash
its `install.sh`. For binfmt, take the digest of the newest `qemu-v*` tag on
Docker Hub.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pdt import console
from pdt.config import data_home
from pdt.download import fetch_verified

INSTALL_PAGES = {
    "Darwin": "https://docs.rancherdesktop.io/getting-started/installation/#macos",
    "Windows": "https://docs.rancherdesktop.io/getting-started/installation/#windows",
    "Linux": "https://docs.docker.com/engine/install/",
}
INSTALLERS = {
    "Darwin": ("brew", ["brew install colima docker docker-buildx", "brew services start colima"]),
    "Windows": ("winget", ["winget install -e --id SUSE.RancherDesktop --silent "
                           "--accept-source-agreements --accept-package-agreements "
                           "--disable-interactivity"]),
    "Linux": ("sudo", ["sudo systemctl enable --now docker"]),
}
DOCKER_INSTALL = ("https://raw.githubusercontent.com/docker/docker-install/"
                  "2b32480025b223ebfddae9a3a8bef09027680f53/install.sh",
                  "fefa50ccd50efb42f438b506fc3a88574118f314aaf2a7cd5b6e1ffb1bffcf26",
                  "install.sh", "install.sh")
RANCHER_START = ["start", "--container-engine.name=moby", "--kubernetes.enabled=false",
                 "--application.start-in-background", "--no-modal-dialogs"]
RANCHER_PER_USER = ' --custom "MSIINSTALLPERUSER=1"'
RANCHER_PROFILE = r"Software\Rancher Desktop\Profile\Defaults"
WSL_USER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
RANCHER_DEFAULTS = [("", "version", 19), ("containerEngine", "name", "moby"),
                    ("kubernetes", "enabled", 0)]
LINUX_SOCKET = "/var/run/docker.sock"
LOCAL_CONTEXTS = ("default", "colima", "desktop-linux", "rancher-desktop")
MAC_ENGINES = [("Docker Desktop", "Docker.app"), ("OrbStack", "OrbStack.app"),
               ("Podman Desktop", "Podman Desktop.app"), ("Rancher Desktop", "Rancher Desktop.app")]
WINDOWS_ENGINES = ("Docker Desktop", "Rancher Desktop")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
RESTART_WINDOWS = "Restart Windows to finish the Docker install, then run the same command again."
START_SECONDS = 300
FIRST_START_SECONDS = 900
INFO_SECONDS = 20
NO_ANSWER = "no answer"
ERROR_LINES = 3
MIN_MACOS = 13
BREW_COLIMA_SERVICE = Path("Library/LaunchAgents/homebrew.mxcl.colima.plist")
BINFMT_TAG = "qemu-v10.2.3"
BINFMT_DIGEST = "sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BINFMT = f"tonistiigi/binfmt:{BINFMT_TAG}@{BINFMT_DIGEST}"
DOCKER_DIR = data_home() / "pdt" / "docker"
BUILDX = "https://github.com/docker/buildx/releases/download/v0.38.0/buildx-v0.38.0."
BUILDX_PAGE = "https://github.com/docker/buildx#installing"
BUILDX_DOWNLOADS = {
    ("Darwin", "arm64"): (f"{BUILDX}darwin-arm64",
                          "85989b895add5f119c1ccf8eada2f6892fc33ccf16bd8917638eae404ef26344"),
    ("Darwin", "x86_64"): (f"{BUILDX}darwin-amd64",
                           "b123dc6fb51f88e47d37223938e2acd66edec73105459f65f19f15cc912ee559"),
    ("Linux", "x86_64"): (f"{BUILDX}linux-amd64",
                          "4fe4cc38adf48169132749b6ca22a990928db0118e3407584ee553723115d287"),
    ("Linux", "aarch64"): (f"{BUILDX}linux-arm64",
                           "38e890e1a162bfdbf32fbe91404991dc4fc28e55b0de901b34374a061bab4d2c"),
}
# Each download: url, sha256, the file it holds (empty for an archive that unpacks into
# DOCKER_DIR as it is), and that file's place in DOCKER_DIR. Colima comes last, so a stopped
# download leaves no colima on the PATH.
COLIMA_DOWNLOADS = {
    "arm64": [
        ("https://github.com/lima-vm/lima/releases/download/v2.2.1/lima-2.2.1-Darwin-arm64.tar.gz",
         "9e9eacce88f37e185c346bad73aa6136f738d8cdf8c3bb23cd42b071824bc66e", "", ""),
        ("https://download.docker.com/mac/static/stable/aarch64/docker-29.9.0.tgz",
         "c640fd61d9ba801e0ea282fd0df344d6943efd1994c7454537707cdb7a3d3e0b", "docker/docker",
         "bin/docker"),
        (*BUILDX_DOWNLOADS["Darwin", "arm64"], "buildx-v0.38.0.darwin-arm64", "bin/docker-buildx"),
        ("https://github.com/abiosoft/colima/releases/download/v0.10.3/colima-Darwin-arm64",
         "980ad8bf61a4ca370243f4cb41401a61276dcd2c2502bee7b9b86f9250169f34", "colima-Darwin-arm64",
         "bin/colima"),
    ],
    "x86_64": [
        ("https://github.com/lima-vm/lima/releases/download/v2.2.1/lima-2.2.1-Darwin-x86_64.tar.gz",
         "6b776e4bc41f358e6ee01fefcc78e2eb26f11635a0fcff632234af77a566c6c4", "", ""),
        ("https://download.docker.com/mac/static/stable/x86_64/docker-29.9.0.tgz",
         "65a7885c563226dba96c31d1a7aa78ee098199dc1c1f111582673544c91ad8de", "docker/docker",
         "bin/docker"),
        (*BUILDX_DOWNLOADS["Darwin", "x86_64"], "buildx-v0.38.0.darwin-amd64", "bin/docker-buildx"),
        ("https://github.com/abiosoft/colima/releases/download/v0.10.3/colima-Darwin-x86_64",
         "3082737fe8a98afda11cba7d9a20b6e56fe80c6153464beda04bec630758770b", "colima-Darwin-x86_64",
         "bin/colima"),
    ],
}


class DockerError(Exception):
    pass


def install_page() -> str:
    return INSTALL_PAGES.get(platform.system(), INSTALL_PAGES["Linux"])


def docker_info() -> str | None:
    """None when no docker is on the PATH, "" when Docker answers, else what docker said."""
    if shutil.which("docker") is None:
        return None
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False,
                              timeout=INFO_SECONDS)
    except subprocess.TimeoutExpired:
        return NO_ANSWER
    return "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip() or "failed"


def denied(said: str | None) -> bool:
    return platform.system() == "Linux" and "permission denied" in (said or "").lower()


def last_lines(proc: subprocess.CompletedProcess) -> str:
    """Dim lines under pdt's error: the last lines a failed command printed."""
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()[-ERROR_LINES:]
    return "".join(f"\n[dim]  {console.escape(line)}[/]" for line in lines)


def group_commands() -> list[list[str]]:
    """The commands that add this user to the docker group. A new group reaches only a new
    login, so the user also owns the socket until the Docker service restarts."""
    user = getpass.getuser()
    return [["sudo", "usermod", "-aG", "docker", user], ["sudo", "chown", user, LINUX_SOCKET]]


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


def on_path() -> None:
    """Put the Docker that pdt downloaded on this run's PATH, after any other Docker."""
    bin_dir = DOCKER_DIR / "bin"
    path = os.environ.get("PATH", "")
    if bin_dir.is_dir() and str(bin_dir) not in path.split(os.pathsep):
        os.environ["PATH"] = f"{path}{os.pathsep}{bin_dir}"


def download(downloads: list[tuple[str, str, str, str]]) -> None:
    """Fetch each (url, sha256, file, place) into DOCKER_DIR. Raises DockerError."""
    for url, sha256, file, place in downloads:
        stage = Path(tempfile.mkdtemp(prefix="pdt-docker-"))
        try:
            fetch_verified(url, sha256, stage, DockerError)
            if file:
                (DOCKER_DIR / place).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(stage / file, DOCKER_DIR / place)
                (DOCKER_DIR / place).chmod(0o755)
            else:
                shutil.copytree(stage, DOCKER_DIR, symlinks=True, dirs_exist_ok=True)
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def starter() -> tuple[str, list[str]] | None:
    """The installed Docker's name and the command that starts it. A Colima that
    `brew services` runs starts through it, so a second `colima start` does not race it."""
    app = desktop()
    if platform.system() == "Darwin" and shutil.which("colima"):
        context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True,
                                 check=False).stdout if shutil.which("docker") else ""
        if app is None or context.startswith("colima"):
            if (Path.home() / BREW_COLIMA_SERVICE).exists():
                return "Colima", ["brew", "services", "start", "colima"]
            return "Colima", ["colima", "start"]
    if app is not None:
        return app
    if (platform.system() == "Linux" and shutil.which("docker") and shutil.which("systemctl")
            and subprocess.run(["systemctl", "cat", "docker"], capture_output=True,
                               check=False).returncode == 0):
        return "the Docker service", ["sudo", "systemctl", "start", "docker"]
    return None


def engine_apps() -> list[str]:
    """The installed apps that run a Docker engine, including those that pdt cannot start."""
    if platform.system() == "Darwin":
        return [name for name, bundle in MAC_ENGINES
                if any((root / bundle).exists() for root in (Path("/Applications"),
                                                              Path.home() / "Applications"))]
    if platform.system() != "Windows":
        return []
    import winreg
    names = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as program:
                        try:
                            names.add(winreg.QueryValueEx(program, "DisplayName")[0])
                        except OSError:
                            pass
        except OSError:
            pass
    return [name for name in WINDOWS_ENGINES if name in names]


def unreachable(said: str | None) -> str:
    """What to do when an engine may exist that pdt cannot start or that the user chose, so
    pdt installs no other. "" when pdt can tell that no such engine exists."""
    host = os.environ.get("DOCKER_HOST")
    if host:
        return (f"pdt cannot reach the Docker engine that {console.value('DOCKER_HOST')} names, "
                f"{console.value(host)}. Start it, then run the same command again.")
    context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True,
                             check=False).stdout.strip() if said is not None else ""
    linux_desktop = platform.system() == "Linux" and context == "desktop-linux"
    if context and (context not in LOCAL_CONTEXTS or linux_desktop):
        return (f"pdt cannot reach the Docker engine that the docker context "
                f"{console.value(context)} names. Start it, then run the same command again.")
    if said == NO_ANSWER:
        return (f"The Docker engine does not answer {console.value('docker info')} in "
                f"{INFO_SECONDS} seconds. Restart it, then run the same command again.")
    apps = engine_apps()
    if apps:
        return f"pdt found {apps[0]} but cannot start it; start it, then run the same command again."
    return ""


def install(provider: str, assume_yes: bool) -> str:
    """Install Docker once the user agrees. Returns "" when it is installed, else what to do."""
    from pdt.deploy import proceed

    absent = ("Docker has no engine on this computer." if shutil.which("docker")
              else "Docker is not installed.")
    missing = f"{absent} pdt builds the image for {provider} with Docker on this computer."
    tool, commands = INSTALLERS.get(platform.system(), ("", []))
    macos = platform.mac_ver()[0]
    if platform.system() == "Darwin" and int(macos.split(".")[0] or 0) < MIN_MACOS:
        return (f"{missing} The Colima that pdt installs needs macOS {MIN_MACOS} or later, and "
                f"this Mac runs macOS {console.value(macos)}. Install Docker from "
                f"{console.value(install_page())}, then run the same command again.")
    downloads = []
    if platform.system() == "Darwin" and shutil.which(tool) is None:
        downloads = COLIMA_DOWNLOADS.get(platform.machine(), [])
    if (not commands or not downloads and shutil.which(tool) is None
            or (platform.system(), platform.machine()) == ("Windows", "ARM64")):
        return f"{missing} Install Docker from {console.value(install_page())}, then run the same command again."
    if platform.system() == "Windows" and wsl_installed():
        commands = [commands[0] + RANCHER_PER_USER]
    console.warn(missing)
    console.say("pdt will install Docker with these steps:")
    if platform.system() == "Linux":
        script = DOCKER_DIR / DOCKER_INSTALL[3]
        downloads = [DOCKER_INSTALL]
        commands = [f"sudo sh {shlex.quote(str(script))}", *commands]
        console.bullet(f"Download {console.value(DOCKER_INSTALL[0])} to {console.value(script)}, "
                       "and check its sha256.")
        for command in [*commands, *(" ".join(command) for command in group_commands())]:
            console.bullet(console.value(command))
        console.bullet(f"Add the QEMU emulator with {console.value('tonistiigi/binfmt')} when the "
                       "image is for another CPU type.")
    elif downloads:
        console.bullet(f"Download Colima, Lima, the docker CLI, and docker-buildx to "
                       f"{console.value(DOCKER_DIR)}, and check each file's sha256:")
        for url, *_ in downloads:
            console.bullet(console.value(url), indent=4)
        plugins = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "cli-plugins"
        console.bullet(f"Link docker-buildx into {console.value(plugins)}.")
        console.bullet(f"Run {console.value('colima start')}, now and on each deploy that finds "
                       "Colima stopped.")
        commands = []
    else:
        for command in commands:
            console.bullet(console.value(command))
    if not proceed(assume_yes):
        return f"{absent} Install Docker from {console.value(install_page())}, then run the same command again."
    try:
        download(downloads)
    except DockerError as e:
        return f"{console.escape(str(e))}\nRun the same command again."
    on_path()
    for command in commands:
        console.step(console.value(command))
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              errors="replace", check=False)
        if proc.returncode:
            if restart_pending():
                return RESTART_WINDOWS
            return (f"{console.value(command)} failed. Install Docker from "
                    f"{console.value(install_page())}, then run the same command again."
                    + last_lines(proc))
    if platform.system() == "Windows":
        rancher_profile()
    if restart_pending():
        return RESTART_WINDOWS
    return ""


def buildx() -> str:
    """Give docker the buildx plugin when it has none: Homebrew's when Homebrew installed it,
    else the pinned download in DOCKER_DIR. Returns "" when docker has it, else what to do."""
    if subprocess.run(["docker", "buildx", "version"], capture_output=True,
                      check=False).returncode == 0:
        return ""
    source = DOCKER_DIR / "bin" / "docker-buildx"
    if shutil.which("brew"):
        prefix = subprocess.run(["brew", "--prefix"], capture_output=True, text=True,
                                check=False).stdout.strip()
        brewed = Path(prefix, "lib", "docker", "cli-plugins", "docker-buildx")
        if brewed.exists():
            source = brewed
    pinned = BUILDX_DOWNLOADS.get((platform.system(), platform.machine()))
    if not source.exists() and pinned is None:
        return (f"Docker has no buildx plugin, which {console.value('docker build --ssh')} needs. "
                f"Install it from {console.value(BUILDX_PAGE)}, then run the same command again.")
    if not source.exists():
        try:
            download([(*pinned, Path(pinned[0]).name, "bin/docker-buildx")])
        except DockerError as e:
            return f"{console.escape(str(e))}\nRun the same command again."
    plugins = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "cli-plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "docker-buildx").unlink(missing_ok=True)
    (plugins / "docker-buildx").symlink_to(source)
    console.status(f"Linked {console.value(source)} into {console.value(plugins)}, so docker "
                   "has the buildx plugin.")
    return ""


def start(first: bool) -> str:
    """Start the installed Docker and wait until it answers. Returns "" when it does. The
    first start after an install also downloads the Linux VM image or WSL distribution, and
    so does the first start of a Rancher Desktop that has no settings file yet."""
    found = starter()
    if found is None:
        return "Docker is installed but not running. Start Docker, then run the same command again."
    name, command = found
    if name == "Rancher Desktop":
        config = (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
                  if platform.system() == "Windows" else Path.home() / "Library" / "Preferences")
        first = first or not (config / "rancher-desktop" / "settings.json").exists()
    console.status(f"Starting {name}...")
    if first:
        console.status("The first start takes several minutes.")
    if platform.system() == "Windows":
        if name == "Rancher Desktop":
            skip_wsl_welcome()
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              check=False)
        if proc.returncode:
            return (f"{console.value(' '.join(command))} failed. Start Docker, then run the same "
                    "command again." + last_lines(proc))
    if name == "Docker Desktop":
        console.status("If Docker Desktop opens a window, answer it. pdt waits until Docker runs.")
    deadline = time.monotonic() + (FIRST_START_SECONDS if first else START_SECONDS)
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
             and "UnhandledRejectionWarning" not in line][-ERROR_LINES:]
    if not found:
        return ""
    return "".join([f"\n[dim]Rancher Desktop's log {console.value(logs / 'background.log')} ends with:[/]",
                    *(f"\n[dim]  {console.escape(line)}[/]" for line in found)])


def join_docker_group(assume_yes: bool, listed: bool) -> str:
    """Add this user to the docker group. Unless an install plan `listed` the commands, show
    them and ask the one question (`assume_yes` skips it)."""
    from pdt.deploy import proceed

    user = getpass.getuser()
    commands = group_commands()
    if not listed:
        console.warn(f"Docker answers {console.value(user)} only through sudo.")
        console.say("pdt will add you to the docker group with these steps:")
        for command in commands:
            console.bullet(console.value(" ".join(command)))
        if not proceed(assume_yes):
            return (f"Add {console.value(user)} to the docker group with "
                    f"{console.value(' '.join(commands[0]))}, log out and log in again, then "
                    "run the same command again.")
    console.status(f"Adding {console.value(user)} to the docker group, so that docker works without sudo...")
    for command in commands:
        subprocess.run(command, capture_output=True, check=False)
    if docker_info() == "":
        return ""
    return "Log out and log in again so that you can use Docker, then run the same command again."


def ensure(provider: str, assume_yes: bool) -> str:
    """Get Docker ready. Returns "" when docker answers, else the one step left for the user."""
    on_path()
    said = docker_info()
    if said is None and desktop() is not None:
        said = docker_info()
    if said == "":
        return buildx()
    installed = False
    if not denied(said) and starter() is None:
        problem = unreachable(said) or install(provider, assume_yes)
        if problem:
            return problem
        installed = True
        said = docker_info()
    if said != "" and not denied(said):
        problem = start(installed)
        if problem:
            return problem
        said = docker_info()
    if denied(said):
        problem = join_docker_group(assume_yes, installed)
        if problem:
            return problem
    console.done("Docker is running.")
    return buildx()


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
    command = ["docker", "run", "--privileged", "--rm", BINFMT, "--install", cpu]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    installing = [line for line in proc.stderr.splitlines() if line.startswith("installing:")]
    try:
        supported = json.loads(proc.stdout)["supported"]
    except (ValueError, KeyError, TypeError):
        supported = []
    if not proc.returncode and image_platform in supported:
        if f"installing: {cpu} OK" in installing:
            console.status(f"Added the {console.value(cpu)} emulator with "
                           f"{console.value(' '.join(command))}, so Docker can build the "
                           f"{console.value(cpu)} image.")
        return ""
    reason = (installing or proc.stderr.strip().splitlines() or ["no output"])[-1]
    return (f"{console.value(' '.join(command))} failed, so Docker cannot build the "
            f"{console.value(cpu)} image: {console.value(reason)}. "
            "Run it yourself, then run the same command again.")
