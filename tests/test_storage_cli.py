import json

import pytest

from pdt import storage_cli
from pdt.utils.storage import Store


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


def test_ls_json_lists_each_entry_with_its_type(store, app, capsys):
    assert storage_cli.run(store, app, ["ls", "--json"], False) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [(entry["name"], entry["type"]) for entry in entries] == [("a.csv", "file")]


def test_ls_recursive_lists_every_file_under_the_folder(store, app, capsys, tmp_path):
    nested = tmp_path / "store" / "my-report" / "runs" / "r1" / "more"
    nested.mkdir(parents=True)
    (nested / "b.txt").write_text("b")
    (nested.parent / "a.csv").write_text("a")
    assert storage_cli.run(store, app, ["ls", "runs/", "--recursive", "--json"], False) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in entries] == ["runs/r1/a.csv", "runs/r1/more/b.txt"]
    assert all(entry["type"] == "file" for entry in entries)


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


def test_unlock_with_no_lock_says_so(store, app, capsys):
    assert storage_cli.run(store, app, ["unlock"], False) == 0
    assert "nothing to unlock" in capsys.readouterr().out


def test_unlock_asks_then_releases_the_lock(store, app, tmp_path, capsys, monkeypatch):
    store.pull("state/", tmp_path / "local")
    monkeypatch.setattr("pdt.deploy.confirm", lambda actions, assume_yes: True)
    assert storage_cli.run(store, app, ["unlock"], False) == 0
    out = capsys.readouterr().out
    assert "released the state lock of my-report" in out
    assert store.read_lock() is None


def test_unlock_refused_keeps_the_lock(store, app, tmp_path, capsys, monkeypatch):
    store.pull("state/", tmp_path / "local")
    seen = []
    monkeypatch.setattr("pdt.deploy.confirm", lambda actions, assume_yes: seen.append(actions) or False)
    assert storage_cli.run(store, app, ["unlock"], False) == 1
    assert store.read_lock() is not None
    assert any("release the state lock held by run" in line for line in seen[0])
