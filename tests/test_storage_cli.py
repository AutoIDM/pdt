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
    assert "remove storage: false from my-report/pdt.yml" in out
