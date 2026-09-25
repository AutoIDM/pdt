import tempfile

import pytest

from conftest import add_app
from pdt.deploy_common import stage_build_context


@pytest.fixture(autouse=True)
def stage_in_tmp(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("stage")))


def app_in(project):
    folder = add_app(project, "my-report")
    return {"name": "my-report", "dir": folder}


def test_credential_files_stay_out_of_the_build_context(project):
    app = app_in(project)
    for name in ("credentials.json", "id_rsa", "server.pem"):
        (app["dir"] / name).write_text("secret")
    (app["dir"] / ".ssh").mkdir()
    (app["dir"] / ".ssh" / "config").write_text("secret")
    staged = stage_build_context(app) / "my-report"
    assert sorted(p.name for p in staged.iterdir()) == ["run.py"]


def test_a_link_outside_the_app_folder_stops_the_build(project, tmp_path_factory, capsys):
    app = app_in(project)
    outside = tmp_path_factory.mktemp("home") / "aws-credentials"
    outside.write_text("secret")
    (app["dir"] / "aws").symlink_to(outside)
    with pytest.raises(SystemExit):
        stage_build_context(app)
    assert "aws is a link to" in capsys.readouterr().out


def test_a_link_inside_the_app_folder_stays_a_link(project):
    app = app_in(project)
    (app["dir"] / "entry.py").symlink_to("run.py")
    staged = stage_build_context(app) / "my-report"
    assert (staged / "entry.py").is_symlink()
    assert (staged / "entry.py").read_text() == (app["dir"] / "run.py").read_text()


def test_the_note_names_what_was_left_out(project, capsys):
    app = app_in(project)
    (app["dir"] / "credentials.json").write_text("secret")
    (app["dir"] / ".ssh").mkdir()
    stage_build_context(app)
    assert "left out of the image: .ssh, credentials.json" in capsys.readouterr().out
