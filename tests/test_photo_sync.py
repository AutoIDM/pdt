from __future__ import annotations

import runpy
from pathlib import Path

import pytest


RUN = runpy.run_path(
    Path(__file__).parent.parent / "src/pdt/examples/photo-sync/run.py")


@pytest.mark.parametrize("dry_run,users,current,expected", [
    (True, [{"id": "1", "mail": "user@example.com", "accountEnabled": True}], None, []),
    (False, [{"id": "1", "mail": "user@example.com", "accountEnabled": True}], b"photo", []),
    (False, [{"id": "1", "mail": "user@example.com", "accountEnabled": False}], None, []),
    (False, [{"id": "1", "mail": "user@example.com", "accountEnabled": True},
             {"id": "2", "mail": "USER@example.com", "accountEnabled": True}], None, []),
    (False, [{"id": "1", "mail": "user@example.com", "accountEnabled": True}], None, ["1"]),
])
def test_photo_upload_requires_a_unique_enabled_match(monkeypatch, dry_run, users, current, expected):
    main = RUN["main"]
    scope = main.__globals__
    uploaded = []
    monkeypatch.setitem(scope, "load_env", lambda path: [])
    monkeypatch.setitem(scope, "merged_app", lambda name: {
        "env": {}, "config": {"bamboohr_subdomain": "example", "photo_size": "large",
                                "dry_run": dry_run}})
    monkeypatch.setitem(scope, "check_env", lambda env: [])
    monkeypatch.setitem(scope, "graph_token", lambda code: "test-token")
    monkeypatch.setitem(scope, "fetch_bamboo_employees", lambda *args: [
        {"id": "employee", "status": "Active", "isPhotoUploaded": True,
         "workEmail": " USER@example.com "}])
    monkeypatch.setitem(scope, "fetch_entra_users", lambda token: users)
    monkeypatch.setitem(scope, "fetch_bamboo_photo", lambda *args: b"photo")
    monkeypatch.setitem(scope, "fetch_entra_photo", lambda *args: current)
    monkeypatch.setitem(scope, "put_entra_photo", lambda token, user, photo: uploaded.append(user))
    assert main() == 0
    assert uploaded == expected
