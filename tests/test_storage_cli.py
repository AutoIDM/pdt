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


def test_destroy_removes_each_object_separately(app):
    class FileSystem:
        def __init__(self):
            self.removed = []

        def find(self, path):
            return ["one.csv", "two.csv"]

        def rm(self, path):
            self.removed.append(path)

    fs = FileSystem()
    store = type("Store", (), {"fs": lambda self: fs})()

    assert storage_cli.run(store, app, ["destroy"], True) == 0
    assert fs.removed == ["one.csv", "two.csv"]


def test_destroy_accepts_an_absent_store(app, capsys):
    class FileSystem:
        def find(self, path):
            raise FileNotFoundError(path)

    store = type("Store", (), {"fs": lambda self: FileSystem()})()

    assert storage_cli.run(store, app, ["destroy"], True) == 0
    assert "no objects under my-report/" in capsys.readouterr().out
