import json
import os
import subprocess

import pytest

from pdt import deploy_common, scaffold
from pdt.utils import email_auth, env_secret

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows has no owner-only file mode")


def mode(path):
    return path.stat().st_mode & 0o777


def readable(path):
    path.write_text("")
    path.chmod(0o644)
    return path


def test_an_existing_world_readable_file_becomes_owner_only(tmp_path):
    path = readable(tmp_path / ".env")
    env_secret.private_file(path)
    assert mode(path) == 0o600


def test_a_missing_file_is_created_owner_only(tmp_path):
    path = tmp_path / ".env"
    env_secret.private_file(path)
    assert path.read_text() == "" and mode(path) == 0o600


def test_saving_email_authorization_makes_the_env_file_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv(email_auth.CACHE_ENV, raising=False)
    env_file = readable(tmp_path / ".env")
    email_auth._save_cache(env_file, {"client_id": "abc"})
    assert mode(env_file) == 0o600


def test_updating_a_secret_locally_makes_the_env_file_owner_only(tmp_path, monkeypatch):
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    env_file = readable(tmp_path / ".env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(env_secret.RESOURCE_ENV, raising=False)
    monkeypatch.setattr(env_secret.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""))
    env_secret.update("TOKEN", "new")
    assert mode(env_file) == 0o600


def test_init_writes_an_owner_only_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    assert mode(tmp_path / ".env") == 0o600


def test_init_makes_an_existing_env_file_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    env_file = readable(tmp_path / ".env")
    assert scaffold.init(None, assume_yes=True) == 0
    assert mode(env_file) == 0o600


def test_secrets_get_makes_the_output_file_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "can_prompt", lambda interactive: False)
    target = readable(tmp_path / ".env.aws")
    app = {"name": "demo", "dir": tmp_path, "platform": {"provider": "aws"}}
    assert deploy_common.run_secrets("get", app, json.dumps({"A": "1"}), None, True) == 0
    assert target.read_text() == "A=1\n" and mode(target) == 0o600
