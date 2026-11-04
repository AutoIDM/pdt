import io
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from service_fixtures import COMMAND, CUSTOMER, INSTALLATION, FakePortal

from pdt import ci_cli
from pdt.utils import autoidm

TARGET = "ins_0123456789"


@pytest.fixture
def portal():
    found = FakePortal()
    yield found
    found.close()


@pytest.fixture
def project(tmp_path, monkeypatch, portal):
    root = tmp_path / "example"
    (root / "hr").mkdir(parents=True)
    (root / "pdt.yml").write_text(f"platform:\n  provider: windows\nmanaged_services:\n"
                                  f"  customer: {CUSTOMER}\n")
    (root / "hr" / "config.yml").write_text("schedule: daily\n")
    (root / "hr" / "run.py").write_text("print('hi')\n")
    monkeypatch.chdir(root)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.setenv(autoidm.ID_ENV, INSTALLATION)
    monkeypatch.setenv(autoidm.KEY_ENV, autoidm.private_pem(Ed25519PrivateKey.generate()).decode())
    monkeypatch.setenv(autoidm.URL_ENV, portal.url)
    monkeypatch.setenv(autoidm.CUSTOMER_ENV, CUSTOMER)
    monkeypatch.setenv("CI_COMMIT_SHA", "abc123")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "12")
    monkeypatch.setenv("CI_JOB_ID", "777")
    monkeypatch.setattr(ci_cli, "POLL_SECONDS", 0)
    return root


def zip_of(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


def serve_run(portal, states, result, artifact=None):
    portal.routes["/v1/ci/commands"] = lambda _body: (201, {"command_id": COMMAND,
                                                            "signalled": True})
    answers = [{"command_id": COMMAND, "state": state, "result": None,
                "output": f"line {index}\n", "output_seq": index}
               for index, state in enumerate(states)]
    answers[-1]["result"] = result
    portal.routes["/v1/ci/commands/get"] = lambda _body: (200, answers.pop(0))
    if artifact is not None:
        portal.zips["artifact"] = artifact
    portal.zips["log"] = b"the log\n"
    portal.routes["/v1/ci/commands/artifact"] = lambda _body: (200, {
        "artifact": {"url": f"{portal.url}/zip/artifact", "filename": "artifacts.zip"}
        if artifact is not None else None,
        "log": {"url": f"{portal.url}/zip/log", "filename": f"{COMMAND}.log"}})


def test_rebuild_publishes_runs_follows_and_saves_the_artifacts(project, portal, capsys):
    serve_run(portal, ["accepted", "running", "done"], {"outcome": "done", "exit_code": 0},
              zip_of({"report.csv": b"a,b\n", "../evil.txt": b"x"}))
    assert ci_cli.rebuild("hr", TARGET, "rebuild", None, None, None, None) == 0
    start = portal.bodies("/v1/snapshots/start")[0]
    assert start["purpose"] == "merge_request"
    assert (start["publication"]["ref"], start["publication"]["label"]) == ("abc123", "MR 12")
    command = portal.bodies("/v1/ci/commands")[0]
    assert command == {"version": 1, "request_id": "ci:777", "project": "example",
                       "snapshot_id": start["snapshot_id"], "app": "hr",
                       "installation_id": TARGET, "preset": "rebuild"}
    afters = [body["after"] for body in portal.bodies("/v1/ci/commands/get")]
    assert afters == [-1, 0, 1]
    out = capsys.readouterr().out
    assert "line 0\nline 1\nline 2" in out
    folder = project / "pdt-artifacts"
    assert (folder / "report.csv").read_text() == "a,b\n"
    assert (folder / f"{COMMAND}.log").read_text() == "the log\n"
    assert not (project / "evil.txt").exists() and not (folder / "artifacts.zip").exists()


def test_a_failed_run_exits_with_its_code_and_another_state_with_1(project, portal):
    serve_run(portal, ["failed"], {"outcome": "failed", "exit_code": 3, "message": "boom"})
    assert ci_cli.rebuild("hr", TARGET, None, "out", None, None, None) == 3
    assert (project / "out").is_dir()
    serve_run(portal, ["refused"], {"outcome": "refused", "exit_code": None})
    assert ci_cli.rebuild("hr", TARGET, None, "out", None, None, None) == 1


def test_a_cloud_app_has_no_rebuild_and_the_folder_still_exists(project, portal, capsys):
    (project / "hr" / "config.yml").write_text("schedule: daily\nplatform:\n  provider: aws\n")
    assert ci_cli.rebuild("hr", TARGET, None, None, None, None, None) == 1
    assert "takes only an app of the windows provider" in " ".join(capsys.readouterr().out.split())
    assert (project / "pdt-artifacts").is_dir()
    assert portal.seen == []


def test_register_prints_the_four_values_and_writes_no_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    seen = {}

    def approve(api_url, body):
        seen.update(body)
        return {"installation_id": "ins_ci", "customer_id": CUSTOMER}, "CODE"

    monkeypatch.setattr(ci_cli.register_cli, "approve_in_browser", approve)
    assert ci_cli.register("example", [TARGET], "https://api.example/") == 0
    assert (seen["kind"], seen["ci"]) == ("ci", {"project": "example", "targets": [TARGET]})
    out = capsys.readouterr().out
    for name in autoidm.SECRET_NAMES:
        assert f"{name}=" in out
    assert "PDT_API_URL=https://api.example\n" in out
    assert "pdt service author add example" in out
    assert not (tmp_path / "data").exists()
