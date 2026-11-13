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
import subprocess
from pathlib import Path

from pdt import config, console
from pdt.deploy_common import docker_problem, stage_build_context, write_dockerfile
from pdt.profiles import web


def read_root() -> Path:
    """Where `pdt run` keeps every app's storage: the parent of each app's folder."""
    return config.find_project() / ".pdt" / "storage"


def run(app: dict, local_url: str) -> int:
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
    command = ["docker", "run", "--rm", "--init", "-p", f"127.0.0.1:{port}:{port}",
               "-e", f"{web.ENV_PORT}={port}", "-e", f"{web.ENV_URL}={local_url}",
               "-e", f"{web.ENV_STORAGE_READ_DIR}={web.STORAGE_READ_DIR}"]
    for other in app["storage_read"]:
        folder = read_root() / other
        folder.mkdir(parents=True, exist_ok=True)
        command += ["-v", f"{folder}:{web.STORAGE_READ_DIR}/{other}:ro"]
    spec = config.env_spec(app)
    for name in [*(spec.get("required") or []), *(spec.get("optional") or [])]:
        if os.environ.get(name, "") != "":
            command += ["-e", name]
    command.append(image)
    console.step(f"running {console.value(image)}")
    try:
        return subprocess.run(command).returncode
    except KeyboardInterrupt:
        return 0
