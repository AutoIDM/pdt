import hashlib
import io
import tarfile
import zipfile

import pytest

from conftest import plain
from pdt import download
from pdt.download import fetch_verified


class FetchError(Exception):
    pass


def archive(tmp_path, name):
    path = tmp_path / name
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("tool/bin/tool", "#!/bin/sh\n")
    else:
        with tarfile.open(path, "w:gz") as tar:
            data = b"#!/bin/sh\n"
            info = tarfile.TarInfo("tool/bin/tool")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["tool-1.0.tar.gz", "tool-1.0.zip"])
def test_a_verified_archive_is_unpacked_into_the_stage(tmp_path, name):
    path, digest = archive(tmp_path, name)
    stage = tmp_path / "stage"
    stage.mkdir()
    assert fetch_verified(path.as_uri(), digest, stage, FetchError) == stage
    assert (stage / "tool" / "bin" / "tool").read_text() == "#!/bin/sh\n"


def test_a_checksum_mismatch_raises_the_callers_error_and_unpacks_nothing(tmp_path):
    path, digest = archive(tmp_path, "tool-1.0.tar.gz")
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(FetchError, match=f"checksum mismatch for {path.as_uri()}\n  expected 00\n  got      {digest}\n"):
        fetch_verified(path.as_uri(), "00", stage, FetchError)
    assert list(stage.iterdir()) == []


def test_a_failed_download_raises_the_callers_error(tmp_path):
    with pytest.raises(FetchError, match="download failed: "):
        fetch_verified((tmp_path / "missing.tar.gz").as_uri(), "00", tmp_path, FetchError)


def test_a_download_without_a_terminal_has_no_per_mb_progress(tmp_path, monkeypatch):
    path, digest = archive(tmp_path, "tool-1.0.tar.gz")
    stage = tmp_path / "stage"
    stage.mkdir()
    status = []
    monkeypatch.setattr(download.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(download.console, "status", status.append)
    monkeypatch.setattr(download.console, "progress", lambda text: pytest.fail(text))
    monkeypatch.setattr(download.console, "say", lambda: pytest.fail("pdt ended a progress line"))

    fetch_verified(path.as_uri(), digest, stage, FetchError)

    assert plain(status) == [
        f"downloading {path.as_uri()}",
        f"downloaded {path.name}",
        f"unpacking {path.name}",
    ]


def test_a_download_with_a_terminal_keeps_its_progress_display(tmp_path, monkeypatch):
    path, digest = archive(tmp_path, "tool-1.0.tar.gz")
    stage = tmp_path / "stage"
    stage.mkdir()
    progress = []
    monkeypatch.setattr(download.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(download.console, "progress", progress.append)
    monkeypatch.setattr(download.console, "say", lambda: progress.append("ended"))

    fetch_verified(path.as_uri(), digest, stage, FetchError)

    assert progress == ["  0 / 0 MB", "ended"]
