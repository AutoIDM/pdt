import base64
import hashlib
import json
import sys

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from service_fixtures import CUSTOMER, INSTALLATION, FakePortal, copy_of

from pdt import cli, config, register_cli, sync_cli
from pdt.utils import autoidm


@pytest.fixture
def portal():
    found = FakePortal()
    yield found
    found.close()


@pytest.fixture
def project(tmp_path, monkeypatch, portal):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    for name in (*autoidm.SECRET_NAMES, "PDT_ENV_JSON", "PDT_PROJECT", config.LOCK_PROJECT_ENV):
        monkeypatch.delenv(name, raising=False)
    folder = autoidm.folder()
    folder.mkdir(parents=True)
    register_cli.write_key(folder / f"{CUSTOMER}.key", Ed25519PrivateKey.generate())
    (folder / f"{CUSTOMER}.json").write_text(json.dumps({
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "api_url": portal.url}))
    root = tmp_path / "example"
    (root / "hr").mkdir(parents=True)
    (root / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")
    (root / "hr" / "config.yml").write_text("schedule: daily\n")
    (root / "hr" / "run.py").write_text("print('hi')\n")
    (root / "hr" / ".env").write_text("TOKEN=secret\n")
    monkeypatch.chdir(root)
    return root


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.run()


def accept_uploads(portal):
    portal.routes["/v1/copies/start"] = lambda _body: (200, {"upload": {
        "url": f"{portal.url}/bucket", "headers": {}}})


def test_sync_up_sends_root_and_each_app_and_nothing_secret(project, portal, monkeypatch, capsys):
    accept_uploads(portal)
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    starts = portal.bodies("/v1/copies/start")
    assert [body["unit"] for body in starts] == ["root", "app=hr"]
    assert [item["path"] for item in starts[1]["files"]] == ["hr/config.yml", "hr/run.py"]
    assert [body["unit"] for body in portal.bodies("/v1/copies/finish")] == ["root", "app=hr"]
    assert all(b"secret" not in data for _path, data, _sum in portal.puts)
    for body, (_path, data, checksum) in zip(starts, portal.puts):
        assert body["sha256"] == hashlib.sha256(data).hexdigest()
        assert base64.b64decode(checksum).hex() == body["sha256"]
    assert "Copied app=hr of example" in capsys.readouterr().out


def test_sync_up_of_one_app_sends_only_that_unit(project, portal, monkeypatch):
    accept_uploads(portal)
    assert run(monkeypatch, "sync", "up", "hr", "--yes") == 0
    assert [body["unit"] for body in portal.bodies("/v1/copies/start")] == ["app=hr"]


def test_a_zip_that_autoidm_holds_is_not_sent_again(project, portal, monkeypatch):
    portal.routes["/v1/copies/start"] = lambda _body: (200, {"upload": None})
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    assert portal.puts == []
    assert len(portal.bodies("/v1/copies/finish")) == 2


def test_sync_up_of_an_unknown_app_says_so(project, portal, monkeypatch, capsys):
    assert run(monkeypatch, "sync", "up", "nope", "--yes") == 1
    assert "no app folder nope" in capsys.readouterr().out


def test_sync_down_backs_up_replaces_and_merges_pdt_yml(project, portal, monkeypatch, capsys):
    (project / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n"
                                     "platform:\n  provider: windows\n  region: local\n")
    portal.serve_copies(
        copy_of("root", {"pdt.yml": b"platform:\n  provider: windows\n  timezone: UTC\n",
                         "README.md": b"new\n"}),
        copy_of("app=hr", {"hr/config.yml": b"schedule: hourly\n", "hr/run.py": b"print('new')\n"}))
    assert run(monkeypatch, "sync", "down", "--yes") == 0
    merged = yaml.safe_load((project / "pdt.yml").read_text())
    assert merged["managed_services"] == {"customer": CUSTOMER}
    assert merged["platform"] == {"provider": "windows", "region": "local", "timezone": "UTC"}
    assert (project / "README.md").read_text() == "new\n"
    assert (project / "hr" / "run.py").read_text() == "print('new')\n"
    assert (project / "hr" / ".env").read_text() == "TOKEN=secret\n"
    backups = list((project / ".pdt" / "backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "hr" / "run.py").read_text() == "print('hi')\n"
    out = capsys.readouterr().out
    assert "change: hr/run.py" in out and "add: README.md" in out


def test_sync_down_of_one_app_replaces_the_app_folder(project, portal, monkeypatch):
    (project / "hr" / "old.py").write_text("x = 1\n")
    portal.serve_copies(copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('hi')\n"}))
    assert run(monkeypatch, "sync", "down", "hr", "--yes") == 0
    assert not (project / "hr" / "old.py").exists()
    assert (project / "hr" / ".env").is_file()
    assert portal.bodies("/v1/copies/get")[0]["unit"] == "app=hr"
    backup = next((project / ".pdt" / "backup").iterdir())
    assert (backup / "hr" / "old.py").read_text() == "x = 1\n"


def test_sync_down_of_an_app_copy_with_no_config_yml_keeps_the_local_one(project, portal,
                                                                        monkeypatch):
    portal.serve_copies(copy_of("app=hr", {"hr/run.py": b"print('new')\n"}))
    assert run(monkeypatch, "sync", "down", "hr", "--yes") == 0
    assert (project / "hr" / "config.yml").read_text() == "schedule: daily\n"
    assert (project / "hr" / "run.py").read_text() == "print('new')\n"


def test_two_sync_downs_keep_two_backups(project, portal, monkeypatch):
    portal.serve_copies(copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('second')\n"}))
    (project / "hr" / "run.py").write_text("print('first')\n")
    assert run(monkeypatch, "sync", "down", "hr", "--yes") == 0
    (project / "hr" / "run.py").write_text("print('third')\n")
    assert run(monkeypatch, "sync", "down", "hr", "--yes") == 0
    backups = sorted((project / ".pdt" / "backup").iterdir())
    assert [(backup / "hr" / "run.py").read_text() for backup in backups] == [
        "print('first')\n", "print('third')\n"]


@pytest.mark.skipif(sys.platform == "win32", reason="a file link needs an administrator on Windows")
def test_sync_down_never_writes_through_a_link_at_the_temporary_name(project, portal, monkeypatch,
                                                                     tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (project / "hr" / "run.py.pdt-sync").symlink_to(outside)
    portal.serve_copies(copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('new')\n"}))
    assert run(monkeypatch, "sync", "down", "hr", "--yes") == 0
    assert outside.read_text() == "keep\n"
    assert (project / "hr" / "run.py").read_text() == "print('new')\n"


def test_sync_down_at_a_terminal_asks_once_and_a_no_changes_nothing(project, portal, monkeypatch):
    portal.serve_copies(copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('new')\n"}))
    monkeypatch.setattr(sync_cli, "can_prompt", lambda _interactive: True)
    asked = []
    monkeypatch.setattr(sync_cli.console, "confirm", lambda question: asked.append(question))
    assert run(monkeypatch, "sync", "down") != 0
    assert asked == ["Write these changes?"]
    assert (project / "hr" / "run.py").read_text() == "print('hi')\n"


def test_sync_down_of_a_copy_of_another_customer_changes_nothing(project, portal, monkeypatch,
                                                                 capsys):
    portal.serve_copies(copy_of("root", {"pdt.yml": b"managed_services:\n  customer: cus_other\n"}))
    assert run(monkeypatch, "sync", "down", "--yes") == 1
    assert "cus_other" in capsys.readouterr().out
    assert CUSTOMER in (project / "pdt.yml").read_text()


def test_sync_down_stops_in_a_git_clone(project, portal, monkeypatch, capsys):
    (project / ".git").mkdir()
    assert run(monkeypatch, "sync", "down", "--yes") == 1
    assert "Git clone" in capsys.readouterr().out
    assert portal.bodies("/v1/copies/get") == []


def test_sync_down_with_no_copy_says_so(project, portal, monkeypatch, capsys):
    portal.serve_copies()
    assert run(monkeypatch, "sync", "down", "--yes") == 1
    assert "holds no copy" in capsys.readouterr().out


def test_a_service_sync_down_makes_a_new_project(tmp_path, portal, monkeypatch):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    for name in (*autoidm.SECRET_NAMES, "PDT_PROJECT", config.LOCK_PROJECT_ENV):
        monkeypatch.delenv(name, raising=False)
    folder = autoidm.folder()
    folder.mkdir(parents=True)
    register_cli.write_key(folder / f"{CUSTOMER}.key", Ed25519PrivateKey.generate())
    (folder / f"{CUSTOMER}.json").write_text(json.dumps({
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "api_url": portal.url}))
    target = tmp_path / "projects" / "fresh"
    target.mkdir(parents=True)
    monkeypatch.chdir(target)
    monkeypatch.setenv("PDT_SERVICE_PROJECT", "fresh")
    monkeypatch.setenv("PDT_SERVICE_CUSTOMER", CUSTOMER)
    portal.serve_copies(copy_of("root", {"pdt.yml": b"platform:\n  provider: windows\n"}),
                        copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('hi')\n"}))
    assert run(monkeypatch, "sync", "down", "--yes") == 0
    assert (target / "hr" / "run.py").is_file()
    assert CUSTOMER in (target / "pdt.yml").read_text()


def test_the_merge_keeps_a_local_key_and_takes_each_key_of_the_copy():
    local = b"platform:\n  provider: aws\n  profile: mine\nalerts: []\n"
    copy = b"platform:\n  provider: aws\n  region: us-east-2\n"
    merged = yaml.safe_load(sync_cli.merged_text(local, copy, "pdt.yml"))
    assert merged == {"platform": {"provider": "aws", "profile": "mine", "region": "us-east-2"},
                      "alerts": []}
    assert sync_cli.merged_text(copy, copy + b"# kept\n", "pdt.yml").endswith(b"# kept\n")
