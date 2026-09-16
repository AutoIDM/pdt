from pdt import gcloud_sdk


def test_an_unattended_run_downloads_gcloud_without_a_question(monkeypatch, tmp_path):
    monkeypatch.setenv("CI", "true")
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
