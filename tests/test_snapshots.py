import io
import json
import os
import stat
import subprocess
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from service_fixtures import CUSTOMER, INSTALLATION, FakePortal, snapshot_of

from pdt import snapshots
from pdt.utils import autoidm
from pdt.snapshots import SnapshotError, check_manifest, collect, path_problem, snapshot_id, unpack


def project(root, files):
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


FILES = {"pdt.yml": b"apps: []\n", "hr/run.py": b"print('hi')\n", "hr/config.yml": b"schedule: daily\n"}


def test_the_same_files_give_the_same_id_in_any_folder(tmp_path):
    first = project(tmp_path / "a", FILES)
    second = project(tmp_path / "b", dict(reversed(list(FILES.items()))))
    os.utime(second / "pdt.yml", (1, 1))
    assert snapshot_id(collect(first)[0]) == snapshot_id(collect(second)[0])


def test_a_changed_file_gives_another_id(tmp_path):
    first = project(tmp_path / "a", FILES)
    second = project(tmp_path / "b", {**FILES, "pdt.yml": b"apps: [x]\n"})
    assert snapshot_id(collect(first)[0]) != snapshot_id(collect(second)[0])


def test_the_id_is_the_hash_of_the_canonical_list():
    import hashlib
    files = [{"path": "a", "sha256": "0" * 64, "size": 1}]
    assert snapshot_id(files) == hashlib.sha256(
        b'[{"path":"a","sha256":"' + b"0" * 64 + b'","size":1}]').hexdigest()


def test_the_list_is_sorted_with_a_forward_slash(tmp_path):
    root = project(tmp_path / "a", FILES)
    assert [entry["path"] for entry in collect(root)[0]] == ["hr/config.yml", "hr/run.py", "pdt.yml"]


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "hr/.env", "hr/.pdt/runs/1/output/x", ".pdt/state",
    "server.key", "hr/id_rsa", "credentials.json", ".venv/bin/python", "pdt.yml.lock",
    "prod.env", "hr/people.csv", "data.tsv", "book.xlsx", "login.ppk", "known_hosts",
    ".secrets-agents/x.txt", "hr/artifacts/out.txt"])
def test_a_snapshot_leaves_out_secrets_and_local_state(tmp_path, name):
    root = project(tmp_path / "a", {**FILES, name: b"secret"})
    files, left_out = collect(root)
    assert name not in [entry["path"] for entry in files]
    assert any(name == path or name.startswith(path + "/") for path in left_out)


def test_a_snapshot_leaves_out_each_link(tmp_path):
    outside = project(tmp_path / "outside", {"secret.txt": b"secret"})
    root = project(tmp_path / "a", FILES)
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linked").symlink_to(outside)
    paths = [entry["path"] for entry in collect(root)[0]]
    assert "link.txt" not in paths and not any(path.startswith("linked") for path in paths)


def test_a_snapshot_leaves_out_a_file_over_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_FILE_BYTES", 4)
    root = project(tmp_path / "a", {"big.bin": b"12345", "small.bin": b"1"})
    files, left_out = collect(root)
    assert [entry["path"] for entry in files] == ["small.bin"]
    assert "larger" in left_out["big.bin"]


def test_two_zips_of_the_same_files_hold_the_same_bytes(tmp_path):
    root = project(tmp_path / "a", FILES)
    files = collect(root)[0]
    snapshots.build_zip(root, files, tmp_path / "one.zip")
    os.utime(root / "pdt.yml", (1, 1))
    snapshots.build_zip(root, files, tmp_path / "two.zip")
    assert (tmp_path / "one.zip").read_bytes() == (tmp_path / "two.zip").read_bytes()


def test_a_zip_stops_when_a_file_changed_after_the_read(tmp_path):
    root = project(tmp_path / "a", FILES)
    files = collect(root)[0]
    (root / "pdt.yml").write_bytes(b"changed")
    with pytest.raises(SnapshotError, match="changed while"):
        snapshots.build_zip(root, files, tmp_path / "one.zip")


