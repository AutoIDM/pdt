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
def test_a_missing_gcloud_downloads_without_a_question(monkeypatch, tmp_path, ci):
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
