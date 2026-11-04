import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from service_fixtures import CUSTOMER, INSTALLATION, FakePortal, publication, snapshot_of

from pdt import cli, config, register_cli, snapshots, sync_cli
from pdt.utils import autoidm


@pytest.fixture
def portal():
    found = FakePortal()
    yield found
    found.close()


@pytest.fixture
def project(tmp_path, monkeypatch, portal):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    for name in (*autoidm.SECRET_NAMES, "PDT_ENV_JSON", "PDT_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    folder = autoidm.folder()
    folder.mkdir(parents=True)
    register_cli.write_key(folder / f"{CUSTOMER}.key", Ed25519PrivateKey.generate())
    (folder / f"{CUSTOMER}.json").write_text(json.dumps({
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "api_url": portal.url}))
    root = tmp_path / "example"
    (root / "hr").mkdir(parents=True)
    (root / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")
    (root / "hr" / "run.py").write_text("print('hi')\n")
    (root / "hr" / ".env").write_text("TOKEN=secret\n")
    monkeypatch.chdir(root)
    return root


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.run()


def test_sync_up_lists_the_files_sends_nothing_secret_and_records_the_snapshot(project, portal,
                                                                               monkeypatch, capsys):
    portal.routes["/v1/snapshots/start"] = lambda _body: (200, {"upload": {
        "url": f"{portal.url}/bucket", "fields": {"key": "x"}, "key": "x"}})
    assert run(monkeypatch, "sync", "up", "--yes", "--ref", "abc", "--label", "MR 12") == 0
    out = capsys.readouterr().out
    assert "hr  1 file" in out
    assert ".  1 file" in out
    start = portal.bodies("/v1/snapshots/start")[0]
    paths = [entry["path"] for entry in start["manifest"]["files"]]
    assert paths == ["hr/run.py", "pdt.yml"]
    assert start["publication"]["ref"] == "abc" and start["publication"]["label"] == "MR 12"
    assert start["purpose"] == "publish" and start["publication"]["parent"] is None
    assert set(start["manifest"]) == {"files"}
    assert snapshots.publication_valid(
        {**start["publication"], "purpose": "publish",
         "public_key": autoidm.public_key(autoidm.machine_registration(CUSTOMER).key)},
        CUSTOMER, "example", start["snapshot_id"])
    assert start["snapshot_id"] == snapshots.snapshot_id(start["manifest"]["files"])
    assert b"secret" not in portal.uploads[0]
    assert portal.bodies("/v1/snapshots/finish")[0]["snapshot_id"] == start["snapshot_id"]
    assert sync_cli.read_record(project)["snapshot_id"] == start["snapshot_id"]


def test_a_file_goes_up_in_chunks_with_no_read_into_memory(portal, tmp_path, monkeypatch):
    path = tmp_path / "big.zip"
    data = os.urandom(3 * autoidm.UPLOAD_CHUNK_BYTES + 17)
    path.write_bytes(data)

    def no_read(_self):
        pytest.fail("the send read the whole file")

    monkeypatch.setattr(type(path), "read_bytes", no_read)
    place = {"url": f"{portal.url}/bucket", "fields": {"key": "projects/x.zip"}}
    autoidm.post_file(place, "big.zip", path, "/v1/snapshots/start")
    sent = portal.uploads[0]
    assert data in sent
    assert b'name="key"' in sent and b"projects/x.zip" in sent
    assert sent.endswith(b"--\r\n")


def test_bytes_and_a_file_send_the_same_parts(portal, tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"content")
    place = {"url": f"{portal.url}/bucket", "fields": {"key": "x"}}
    autoidm.post_file(place, "a.zip", path, "what")
    autoidm.post_file(place, "a.zip", b"content", "what")
    first, second = [item.replace(item[2:34], b"B") for item in portal.uploads]
    assert first == second


def test_a_repeat_sync_up_sends_no_zip(project, portal, monkeypatch, capsys):
    portal.routes["/v1/snapshots/start"] = lambda _body: (200, {"upload": None})
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    assert portal.uploads == []
    assert "already holds" in capsys.readouterr().err + "already holds"


def test_sync_up_names_each_file_it_left_out(project, portal, monkeypatch, capsys):
    (project / "prod.env").write_text("TOKEN=secret\n")
    (project / "people.csv").write_text("a,b\n")
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    out = capsys.readouterr().out
    assert "left out: prod.env" in out and "left out: people.csv" in out
    assert "left out: hr/.env" in out


def test_a_report_changes_no_record_and_names_its_purpose(project, portal, monkeypatch):
    assert run(monkeypatch, "sync", "up", "--yes", "--report") == 0
    assert portal.bodies("/v1/snapshots/start")[0]["purpose"] == "report"
    assert sync_cli.read_record(project) == {}


def test_a_later_publish_names_the_record_as_its_parent(project, portal, monkeypatch):
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    first = sync_cli.read_record(project)["snapshot_id"]
    (project / "hr" / "run.py").write_text("print('later')\n")
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    assert portal.bodies("/v1/snapshots/start")[1]["publication"]["parent"] == first


def test_a_ci_registration_sends_a_merge_request_and_changes_no_record(project, portal, monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(autoidm.ID_ENV, "ins_0000000007")
    monkeypatch.setenv(autoidm.KEY_ENV, autoidm.private_pem(key).decode())
    monkeypatch.setenv(autoidm.URL_ENV, portal.url)
    monkeypatch.setenv(autoidm.CUSTOMER_ENV, CUSTOMER)
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    assert portal.bodies("/v1/snapshots/start")[0]["purpose"] == "merge_request"
    assert sync_cli.read_record(project) == {}


def test_sync_up_waits_for_the_check_of_the_zip(project, portal, monkeypatch):
    answers = [(202, {"state": "checking"}), (200, {"state": "ready"})]
    portal.routes["/v1/snapshots/finish"] = lambda _body: answers.pop(0)
    monkeypatch.setattr(snapshots, "CHECK_SECONDS", 0)
    assert run(monkeypatch, "sync", "up", "--yes") == 0
    assert len(portal.bodies("/v1/snapshots/finish")) == 2


def test_a_rejected_snapshot_stops_sync_up_with_its_message(project, portal, monkeypatch, capsys):
    portal.routes["/v1/snapshots/finish"] = lambda _body: (
        422, {"error": "snapshot_rejected", "message": "the zip holds a link"})
    assert run(monkeypatch, "sync", "up", "--yes") == 1
    assert "the zip holds a link" in capsys.readouterr().out
    assert sync_cli.read_record(project) == {}


def test_sync_up_with_no_one_to_answer_sends_nothing(project, portal, monkeypatch, capsys):
    assert run(monkeypatch, "sync", "up") == 1
    assert portal.seen == []
    assert "--yes" in capsys.readouterr().out


def test_sync_up_stops_at_the_body_limit_with_a_plain_message(project, portal, monkeypatch, capsys):
    monkeypatch.setattr(snapshots, "MAX_BODY_BYTES", 1024 + 10)
    assert run(monkeypatch, "sync", "up", "--yes") == 1
    assert "about 2000 files" in capsys.readouterr().out
    assert portal.seen == []


def test_a_note_past_the_limit_stops_sync_up(project, portal, monkeypatch, capsys):
    assert run(monkeypatch, "sync", "up", "--yes", "--label", "x" * 257) == 1
    assert portal.seen == []


def test_a_non_ascii_path_gives_the_id_the_server_computes(project, portal, monkeypatch):
    import hashlib
    (project / "hr" / "résumé.txt").write_text("x")
    files = snapshots.collect(project)[0]
    canonical = json.dumps(files, separators=(",", ":"), sort_keys=True)
    assert "\\u00e9" in canonical
    assert snapshots.snapshot_id(files) == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_refused_request_is_one_error_line(project, portal, monkeypatch, capsys):
    portal.routes["/v1/snapshots/get"] = lambda _body: (404, {"error": "unknown_snapshot", "message": "none"})
    assert run(monkeypatch, "sync", "diff") == 1
    assert "error:" in capsys.readouterr().out


def test_sync_up_needs_managed_services(project, monkeypatch, capsys):
    (project / "pdt.yml").write_text("apps: []\n")
    assert run(monkeypatch, "sync", "up", "--yes") == 1
    assert "pdt register" in capsys.readouterr().out


def serve(portal, files, parent=None):
    identity, manifest, data = snapshot_of(files)
    portal.serve_snapshot(identity, manifest, data, [publication(identity, parent)])
    return identity


BASE = {"pdt.yml": f"managed_services:\n  customer: {CUSTOMER}\n".encode(),
        "hr/run.py": b"print('new')\n", "hr/new.py": b"new\n"}


def test_sync_down_writes_the_snapshot_and_keeps_a_file_it_does_not_list(project, portal,
                                                                         monkeypatch, capsys):
    (project / "hr" / "run.py").unlink()
    (project / "kept.txt").write_text("mine\n")
    identity = serve(portal, BASE)
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 0
    assert (project / "hr" / "run.py").read_bytes() == b"print('new')\n"
    assert (project / "hr" / "new.py").read_bytes() == b"new\n"
    assert (project / "kept.txt").read_text() == "mine\n"
    assert (project / "hr" / ".env").read_text() == "TOKEN=secret\n"
    assert sync_cli.read_record(project)["snapshot_id"] == identity
    assert portal.bodies("/v1/snapshots/finish") == []
    assert "added: hr/run.py" in capsys.readouterr().out


def test_sync_down_with_no_id_prints_the_heads_and_stops(project, portal, monkeypatch, capsys):
    (project / "hr" / "run.py").unlink()
    identity = serve(portal, BASE)
    assert run(monkeypatch, "sync", "down") == 1
    assert portal.bodies("/v1/snapshots/get") == []
    assert not (project / "hr" / "run.py").exists()
    assert f"pdt sync down --snapshot {identity}" in " ".join(capsys.readouterr().out.split())


def test_sync_down_stops_in_a_clone(project, portal, monkeypatch, capsys):
    (project / ".git").mkdir()
    identity = serve(portal, BASE)
    assert run(monkeypatch, "sync", "down", "--snapshot", identity, "--force") == 1
    assert "Git clone" in capsys.readouterr().out
    assert portal.bodies("/v1/snapshots/get") == []


def test_a_snapshot_with_another_parent_stops_sync_down(project, portal, monkeypatch, capsys):
    sync_cli.write_record(project, "old", [])
    (project / "hr" / "run.py").unlink()
    identity = serve(portal, BASE, parent="other")
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 1
    assert not (project / "hr" / "run.py").exists()
    out = " ".join(capsys.readouterr().out.split())
    assert "no publication of snapshot" in out and "--force" in out
    assert run(monkeypatch, "sync", "down", "--snapshot", identity, "--force") == 0
    assert (project / "hr" / "run.py").is_file()


def test_a_file_changed_here_stops_sync_down_and_changes_nothing(project, portal, monkeypatch, capsys):
    sync_cli.write_record(project, "old", [
        {"path": "hr/run.py", "sha256": sync_cli.file_hash(project / "hr" / "run.py"), "size": 1}])
    (project / "hr" / "run.py").write_text("print('edited here')\n")
    identity = serve(portal, BASE, parent="old")
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 1
    assert (project / "hr" / "run.py").read_text() == "print('edited here')\n"
    assert not (project / "hr" / "new.py").exists()
    assert sync_cli.read_record(project)["snapshot_id"] == "old"
    out = capsys.readouterr().out
    assert "hr/run.py" in out and "--force" in out
    assert portal.bodies("/v1/snapshots/finish") == []


def test_a_file_this_computer_does_not_know_stops_sync_down(project, portal, monkeypatch):
    identity = serve(portal, BASE)
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 1
    assert (project / "hr" / "run.py").read_text() == "print('hi')\n"


def test_force_overwrites_a_changed_file(project, portal, monkeypatch):
    identity = serve(portal, BASE)
    assert run(monkeypatch, "sync", "down", "--snapshot", identity, "--force") == 0
    assert (project / "hr" / "run.py").read_bytes() == b"print('new')\n"


def test_sync_down_deletes_only_what_the_last_snapshot_held(project, portal, monkeypatch):
    gone = project / "hr" / "gone.py"
    gone.write_text("old\n")
    sync_cli.write_record(project, "old", [
        {"path": "hr/run.py", "sha256": sync_cli.file_hash(project / "hr" / "run.py"), "size": 1},
        {"path": "hr/gone.py", "sha256": sync_cli.file_hash(gone), "size": 1}])
    (project / "untracked.txt").write_text("mine\n")
    identity = serve(portal, BASE, parent="old")
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 0
    assert not gone.exists()
    assert (project / "untracked.txt").is_file()


def test_a_changed_file_the_snapshot_would_delete_stops_sync_down(project, portal, monkeypatch):
    gone = project / "hr" / "gone.py"
    gone.write_text("old\n")
    sync_cli.write_record(project, "old", [
        {"path": "hr/gone.py", "sha256": sync_cli.file_hash(gone), "size": 1}])
    gone.write_text("edited\n")
    (project / "hr" / "run.py").unlink()
    identity = serve(portal, BASE, parent="old")
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 1
    assert gone.read_text() == "edited\n"


def test_a_hostile_snapshot_writes_nothing_in_the_project(project, portal, monkeypatch, tmp_path):
    identity, manifest, data = snapshot_of({"../evil.txt": b"evil"})
    portal.serve_snapshot(identity, manifest, data)
    (project / "hr" / "run.py").unlink()
    with pytest.raises(snapshots.SnapshotError, match="leaves the folder"):
        sync_cli.down(identity, True)
    assert not (tmp_path / "evil.txt").exists()


def test_a_record_with_a_hostile_path_deletes_nothing_outside(project, portal, monkeypatch, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    (project / ".pdt").mkdir()
    (project / ".pdt" / "sync.json").write_text(json.dumps({
        "snapshot_id": "old", "files": {"../victim.txt": sync_cli.file_hash(victim)}}))
    (project / "hr" / "run.py").unlink()
    identity = serve(portal, BASE, parent="old")
    assert run(monkeypatch, "sync", "down", "--snapshot", identity) == 0
    assert victim.is_file()


def test_a_new_project_goes_in_the_folder_the_service_names(tmp_path, portal, monkeypatch, project):
    new = tmp_path / "projects" / "fresh"
    new.mkdir(parents=True)
    monkeypatch.chdir(new)
    monkeypatch.setenv("PDT_SERVICE_PROJECT", "fresh")
    monkeypatch.setenv("PDT_SERVICE_CUSTOMER", CUSTOMER)
    identity = serve(portal, BASE)
    assert sync_cli.down(identity, False) == 0
    assert (new / "pdt.yml").is_file()
    assert portal.bodies("/v1/snapshots/get")[0]["project"] == "fresh"


def new_project(tmp_path, monkeypatch):
    new = tmp_path / "projects" / "fresh"
    new.mkdir(parents=True)
    monkeypatch.chdir(new)
    monkeypatch.setenv("PDT_SERVICE_PROJECT", "fresh")
    monkeypatch.setenv("PDT_SERVICE_CUSTOMER", CUSTOMER)
    return new


def test_a_new_project_with_no_customer_gets_the_customer(tmp_path, portal, monkeypatch, project, capsys):
    new = new_project(tmp_path, monkeypatch)
    identity = serve(portal, {"pdt.yml": b"apps: []\n", "hr/run.py": b"print('new')\n"})
    assert sync_cli.down(identity, False) == 0
    assert managed_services_customer(new) == CUSTOMER
    assert "Saved managed_services" in capsys.readouterr().out
    assert sync_cli.read_record(new)["snapshot_id"] == identity


def managed_services_customer(root):
    return config.load_yaml(root / "pdt.yml")["managed_services"]["customer"]


def test_the_customer_line_is_not_a_change_at_the_next_sync(tmp_path, portal, monkeypatch, project):
    new = new_project(tmp_path, monkeypatch)
    identity = serve(portal, {"pdt.yml": b"apps: []\n", "hr/run.py": b"print('new')\n"})
    assert sync_cli.down(identity, False) == 0
    newer = serve(portal, {"pdt.yml": b"apps: [hr]\n", "hr/run.py": b"print('newer')\n"},
                  parent=identity)
    assert sync_cli.down(newer, False) == 0
    assert (new / "hr" / "run.py").read_bytes() == b"print('newer')\n"
    assert managed_services_customer(new) == CUSTOMER
    assert sync_cli.down(newer, False) == 0


def test_a_snapshot_of_another_customer_writes_nothing(tmp_path, portal, monkeypatch, project):
    new = new_project(tmp_path, monkeypatch)
    identity = serve(portal, {"pdt.yml": b"managed_services:\n  customer: cus_0000000000\n",
                              "hr/run.py": b"print('new')\n"})
    with pytest.raises(snapshots.SnapshotError, match="cus_0000000000"):
        sync_cli.down(identity, True)
    assert list(new.iterdir()) == []


def test_a_new_project_needs_a_pdt_yml_in_the_snapshot(tmp_path, portal, monkeypatch, project, capsys):
    new = new_project(tmp_path, monkeypatch)
    identity = serve(portal, {"hr/run.py": b"print('new')\n"})
    assert sync_cli.down(identity, False) == 1
    assert list(new.iterdir()) == []
    assert "holds no pdt.yml" in capsys.readouterr().out


def test_a_snapshot_that_drops_the_customer_keeps_it_in_the_project(project, portal, monkeypatch):
    (project / "hr" / "run.py").unlink()
    identity = serve(portal, {"hr/run.py": b"print('new')\n", "pdt.yml": b"apps: []\n"})
    assert sync_cli.down(identity, True) == 0
    assert managed_services_customer(project) == CUSTOMER


def test_sync_diff_lists_the_changed_files(project, portal, monkeypatch, capsys):
    serve(portal, BASE)
    assert run(monkeypatch, "sync", "diff", "--json") == 0
    found = json.loads(capsys.readouterr().out)
    assert found["added"] == []
    assert found["removed"] == ["hr/new.py"]
    assert found["changed"] == ["hr/run.py"]


def test_sync_diff_says_when_the_folder_matches(project, portal, monkeypatch, capsys):
    files = {path: (project / path).read_bytes() for path in ("pdt.yml", "hr/run.py")}
    serve(portal, files)
    assert run(monkeypatch, "sync", "diff") == 0
    assert "matches the snapshot" in capsys.readouterr().out


def test_the_excludes_are_the_build_excludes():
    from pdt.deploy_common import BUILD_EXCLUDES
    assert set(BUILD_EXCLUDES) <= set(snapshots.LEFT_OUT)
    assert config.STATE_DIR == ".pdt"
