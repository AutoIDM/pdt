"""`pdt ci`: a merge request rebuild from GitLab CI, through a CI installation.

`pdt ci register` makes a key in memory, runs the device flow with `kind: "ci"`, and prints the four
values to store as masked CI variables. It writes no file. A CI installation has rights for one
project: publish a snapshot, start `run_snapshot` on the named target installations, and read that
command's state, output, and artifact.

`pdt ci rebuild APP --installation INS` publishes the project as a `merge_request` snapshot,
which writes no head, starts `run_snapshot` on the target, prints each output chunk to standard
output, downloads the artifact and the log into the artifacts folder, and exits with the result
of the run. A target is a computer with the pdt service, so a project that deploys to a cloud
provider has no rebuild here.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pdt import config, console, register_cli, snapshots, sync_cli
from pdt.utils import autoidm

POLL_SECONDS = 5
FINAL_STATES = ("done", "failed", "refused", "cancelled", "interrupted", "expired", "unknown")
DOWNLOAD_TIMEOUT_SECONDS = 300
ARTIFACT_NAME = "artifacts.zip"


def register(project: str, targets: list[str], api_url: str | None) -> int:
    api_url = (api_url or register_cli.default_api_url()).rstrip("/")
    key = Ed25519PrivateKey.generate()
    body = register_cli.device_start_body(key, "ci", {"project": project, "targets": targets})
    answer, _user_code = register_cli.approve_in_browser(api_url, body)
    if answer is None:
        return 1
    for name, value in ((autoidm.ID_ENV, answer["installation_id"]),
                        (autoidm.KEY_ENV, autoidm.private_pem(key).decode()),
                        (autoidm.URL_ENV, api_url), (autoidm.CUSTOMER_ENV, answer["customer_id"])):
        console.data(f"{name}={value}")
    console.next_steps([
        (", ".join(autoidm.SECRET_NAMES), "store each value above as a masked CI variable of the "
                                          "project; leave it unprotected, because a merge request "
                                          "pipeline runs on an unprotected branch"),
        (f"pdt service author add {project} {autoidm.public_key(key)}",
         "run this one time as an administrator on each target computer"),
    ])
    return 0


def place_problem(app: str) -> str:
    provider = config.merged_app(app)["platform"].get("provider")
    if provider in (None, "windows"):
        return ""
    return (f"{app} deploys to {provider}. A merge request rebuild runs on a computer with the "
            "pdt service, so pdt ci rebuild takes only an app of the windows provider.")


def publish(app: str, ref: str | None, label: str | None) -> tuple[str, str, autoidm.Registration]:
    """Publish the project as a merge request snapshot. Returns the project, the snapshot, and the
    CI registration."""
    root, name, customer, registration = sync_cli.context()
    if registration.source != autoidm.ID_ENV:
        raise config.ConfigError(f"pdt ci rebuild reads {', '.join(autoidm.SECRET_NAMES)} from "
                                 "the environment. Run pdt ci register, and store its values as "
                                 "CI variables.")
    files, _left_out = snapshots.collect(root)
    with tempfile.TemporaryDirectory(prefix="pdt-ci-") as folder:
        with console.waiting(f"sending: snapshot of {name}"):
            identity = snapshots.publish(registration, customer, name, root, files,
                                         "merge_request", ref, label, None, Path(folder))
    console.done(f"Sent {console.value(name)} as snapshot {console.value(identity)}.")
    return name, identity, registration


def follow(registration: autoidm.Registration, command_id: str, installation: str) -> dict:
    """Print each output chunk until the command reaches a final state. Returns the last answer."""
    after = -1
    while True:
        answer = autoidm.post(registration, "/v1/ci/commands/get", {
            "version": 1, "command_id": command_id, "installation_id": installation,
            "after": after})
        if answer.get("output"):
            console.data(str(answer["output"]).rstrip("\n"))
        if type(answer.get("output_seq")) is int:
            after = answer["output_seq"]
        if answer.get("state") in FINAL_STATES:
            return answer
        time.sleep(POLL_SECONDS)


def download(url: str, target: Path) -> None:
    try:
        with urllib.request.urlopen(autoidm.check_url(url, "/v1/ci/commands/artifact"),
                                    timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            target.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as e:
        raise autoidm.AutoidmError(f"pdt could not download {target.name}: "
                                   f"{getattr(e, 'reason', e)}") from None


def fetch_files(registration: autoidm.Registration, command_id: str, installation: str,
                folder: Path) -> None:
    """The log and the unpacked artifact, in `folder`."""
    answer = autoidm.post(registration, "/v1/ci/commands/artifact", {
        "version": 1, "command_id": command_id, "installation_id": installation})
    log = answer.get("log")
    if isinstance(log, dict) and log.get("url"):
        download(log["url"], folder / f"{command_id}.log")
    artifact = answer.get("artifact")
    if isinstance(artifact, dict) and artifact.get("url"):
        archive = folder / ARTIFACT_NAME
        download(artifact["url"], archive)
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                if snapshots.path_problem(info.filename) or info.is_dir():
                    continue
                zipped.extract(info, folder)
        archive.unlink()
        console.done(f"Saved the artifacts in {console.value(folder)}.")


def exit_code(result: dict | None, state: str) -> int:
    if state == "done":
        return 0
    code = (result or {}).get("exit_code")
    if state == "failed" and type(code) is int and code > 0:
        return code
    return 1


def rebuild(app: str, installation: str, preset: str | None, artifacts: str | None,
            ref: str | None, label: str | None, request_id: str | None) -> int:
    folder = Path(artifacts or "pdt-artifacts")
    folder.mkdir(parents=True, exist_ok=True)
    problem = place_problem(app)
    if problem:
        console.error(console.escape(problem))
        return 1
    ref = ref or os.environ.get("CI_COMMIT_SHA") or None
    if label is None and os.environ.get("CI_MERGE_REQUEST_IID"):
        label = f"MR {os.environ['CI_MERGE_REQUEST_IID']}"
    if request_id is None:
        job = os.environ.get("CI_JOB_ID")
        request_id = f"ci:{job}" if job else f"ci:{secrets.token_hex(8)}"
    project, identity, registration = publish(app, ref, label)
    body = {"version": 1, "request_id": request_id, "project": project, "snapshot_id": identity,
            "app": app, "installation_id": installation}
    if preset:
        body["preset"] = preset
    command_id = autoidm.post(registration, "/v1/ci/commands", body)["command_id"]
    console.say(f"Started {console.value(command_id)} on {console.value(installation)}.")
    answer = follow(registration, command_id, installation)
    state = answer["state"]
    result = answer.get("result") if isinstance(answer.get("result"), dict) else None
    try:
        fetch_files(registration, command_id, installation, folder)
    except (autoidm.AutoidmError, zipfile.BadZipFile) as e:
        console.warn(f"pdt did not get the files of {console.value(command_id)}: "
                     f"{console.escape(str(e))}")
    console.field("State", state)
    if result and result.get("message"):
        console.field("Message", str(result["message"]))
    return exit_code(result, state)
