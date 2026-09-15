import subprocess
from pathlib import Path

import pytest

from pdt import storage_cli
from pdt.utils.storage import Local, Store


@pytest.fixture
def store(tmp_path):
    folder = tmp_path / "store" / "my-report"
    folder.mkdir(parents=True)
    (folder / "a.csv").write_text("a")
    return Store(folder.as_uri() + "/")


@pytest.fixture
def app():
    return {"name": "my-report", "storage": True}


def test_ls_lists_the_folder(store, app, capsys):
    assert storage_cli.run(store, app, ["ls"], False) == 0
    assert "a.csv" in capsys.readouterr().out


def test_no_subcommand_prints_the_usage(store, app, capsys):
    assert storage_cli.run(store, app, [], False) == 1
    assert storage_cli.USAGE in capsys.readouterr().out


def test_get_without_a_path_prints_the_usage(store, app, capsys):
    assert storage_cli.run(store, app, ["get"], False) == 1
    assert storage_cli.USAGE in capsys.readouterr().out


def test_storage_turned_off_is_refused(store, capsys):
    app = {"name": "my-report", "storage": False}
    assert storage_cli.run(store, app, ["ls"], False) == 1
    out = capsys.readouterr().out
    assert "storage is turned off for my-report" in out
    assert "remove storage: false from my-report/config.yml" in out


def test_mount_of_a_local_store_names_the_folder(store, app, capsys):
    assert storage_cli.run(store, app, ["mount"], False) == 0
    assert "already a folder on this computer" in capsys.readouterr().out


@pytest.fixture
def cloud(store, monkeypatch, tmp_path):
    from pdt import rclone
    monkeypatch.setattr(rclone, "ensure_rclone", lambda assume_yes: "/opt/rclone")
    monkeypatch.setattr(rclone, "ensure_winfsp", lambda assume_yes: None)
    monkeypatch.setattr(storage_cli, "find_mount_folder", lambda app: tmp_path / "mnt")
    monkeypatch.setattr(storage_cli, "Local", type("NotLocal", (), {}))
    monkeypatch.setattr(Local, "rclone_remote",
                        lambda self: (":gcs,env_auth=true:pdt-data-abc/my-report/", {"TOKEN": "t"}))
    calls = []

    class Proc:
        returncode = 0

        def __init__(self, command, env):
            calls.append((command, env))

        def wait(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", Proc)
    return calls


def test_mount_runs_rclone_with_the_remote_and_the_sign_in(store, app, cloud, capsys):
    assert storage_cli.run(store, app, ["mount"], False) == 0
    (command, env), = cloud
    assert command[0] == "/opt/rclone"
    assert command[1] in ("mount", "nfsmount")
    assert command[2] == ":gcs,env_auth=true:pdt-data-abc/my-report/"
    assert command[3].endswith("mnt")
    assert "--read-only" not in command
    assert env["TOKEN"] == "t"
    assert "unmounted" in capsys.readouterr().out


def test_mount_takes_the_state_lock_and_releases_it(store, app, cloud, monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode = 0

        def __init__(self, command, env):
            seen["lock"] = store.held_lock()

        def wait(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", Proc)
    assert storage_cli.run(store, app, ["mount", str(tmp_path / "here")], False) == 0
    assert seen["lock"]["run"].startswith("mount on ")
    assert store.held_lock() is None


def test_mount_falls_back_to_read_only_while_a_run_holds_the_state(store, app, cloud, capsys):
    held = store.take_lock(owner="run1")
    assert storage_cli.run(store, app, ["mount"], False) == 0
    (command, env), = cloud
    assert "--read-only" in command
    assert "mounting read-only" in capsys.readouterr().out
    assert store.held_lock() == held


def test_read_only_mount_takes_no_lock(store, app, cloud):
    assert storage_cli.run(store, app, ["mount", "--read-only"], False) == 0
    (command, env), = cloud
    assert "--read-only" in command
    assert store.held_lock() is None
