import contextlib
from pathlib import Path

import pytest

from pdt import gcloud_sdk

UPDATE_CHECK = "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK"
SURVEY = "CLOUDSDK_SURVEY_DISABLE_PROMPTS"


@pytest.fixture(autouse=True)
def restore_quiet_env(monkeypatch):
    # delenv records nothing for an unset name, so setenv first: then
    # monkeypatch removes what ensure_gcloud sets when the test ends.
    for name in (UPDATE_CHECK, SURVEY):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("ci", ["true", ""])
def test_a_missing_gcloud_downloads_without_a_question(monkeypatch, tmp_path, capsys, ci):
    monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: None)
    monkeypatch.setattr(gcloud_sdk, "LOCAL_GCLOUD", tmp_path / "gcloud")

    def refuse(prompt=""):
        raise AssertionError(f"pdt asked a question: {prompt}")

    monkeypatch.setattr("builtins.input", refuse)
    downloads = []

    def fake_download(key):
        downloads.append(key)
        (tmp_path / "gcloud").write_text("")

    monkeypatch.setattr(gcloud_sdk, "download_sdk", fake_download)
    assert gcloud_sdk.ensure_gcloud() == str(tmp_path / "gcloud")
    assert downloads == [gcloud_sdk.sdk_platform()]
    assert "Installing the Google Cloud CLI" in capsys.readouterr().out


def test_ensure_gcloud_turns_off_the_update_nag_and_survey(monkeypatch):
    monkeypatch.delenv(UPDATE_CHECK, raising=False)
    monkeypatch.delenv(SURVEY, raising=False)
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: "/usr/bin/gcloud")
    assert gcloud_sdk.ensure_gcloud() == "/usr/bin/gcloud"
    assert gcloud_sdk.os.environ[UPDATE_CHECK] == "true"
    assert gcloud_sdk.os.environ[SURVEY] == "true"


def test_ensure_gcloud_keeps_a_value_the_user_set(monkeypatch):
    monkeypatch.setenv(UPDATE_CHECK, "false")
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: "/usr/bin/gcloud")
    gcloud_sdk.ensure_gcloud()
    assert gcloud_sdk.os.environ[UPDATE_CHECK] == "false"


def local_copy(monkeypatch, tmp_path, version):
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: None)
    sdk = tmp_path / "gcloud" / "google-cloud-sdk"
    monkeypatch.setattr(gcloud_sdk, "SDK_DIR", tmp_path / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "LOCAL_GCLOUD", sdk / "bin" / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "SDK_VERSION", sdk / "VERSION")
    (sdk / "bin").mkdir(parents=True)
    (sdk / "bin" / "gcloud").write_text("")
    (sdk / "VERSION").write_text(version + "\n")


def test_a_local_gcloud_of_another_version_is_replaced(monkeypatch, tmp_path, capsys):
    local_copy(monkeypatch, tmp_path, "581.0.0")
    downloads = []
    monkeypatch.setattr(gcloud_sdk, "download_sdk", downloads.append)
    assert gcloud_sdk.ensure_gcloud() == str(gcloud_sdk.LOCAL_GCLOUD)
    assert downloads == [gcloud_sdk.sdk_platform()]
    assert f"Updating the Google Cloud CLI 581.0.0 to {gcloud_sdk.VERSION}" in capsys.readouterr().out


def test_a_local_gcloud_of_the_pinned_version_is_kept(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, gcloud_sdk.VERSION)
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("downloaded"))
    assert gcloud_sdk.ensure_gcloud() == str(gcloud_sdk.LOCAL_GCLOUD)


def test_gcloud_on_the_path_wins_over_a_local_gcloud_of_another_version(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "581.0.0")
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("downloaded"))
    assert gcloud_sdk.ensure_gcloud() == "/usr/bin/gcloud"
    assert gcloud_sdk.SDK_VERSION.read_text() == "581.0.0\n"