def test_a_snapshot_round_trip(tmp_path):
    root = project(tmp_path / "a", FILES)
    files = collect(root)[0]
    snapshots.build_zip(root, files, tmp_path / "s.zip")
    target = tmp_path / "out"
    target.mkdir()
    unpack(tmp_path / "s.zip", files, target)
    assert (target / "hr" / "run.py").read_bytes() == FILES["hr/run.py"]
    assert snapshot_id(collect(target)[0]) == snapshot_id(files)


@pytest.mark.parametrize("path", [
    "../evil", "a/../../evil", "/etc/passwd", "a//b", "./a", "a/.", "..", "",
    "C:/Windows/x", "C:evil", "a\\b", "..\\evil", "\\\\server\\share", "file.txt:stream",
    "a\x00b", "a\nb", "CON", "nul.txt", "a/COM1", "trailing.", "trailing ",
    ".env", "a/.env", ".git/hooks/pre-commit", ".pdt/state",
    "x" * 2000, 5, None])
def test_a_manifest_path_that_could_do_harm_is_refused(path):
    assert path_problem(path) != ""


@pytest.mark.parametrize("path", ["pdt.yml", "hr/run.py", "a b/c d.txt", "hr/data.v2.csv", "x/server.pem"])
def test_a_plain_manifest_path_passes(path):
    assert path_problem(path) == ""


def entry(path="a.txt", data=b"x"):
    return {"path": path, "sha256": snapshots.content_hash(data), "size": len(data)}


@pytest.mark.parametrize("manifest, says", [
    (None, "no list"), ({}, "no list"), ({"files": "x"}, "no list"),
    ({"files": [{"path": "a"}]}, "does not hold"),
    ({"files": [{**entry(), "extra": 1}]}, "does not hold"),
    ({"files": [entry("../evil")]}, "leaves the folder"),
    ({"files": [{**entry(), "sha256": "abc"}]}, "SHA-256"),
    ({"files": [{**entry(), "size": -1}]}, "size"),
    ({"files": [{**entry(), "size": True}]}, "size"),
    ({"files": [entry("A.txt"), entry("a.txt")]}, "twice"),
])
def test_a_hostile_manifest_is_refused(manifest, says):
    with pytest.raises(SnapshotError, match=says):
        check_manifest(manifest, None)


def test_a_manifest_with_another_id_is_refused():
    with pytest.raises(SnapshotError, match="has the ID"):
        check_manifest({"files": [entry()]}, "0" * 64)


def hostile_zip(tmp_path, build) -> tuple:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        build(zipped)
    path = tmp_path / "hostile.zip"
    path.write_bytes(buffer.getvalue())
    target = tmp_path / "target"
    target.mkdir()
    return path, target


def nothing_written(tmp_path, target):
    assert list(target.iterdir()) == []
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()


@pytest.mark.parametrize("name", ["../evil", "../../evil", "/tmp/pdt-evil", "..\\evil", "a/../../evil"])
def test_a_zip_entry_outside_the_manifest_writes_nothing(tmp_path, name):
    def build(zipped):
        zipped.writestr("a.txt", "x")
        zipped.writestr(name, "evil")
    path, target = hostile_zip(tmp_path, build)
    with pytest.raises(SnapshotError, match="exactly"):
        unpack(path, [entry()], target)
    nothing_written(tmp_path, target)


def test_a_manifest_that_lists_a_hostile_path_stops_before_the_zip(tmp_path):
    with pytest.raises(SnapshotError, match="leaves the folder"):
        check_manifest({"files": [entry("../evil", b"evil")]}, None)


def test_a_duplicate_zip_entry_writes_nothing(tmp_path):
    def build(zipped):
        zipped.writestr("a.txt", "x")
        with pytest.warns(UserWarning):
            zipped.writestr("a.txt", "evil")
    path, target = hostile_zip(tmp_path, build)
    with pytest.raises(SnapshotError, match="exactly"):
        unpack(path, [entry()], target)
    nothing_written(tmp_path, target)


