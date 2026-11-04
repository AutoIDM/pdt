import hashlib
import io
import zipfile

import pytest

from pdt import copies


def zip_of(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


def entry_of(data: bytes, files: dict[str, bytes], unit="root") -> dict:
    return {"unit": unit, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "files": [{"path": name, "size": len(content)} for name, content in files.items()]}


def project(tmp_path):
    (tmp_path / "hr").mkdir()
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    (tmp_path / "hr" / "config.yml").write_text("schedule: daily\n")
    (tmp_path / "hr" / "run.py").write_text("print('hi')\n")
    (tmp_path / "hr" / ".env").write_text("TOKEN=secret\n")
    (tmp_path / "notes.txt").write_text("notes\n")
    (tmp_path / "data.csv").write_text("a,b\n")
    return tmp_path


def test_a_project_splits_into_root_and_one_unit_for_each_app(tmp_path):
    root = project(tmp_path)
    files, left_out = copies.collect(root)
    units = copies.split_units(files, copies.app_names(root))
    assert [entry["path"] for entry in units["root"]] == ["notes.txt", "pdt.yml"]
    assert [entry["path"] for entry in units["app=hr"]] == ["hr/config.yml", "hr/run.py"]
    assert "hr/.env" in left_out and "data.csv" in left_out


def test_the_same_files_give_the_same_zip(tmp_path):
    root = project(tmp_path)
    files = copies.split_units(copies.collect(root)[0], ["hr"])["app=hr"]
    assert copies.build_zip(root, files) == copies.build_zip(root, files)


def test_unpack_writes_the_files_of_a_unit(tmp_path):
    files = {"pdt.yml": b"apps: []\n", "lib/util.py": b"x = 1\n"}
    data = zip_of(files)
    copies.unpack(data, entry_of(data, files), tmp_path / "out")
    assert (tmp_path / "out" / "lib" / "util.py").read_bytes() == b"x = 1\n"


def test_a_zip_with_another_sha256_writes_nothing(tmp_path):
    files = {"pdt.yml": b"apps: []\n"}
    data = zip_of(files)
    entry = {**entry_of(data, files), "sha256": "0" * 64}
    with pytest.raises(copies.CopyError, match="SHA-256"):
        copies.unpack(data, entry, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a\\b", "c:x", ".pdt/state",
                                  "con.txt", ".git/config", "folder/.env"])
def test_a_hostile_path_writes_nothing(tmp_path, path):
    files = {path: b"x"}
    data = zip_of(files)
    with pytest.raises(copies.CopyError):
        copies.unpack(data, entry_of(data, files), tmp_path / "out")
    assert not any(tmp_path.rglob("x*"))


def test_a_zip_with_a_file_the_list_does_not_name_writes_nothing(tmp_path):
    data = zip_of({"pdt.yml": b"a", "extra.py": b"b"})
    entry = entry_of(data, {"pdt.yml": b"a"})
    with pytest.raises(copies.CopyError, match="exactly"):
        copies.unpack(data, entry, tmp_path / "out")


def test_a_file_of_another_size_writes_nothing(tmp_path):
    data = zip_of({"pdt.yml": b"abc"})
    entry = entry_of(data, {"pdt.yml": b"ab"})
    with pytest.raises(copies.CopyError, match="size"):
        copies.unpack(data, entry, tmp_path / "out")


def test_a_copy_that_names_another_customer_is_refused():
    assert "cus_other" in copies.customer_problem(
        b"managed_services:\n  customer: cus_other\n", "cus_mine")
    assert copies.customer_problem(b"platform: {}\n", "cus_mine") == ""
    assert copies.customer_problem(None, "cus_mine") == ""


def test_a_second_unit_goes_into_the_same_project_folder_and_overwrites_nothing(tmp_path):
    root_files = {"pdt.yml": b"apps: []\n"}
    app_files = {"hr/run.py": b"print(1)\n"}
    for files in (root_files, app_files):
        data = zip_of(files)
        copies.unpack(data, entry_of(data, files), tmp_path / "out")
    assert (tmp_path / "out" / "hr" / "run.py").is_file()
    data = zip_of(root_files)
    with pytest.raises(copies.CopyError, match="exists"):
        copies.unpack(data, entry_of(data, root_files), tmp_path / "out")


def test_an_app_unit_with_a_path_outside_its_folder_writes_nothing(tmp_path):
    files = {"pdt.yml": b"x"}
    data = zip_of(files)
    with pytest.raises(copies.CopyError, match="outside its app folder"):
        copies.unpack(data, entry_of(data, files, "app=hr"), tmp_path / "out")