def test_the_download_replaces_the_local_gcloud_only_after_it_unpacks(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "581.0.0")

    def refuse(url, sha256, stage, error):
        raise error("checksum mismatch")

    monkeypatch.setattr(gcloud_sdk, "fetch_verified", refuse)
    with pytest.raises(gcloud_sdk.GcloudError, match="checksum mismatch"):
        gcloud_sdk.download_sdk("darwin-arm")
    assert gcloud_sdk.SDK_VERSION.read_text() == "581.0.0\n"

    (tmp_path / "gcloud.new").mkdir()
    (tmp_path / "gcloud.new" / "leftover").write_text("")
    monkeypatch.setattr(gcloud_sdk, "fetch_verified", unpack)
    gcloud_sdk.download_sdk("darwin-arm")
    assert gcloud_sdk.SDK_VERSION.read_text() == gcloud_sdk.VERSION + "\n"
    assert gcloud_sdk.LOCAL_GCLOUD.read_text() == gcloud_sdk.VERSION
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gcloud"]


def unpack(url, sha256, stage, error):
    (stage / "google-cloud-sdk" / "bin").mkdir(parents=True)
    (stage / "google-cloud-sdk" / "bin" / "gcloud").write_text(gcloud_sdk.VERSION)
    (stage / "google-cloud-sdk" / "VERSION").write_text(gcloud_sdk.VERSION + "\n")


def refuse_renames(monkeypatch, *names):
    rename = Path.rename

    def fake(self, target):
        if self.name in names:
            raise PermissionError(13, "Access is denied")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fake)


def test_a_failed_install_puts_the_current_gcloud_back(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "581.0.0")
    monkeypatch.setattr(gcloud_sdk, "fetch_verified", unpack)
    refuse_renames(monkeypatch, "gcloud.new")
    with pytest.raises(gcloud_sdk.GcloudError, match="cannot install to .*: Access is denied$"):
        gcloud_sdk.download_sdk("darwin-arm")
    assert gcloud_sdk.SDK_VERSION.read_text() == "581.0.0\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gcloud"]


def test_a_failed_install_and_restore_names_the_current_gcloud(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "581.0.0")
    monkeypatch.setattr(gcloud_sdk, "fetch_verified", unpack)
    refuse_renames(monkeypatch, "gcloud.new", "gcloud.previous")
    with pytest.raises(gcloud_sdk.GcloudError, match="the current copy is in .*gcloud.previous"):
        gcloud_sdk.download_sdk("darwin-arm")
    assert (tmp_path / "gcloud.previous" / "google-cloud-sdk" / "VERSION").read_text() == "581.0.0\n"


def test_an_interrupted_update_restores_the_current_gcloud(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, gcloud_sdk.VERSION)
    (tmp_path / "gcloud").rename(tmp_path / "gcloud.previous")
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("downloaded"))
    assert gcloud_sdk.ensure_gcloud() == str(gcloud_sdk.LOCAL_GCLOUD)
    assert gcloud_sdk.SDK_VERSION.read_text() == gcloud_sdk.VERSION + "\n"
    assert not (tmp_path / "gcloud.previous").exists()


def test_an_update_that_another_process_finished_downloads_nothing(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "581.0.0")

    @contextlib.contextmanager
    def other_process_updated(path):
        gcloud_sdk.SDK_VERSION.write_text(gcloud_sdk.VERSION + "\n")
        yield

    monkeypatch.setattr(gcloud_sdk, "locked", other_process_updated)
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("downloaded"))
    assert gcloud_sdk.ensure_gcloud() == str(gcloud_sdk.LOCAL_GCLOUD)


def test_an_install_moved_away_during_the_first_read_is_checked_again_under_the_lock(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, gcloud_sdk.VERSION)
    read_text = Path.read_text
    moves = []

    def moved_by_another_process(self, *args, **kwargs):
        if self.name == "VERSION" and not moves:
            moves.append(self)
            (tmp_path / "gcloud").rename(tmp_path / "gcloud.previous")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", moved_by_another_process)
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("downloaded"))
    assert gcloud_sdk.ensure_gcloud() == str(gcloud_sdk.LOCAL_GCLOUD)
    assert moves and gcloud_sdk.SDK_VERSION.is_file()