def test_a_link_in_a_zip_writes_nothing(tmp_path):
    def build(zipped):
        info = zipfile.ZipInfo("a.txt")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, "/etc/passwd")
    path, target = hostile_zip(tmp_path, build)
    with pytest.raises(SnapshotError, match="not a file"):
        unpack(path, [entry("a.txt", b"/etc/passwd")], target)
    nothing_written(tmp_path, target)


def test_a_file_that_does_not_match_the_manifest_is_refused(tmp_path):
    path, target = hostile_zip(tmp_path, lambda zipped: zipped.writestr("a.txt", "evil"))
    with pytest.raises(SnapshotError, match="does not match"):
        unpack(path, [entry()], target)


def test_a_file_larger_than_its_manifest_size_is_not_read_in_full(tmp_path):
    path, target = hostile_zip(tmp_path, lambda zipped: zipped.writestr("a.txt", b"0" * 100000))
    with pytest.raises(SnapshotError, match="does not match"):
        unpack(path, [entry("a.txt", b"0")], target)


def test_unpack_needs_an_empty_folder(tmp_path):
    path, target = hostile_zip(tmp_path, lambda zipped: zipped.writestr("a.txt", "x"))
    (target / "kept").write_text("x")
    with pytest.raises(SnapshotError, match="not empty"):
        unpack(path, [entry()], target)


def test_inside_refuses_a_link_that_leads_out(tmp_path):
    outside = project(tmp_path / "outside", {"x.txt": b"1"})
    root = project(tmp_path / "root", {"a.txt": b"1"})
    (root / "out").symlink_to(outside)
    (root / "file").symlink_to(outside / "x.txt")
    with pytest.raises(SnapshotError, match="leads out"):
        snapshots.inside(root, "out/x.txt")
    with pytest.raises(SnapshotError, match="leads out"):
        snapshots.inside(root, "file")
    assert snapshots.inside(root, "a.txt") == root / "a.txt"


def test_a_download_stops_past_its_limit(tmp_path):
    from service_fixtures import FakePortal
    portal = FakePortal()
    portal.zips["big"] = b"0" * 5000
    with pytest.raises(SnapshotError, match="larger than"):
        snapshots.download_part(f"{portal.url}/zip/big", tmp_path / "out.zip", 100)
    portal.close()


def registration(url):
    return autoidm.Registration(INSTALLATION, url, Ed25519PrivateKey.generate(), "test")


def test_a_failed_download_gets_a_new_url_and_goes_on_with_a_range(tmp_path):
    portal = FakePortal()
    data = os.urandom(4000)
    portal.zips["big"] = data
    portal.zip_cut_after = [1500]
    portal.routes["/v1/snapshots/get"] = lambda _body: (200, {"url": f"{portal.url}/zip/big"})
    target = tmp_path / "out.zip"
    snapshots.download(registration(portal.url), "example", "big", f"{portal.url}/zip/big", target,
                       10000)
    assert target.read_bytes() == data
    assert portal.ranges == [None, "bytes=1500-"]
    assert len(portal.bodies("/v1/snapshots/get")) == 1
    portal.close()


def test_a_download_stops_after_five_tries(tmp_path):
    portal = FakePortal()
    portal.routes["/v1/snapshots/get"] = lambda _body: (200, {"url": f"{portal.url}/zip/none"})
    with pytest.raises(SnapshotError, match="in 5 tries"):
        snapshots.download(registration(portal.url), "example", "none", f"{portal.url}/zip/none",
                           tmp_path / "out.zip", 100)
    assert len(portal.bodies("/v1/snapshots/get")) == 4
    portal.close()


def test_a_text_file_has_one_hash_for_each_line_ending():
    assert snapshots.content_hash(b"a\r\nb\r\n") == snapshots.content_hash(b"a\nb\n")


def test_a_binary_file_keeps_its_bytes_in_the_hash():
    assert snapshots.content_hash(b"\0a\r\n") != snapshots.content_hash(b"\0a\n")


