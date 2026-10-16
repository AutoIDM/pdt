import os
import tempfile
from pathlib import Path

import pytest

import pdt
from conftest import add_app
from pdt import deploy_common
from pdt.deploy_common import (
    LIFECYCLE_MODULES, REBUILD_ENV, context_fingerprint, ensure_image, stage_build_context)


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


def test_a_meltano_link_into_the_uv_cache_is_left_out(project, tmp_path_factory):
    app = app_in(project)
    outside = tmp_path_factory.mktemp("cache") / "meltano"
    outside.write_text("binary")
    (app["dir"] / ".meltano" / "run").mkdir(parents=True)
    (app["dir"] / ".meltano" / "run" / "bin").symlink_to(outside)
    staged = stage_build_context(app) / "my-report"
    assert not (staged / ".meltano").exists()


def test_the_context_carries_this_pdts_lifecycle_outside_the_app_folder(project):
    app = app_in(project)
    vendored = stage_build_context(app) / ".pdt-lifecycle" / "pdt"
    for module in LIFECYCLE_MODULES:
        assert (vendored / module).read_bytes() == (Path(pdt.__file__).parent / module).read_bytes()


def fingerprint_of(app, image_platform="linux/amd64"):
    return context_fingerprint(stage_build_context(app), image_platform)


def test_the_fingerprint_is_stable_and_fits_a_cloud_run_label(project):
    app = app_in(project)
    first = fingerprint_of(app)
    assert fingerprint_of(app) == first
    assert len(first) == 32 and set(first) <= set("0123456789abcdef")


def test_the_fingerprint_ignores_mtimes_and_left_out_files(project):
    app = app_in(project)
    first = fingerprint_of(app)
    os.utime(app["dir"] / "run.py", (0, 0))
    (app["dir"] / ".env").write_text("TOKEN=secret\n")
    assert fingerprint_of(app) == first


@pytest.mark.parametrize("change", ["content", "new file", "pdt.yml", "platform", "version",
                                    "link"])
def test_the_fingerprint_changes_with_what_the_image_gets(project, monkeypatch, change):
    app = app_in(project)
    (app["dir"] / "entry.py").symlink_to("run.py")
    (app["dir"] / "other.py").write_text("X = 1\n")
    first = fingerprint_of(app)
    platform = "linux/amd64"
    if change == "content":
        (app["dir"] / "run.py").write_text("def main():\n    return 1\n")
    elif change == "new file":
        (app["dir"] / "helper.py").write_text("")
    elif change == "pdt.yml":
        (project / "pdt.yml").write_text((project / "pdt.yml").read_text() + "\n# note\n")
    elif change == "platform":
        platform = "linux/arm64"
    elif change == "version":
        monkeypatch.setattr(pdt, "__version__", "0.0.0-other")
    else:
        (app["dir"] / "entry.py").unlink()
        (app["dir"] / "entry.py").symlink_to("other.py")
    assert fingerprint_of(app, platform) != first


@pytest.fixture
def ensure(project, monkeypatch):
    """Run ensure_image on a real stage; return what it did."""
    monkeypatch.delenv(REBUILD_ENV, raising=False)
    app = app_in(project)
    stages = []
    monkeypatch.setattr(deploy_common, "stage_build_context",
                        lambda app: stages.append(stage_build_context(app)) or stages[-1])

    def run(deployed, current=True, fail=False):
        calls = {"current": 0, "build": 0}

        def is_current():
            calls["current"] += 1
            return current

        def build(stage):
            assert stage == stages[-1] and (stage / "Dockerfile").is_file()
            calls["build"] += 1
            if fail:
                raise SystemExit(1)

        fingerprint = ensure_image(app, "image", "linux/amd64", "", deployed, is_current, build)
        assert not stages[-1].exists()
        return fingerprint, calls
    run.stages = stages
    return run


def test_ensure_image_skips_an_unchanged_image_the_registry_holds(ensure, capsys):
    fingerprint, calls = ensure(None)
    assert calls == {"current": 0, "build": 1}
    capsys.readouterr()
    assert ensure(fingerprint) == (fingerprint, {"current": 1, "build": 0})
    assert "Image image is unchanged; skipped the build and push." in capsys.readouterr().out


def test_ensure_image_builds_again_when_the_registry_lost_the_image(ensure, capsys):
    fingerprint, _calls = ensure(None)
    assert ensure(fingerprint, current=False) == (fingerprint, {"current": 1, "build": 1})
    assert "the registry no longer holds the image the job runs" in capsys.readouterr().out


def test_ensure_image_builds_a_changed_image_without_asking_the_registry(ensure):
    fingerprint, calls = ensure("0" * 32)
    assert fingerprint != "0" * 32
    assert calls == {"current": 0, "build": 1}


def test_ensure_image_builds_when_rebuild_is_asked(ensure, monkeypatch):
    fingerprint, _calls = ensure(None)
    monkeypatch.setenv(REBUILD_ENV, "1")
    assert ensure(fingerprint) == (fingerprint, {"current": 0, "build": 1})


def test_a_failed_build_removes_the_stage_and_returns_nothing(ensure):
    with pytest.raises(SystemExit):
        ensure(None, fail=True)
    assert len(ensure.stages) == 1 and not ensure.stages[0].exists()
