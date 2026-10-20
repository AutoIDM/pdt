import json

import pytest

from pdt import console, storage_cli
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


@pytest.mark.parametrize("subcommand", [["get", "a.csv"], ["query", "select 1"], ["unlock"]])
def test_json_works_only_with_ls(store, app, capsys, subcommand):
    assert storage_cli.run(store, app, [*subcommand, "--json"], False) == 1
    captured = capsys.readouterr().out
    assert "--json works only with ls" in captured


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


@pytest.mark.parametrize("subcommand", ["unlock", "destroy"])
def test_yes_after_the_separator_skips_the_question(store, app, tmp_path, monkeypatch, subcommand):
    store.pull("state/", tmp_path / "local")
    seen = []
    monkeypatch.setattr("pdt.deploy.confirm", lambda actions, assume_yes: seen.append(assume_yes))
    storage_cli.run(store, app, [subcommand, "--yes"], False)
    assert seen == [True]


def test_ls_counts_the_files_and_bytes_under_each_folder(store, app, capsys, tmp_path):
    runs = tmp_path / "store" / "my-report" / "runs"
    (runs / "r1" / "more").mkdir(parents=True)
    (runs / "r1" / "a.csv").write_text("abc")
    (runs / "r1" / "more" / "b.txt").write_text("de")
    (runs / "r2").mkdir()
    (runs / "r2" / "c.txt").write_text("f")
    (runs / "empty").mkdir()
    assert storage_cli.run(store, app, ["ls", "--json"], False) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [(entry["name"], entry["files"], entry["size"]) for entry in entries] == [
        ("a.csv", 1, 1), ("runs", 3, 6)]
    assert storage_cli.run(store, app, ["ls", "runs/", "--json"], False) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [(entry["name"], entry["files"], entry["size"]) for entry in entries] == [
        ("runs/empty", 0, 0), ("runs/r1", 2, 5), ("runs/r2", 1, 1)]
    assert storage_cli.run(store, app, ["ls", "runs"], False) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Path", "Files", "Size"]
    assert lines[2].split() == ["runs/r1", "2", "5", "B"]


def test_size_prints_bytes_until_kb_and_so_on_up_to_tb():
    assert [console.size(count) for count in (0, 5, 2048, 1536 * 1024, 3 * 1024 ** 3,
                                              2 * 1024 ** 4, 5000 * 1024 ** 4)] == [
        "0 B", "5 B", "2.0 KB", "1.5 MB", "3.0 GB", "2.0 TB", "5000.0 TB"]


def test_open_with_no_subcommand_opens_the_storage_page(store, app, monkeypatch):
    opened = []
    monkeypatch.setattr(storage_cli.runs_cli, "open_place",
                        lambda target, note="": opened.append(target) or 0)
    assert storage_cli.run(store, app, ["--open"], False, "https://console.example/bucket") == 0
    assert opened == ["https://console.example/bucket"]


def test_get_open_opens_the_saved_file(store, app, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(storage_cli.runs_cli, "open_place",
                        lambda target, note="": opened.append(target) or 0)
    assert storage_cli.run(store, app, ["get", "a.csv", str(tmp_path / "a.csv"), "--open"],
                           False) == 0
    assert opened == [str(tmp_path / "a.csv")]


def test_open_with_another_subcommand_is_an_error(store, app, capsys):
    assert storage_cli.run(store, app, ["ls", "--open"], False) == 1
    assert "--open works only with get" in capsys.readouterr().out
