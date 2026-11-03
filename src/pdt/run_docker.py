#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "python-dotenv",
#     "rich",
#     "backoff",
#     "fsspec",
# ]
# ///
"""Run a Docker app: the part run.py plays for a Python app.

usage: run_docker.py <app dir> [--state] [--image IMAGE] -- <command...>

<command> is what the app's Dockerfile starts (`pdt.docker_app`). In the
image of a deployed Docker app, DOCKER_APP_TAIL starts this module as
`python -m pdt.run_docker` from the image's own working folder, and it runs
the command there. `pdt run` starts it on this computer with --image, the
tag of the image it just built from the app's Dockerfile as it is, and it
runs the command in a container of that image with `docker run`.

Either way it loads the app's config and env (PDT_ENV_JSON secrets become
env vars), stops when an env var that config.yml lists as required is not
set, gives the command the app's merged `config:` section as JSON in
PDT_CONFIG_JSON and an empty folder in PDT_OUTPUT_DIR, and keeps what the
command leaves there, the same way as `pdt.run_powershell`
(`run_with_storage`). --state, which pdt passes when a file of the app
folder names PDT_STATE_DIR, fills PDT_STATE_DIR with the app's saved state
and saves it again when the command exits 0. It exits with the command's
code.

With --image, the two folders are mounted into the container at
/pdt/output and /pdt/state. `docker run` gets each env var of the app's
env spec by name only (`-e NAME`), so no value is on its command line, and
a _PATH var that names a file goes as <NAME>_B64, as a deploy sends it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt.run_powershell import run_with_storage
from pdt.utils.log import die

USAGE = "usage: run_docker.py <app dir> [--state] [--image IMAGE] -- <command...>"
MOUNTS = {"PDT_OUTPUT_DIR": "/pdt/output", "PDT_STATE_DIR": "/pdt/state"}


def parse(argv: list[str]) -> tuple[Path, bool, str, list[str]] | None:
    if "--" not in argv:
        return None
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    if not options or not command:
        return None
    app_dir, rest = Path(options[0]).resolve(), options[1:]
    keeps_state = "--state" in rest
    rest = [option for option in rest if option != "--state"]
    image = ""
    if rest[:1] == ["--image"] and len(rest) == 2:
        image, rest = rest[1], []
    if rest:
        return None
    return app_dir, keeps_state, image, command


def docker_command(image: str, command: list[str], names: list[str], output: Path,
                   state: Path) -> list[str]:
    """`docker run` of `command` in `image`, with the two storage folders mounted and each of
    `names` passed from this process's env."""
    run = ["docker", "run", "--rm", "-v", f"{output}:{MOUNTS['PDT_OUTPUT_DIR']}",
           "-v", f"{state}:{MOUNTS['PDT_STATE_DIR']}"]
    for name in names:
        run += ["-e", name]
    return run + ["--entrypoint", command[0], image, *command[1:]]


def local_env(app: dict, output: Path, state: Path) -> tuple[dict[str, str], list[str]]:
    """The env of `docker run` and the names it passes into the container: the secrets a
    deploy would send, PDT_CONFIG_JSON, PDT_RUN_ID, and the two mounted folders."""
    from pdt.deploy_common import gather_secrets
    secrets = gather_secrets(app)
    env = {**os.environ, **secrets, "PDT_CONFIG_JSON": json.dumps(app["config"], default=str),
           **MOUNTS}
    names = [*secrets, "PDT_CONFIG_JSON", "PDT_RUN_ID", *MOUNTS]
    for folder in (output, state):
        # The container's user may not be the user who made the folders.
        folder.chmod(0o777)
    return env, names


def main(argv: list[str]) -> int:
    parsed = parse(argv)
    if parsed is None:
        sys.stderr.write(USAGE + "\n")
        return 2
    app_dir, keeps_state, image, command = parsed
    try:
        app = config.merged_app(app_dir.name)
        config.load_env(app_dir)
    except config.ConfigError as exc:
        die(1, "config is not valid", problem=str(exc))
    missing = config.missing_env(app)
    if missing != "":
        die(1, f"env vars missing: {missing}")

    def run(output: Path, state: Path) -> int:
        if image:
            env, names = local_env(app, output, state)
            return subprocess.run(docker_command(image, command, names, output, state), env=env,
                                  stdin=subprocess.DEVNULL).returncode
        env = {**os.environ, "PDT_OUTPUT_DIR": str(output), "PDT_STATE_DIR": str(state),
               "PDT_CONFIG_JSON": json.dumps(app["config"], default=str)}
        try:
            return subprocess.run(command, env=env, stdin=subprocess.DEVNULL).returncode
        except OSError as exc:
            die(127, f"cannot start {command[0]}", problem=str(exc))

    return run_with_storage(app, keeps_state, run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
