import hashlib
import io
import tarfile
import zipfile

import pytest

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