def test_the_size_is_the_raw_size(tmp_path):
    root = project(tmp_path / "a", {"t.txt": b"a\r\n"})
    assert collect(root)[0][0]["size"] == 3


def git(root, *args):
    subprocess.run(["git", "-c", "user.email=a@example.com", "-c", "user.name=a", "-C", str(root),
                    *args], check=True, capture_output=True)


def test_a_clone_gives_only_tracked_files_and_names_the_rest(tmp_path):
    root = project(tmp_path / "a", {**FILES, "people.csv": b"tracked", "notes.txt": b"untracked",
                                    "cache/x.bin": b"untracked"})
    git(root, "init", "-q")
    git(root, "add", "pdt.yml", "hr", "people.csv")
    files, left_out = collect(root)
    assert [entry["path"] for entry in files] == ["hr/config.yml", "hr/run.py", "pdt.yml",
                                                  "people.csv"]
    assert left_out["notes.txt"] == "not tracked by Git"
    assert left_out["cache"] == "not tracked by Git"


def test_a_clone_leaves_out_a_tracked_env_file_and_a_tracked_link(tmp_path):
    root = project(tmp_path / "a", {**FILES, ".env": b"TOKEN=secret"})
    (root / "link.txt").symlink_to(root / "pdt.yml")
    git(root, "init", "-q")
    git(root, "add", "-f", ".")
    files, left_out = collect(root)
    paths = [entry["path"] for entry in files]
    assert ".env" not in paths and "link.txt" not in paths
    assert left_out["link.txt"] == "a link"
    assert left_out[".env"] == "pdt's own state"


AUTHOR = Ed25519PrivateKey.generate()


def signed(identity="a" * 64, key=AUTHOR, **changes):
    fields = {"purpose": "publish", "ref": "abc", "label": None, "parent": None, **changes}
    return {**fields, "public_key": autoidm.public_key(key), "signature": snapshots.sign_publication(
        key, CUSTOMER, "example", identity, fields["purpose"], fields["ref"], fields["label"],
        fields["parent"])}


def test_a_signature_signs_the_exact_bytes_of_the_contract():
    raw = snapshots.publication_bytes(CUSTOMER, "example", "a" * 64, "publish", None, "MR 1", None)
    assert raw == (b'{"customer_id":"' + CUSTOMER.encode() + b'","label":"MR 1","parent":null,'
                   b'"project":"example","purpose":"publish","ref":null,"snapshot_id":"'
                   + b"a" * 64 + b'"}')


def test_a_valid_publication_passes():
    assert snapshots.publication_valid(signed(), CUSTOMER, "example", "a" * 64)


@pytest.mark.parametrize("change", [{"ref": "other"}, {"parent": "b" * 64}, {"purpose": "report"},
                                    {"signature": "AAAA"}, {"public_key": "@@"}])
def test_a_changed_publication_fails(change):
    assert not snapshots.publication_valid({**signed(), **change}, CUSTOMER, "example", "a" * 64)


def test_a_signature_of_another_snapshot_or_project_fails():
    assert not snapshots.publication_valid(signed(), CUSTOMER, "example", "b" * 64)
    assert not snapshots.publication_valid(signed(), CUSTOMER, "other", "a" * 64)


def test_the_author_must_be_on_the_list():
    public = autoidm.public_key(AUTHOR)
    item = signed()
    assert snapshots.author_problem([item], CUSTOMER, "example", "a" * 64, {public}) == ""
    assert "no author" in snapshots.author_problem([item], CUSTOMER, "example", "a" * 64, {"other"})


def test_a_worker_needs_one_valid_signature_and_no_list():
    assert snapshots.author_problem([signed()], CUSTOMER, "example", "a" * 64, None) == ""
    assert "no valid signature" in snapshots.author_problem([], CUSTOMER, "example", "a" * 64, None)


def test_snapshot_of_in_the_fixtures_matches_the_module(tmp_path):
    identity, manifest, _data = snapshot_of({"a.txt": b"x"})
    assert check_manifest(manifest, identity)[0] == identity
    assert json.dumps(manifest["files"][0], sort_keys=True)
