"""`pdt run` for a web app that is a container: build its Dockerfile and run it with Docker.

The container gets the same env a deployed web app gets: PDT_PORT and PDT_URL,
PDT_STORAGE_READ_DIR with each `storage.read` app's local storage folder
(<project>/.pdt/storage/<app>/, the folder `pdt run` of that app writes) mounted
read-only under it, and each env var the app's config lists that is set. The
port is published on 127.0.0.1 only. Ctrl+C stops the container.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable

from pdt import config, console
from pdt.deploy_common import docker_problem, stage_build_context, write_dockerfile
from pdt.profiles import web

# How long a stopped server may take to exit before pdt kills it.
STOP_WAIT = 10


def read_root() -> Path:
    """Where `pdt run` keeps every app's storage: the parent of each app's folder."""
    return config.find_project() / ".pdt" / "storage"


def run(app: dict, local_url: str, started: Callable[[], object] = lambda: None) -> int:
    """Build and run the container; `started` runs once the container starts."""
    problem = docker_problem()
    if problem:
        console.error(f"{console.value(app['name'])} is a container, so {console.value('pdt run')} "
                      f"needs Docker: {console.escape(problem)}")
        return 1
    image = f"pdt-{app['name']}:local"
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        console.step(f"building {console.value(image)}")
        built = subprocess.run(["docker", "build", "-t", image, str(stage)])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if built.returncode != 0:
        return built.returncode
    port = web.port(app)
    # A name of its own, so a second `pdt run` never removes the first one's container.
    container = f"pdt-{app['name']}-local-{os.getpid()}"
    command = ["docker", "run", "--rm", "--init", "--name", container,
               "-p", f"127.0.0.1:{port}:{port}",
               "-e", f"{web.ENV_PORT}={port}", "-e", f"{web.ENV_URL}={local_url}",
               "-e", f"{web.ENV_STORAGE_READ_DIR}={web.STORAGE_READ_DIR}"]
    for other in app["storage_read"]:
        folder = read_root() / other
        folder.mkdir(parents=True, exist_ok=True)
        command += ["-v", f"{folder}:{web.STORAGE_READ_DIR}/{other}:ro"]
    spec = config.env_spec(app)
    for name in [*(spec.get("required") or []), *(spec.get("optional") or []),
                 *(name for group in spec.get("one_of") or [] for name in group)]:
        if os.environ.get(name, "") != "":
            command += ["-e", name]
    command.append(image)
    console.step(f"running {console.value(image)}")
    process = start(command)
    started()
    return serve(app["name"], process, container)


def start(command: list[str], **options) -> subprocess.Popen:
    """Start a server in its own process group, so that pdt stops the whole group: the server
    and every process it starts."""
    if os.name == "nt":
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                                **options)
    return subprocess.Popen(command, start_new_session=True, **options)


def interrupt(process: subprocess.Popen) -> None:
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def kill(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGKILL)


def serve(name: str, process: subprocess.Popen, container: str = "") -> int:
    """Wait for a web app's server. Ctrl+C reaches only pdt, because the server has its own
    process group; pdt stops the group, kills it after STOP_WAIT, and stops the container."""
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            interrupt(process)
            process.wait(STOP_WAIT)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # The first process can exit while another process of the group still runs.
        try:
            kill(process)
        except OSError:
            pass
        process.wait()
        console.done(f"Stopped {console.value(name)}.")
        return 0
    finally:
        if container:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True)
