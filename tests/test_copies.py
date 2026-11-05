import hashlib
import io
import os
import subprocess
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


def test_a_clone_copy_leaves_out_tracked_secret_files_and_keeps_tracked_data(tmp_path):
    root = project(tmp_path)
    (root / "hr" / ".env.aws").write_text("KEY=secret\n")
    (root / "hr" / "token.key").write_text("secret\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "-f", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)

    files, left_out = copies.collect(root)

    assert [entry["path"] for entry in files] == ["data.csv", "hr/config.yml", "hr/run.py",
                                                  "notes.txt", "pdt.yml"]
    assert {"hr/.env", "hr/.env.aws", "hr/token.key"} <= set(left_out)


@pytest.mark.skipif(os.name == "nt", reason="a folder link needs an administrator on Windows")
def test_a_clone_copy_leaves_out_a_tracked_file_under_a_linked_folder(tmp_path):
    (tmp_path / "project").mkdir()
    root = project(tmp_path / "project")
    (root / "shared").mkdir()
    (root / "shared" / "settings.json").write_text("{}\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.json").write_text("secret\n")
    (root / "shared" / "settings.json").unlink()
    (root / "shared").rmdir()
    (root / "shared").symlink_to(outside, target_is_directory=True)

    files, left_out = copies.collect(root)

    assert "shared/settings.json" not in [entry["path"] for entry in files]
    assert left_out["shared/settings.json"] == "a link"


@pytest.mark.skipif(os.name == "nt", reason="a folder link needs an administrator on Windows")
def test_a_clone_copy_leaves_out_a_tracked_file_under_a_folder_linked_inside_the_project(
        tmp_path):
    root = project(tmp_path)
    (root / "shared").mkdir()
    (root / "shared" / "settings.json").write_text("{}\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    (root / ".secrets").mkdir()
    (root / ".secrets" / "settings.json").write_text("secret\n")
    (root / "shared" / "settings.json").unlink()
    (root / "shared").rmdir()
    (root / "shared").symlink_to(root / ".secrets", target_is_directory=True)

    files, left_out = copies.collect(root)

    assert "shared/settings.json" not in [entry["path"] for entry in files]
    assert left_out["shared/settings.json"] == "a link"


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


def git_archive_zip(tmp_path) -> bytes:
    repo = tmp_path / "repo"
    (repo / "hr").mkdir(parents=True)
    (repo / "pdt.yml").write_text("apps: []\n")
    (repo / "hr" / "run.py").write_text("print(1)\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    return subprocess.run(["git", "-C", str(repo), "archive", "--format=zip", "HEAD"],
                          check=True, capture_output=True).stdout


def test_a_git_archive_zip_with_folder_entries_unpacks(tmp_path):
    data = git_archive_zip(tmp_path)
    assert "hr/" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    files = {"pdt.yml": b"apps: []\n", "hr/run.py": b"print(1)\n"}
    copies.unpack(data, entry_of(data, files, "mr=12"), tmp_path / "out")
    assert (tmp_path / "out" / "hr" / "run.py").read_bytes() == b"print(1)\n"


def test_a_folder_entry_that_leaves_the_folder_writes_nothing(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("../escape/", b"")
        zipped.writestr("pdt.yml", b"x")
    data = buffer.getvalue()
    with pytest.raises(copies.CopyError, match="leaves the folder"):
        copies.unpack(data, entry_of(data, {"pdt.yml": b"x"}), tmp_path / "out")
    assert not (tmp_path / "out" / "pdt.yml").exists()


def test_a_file_outside_the_copy_is_still_refused_next_to_folder_entries(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("hr/", b"")
        zipped.writestr("hr/run.py", b"x")
        zipped.writestr("hr/extra.py", b"y")
    data = buffer.getvalue()
    with pytest.raises(copies.CopyError, match="exactly"):
        copies.unpack(data, entry_of(data, {"hr/run.py": b"x"}), tmp_path / "out")
