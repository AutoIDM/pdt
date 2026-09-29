import io
import json
import os

import subprocess

import pytest
from dotenv import dotenv_values

from pdt import deploy_aws, deploy_common
from pdt.utils import env_secret

ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:pdt-demo-env-AbCdEf"
GOOGLE = "projects/my-project/secrets/pdt-demo-env"
AZURE = "https://pdt-abc.vault.azure.net/secrets/pdt-demo-env"
SNOWFLAKE = "snow://PDT.PDT_DEMO.ENV"


def test_the_resource_name_picks_the_secret_store():
    assert isinstance(env_secret.backend(ARN), env_secret.SecretsManager)
    assert isinstance(env_secret.backend(GOOGLE), env_secret.SecretManager)
    assert isinstance(env_secret.backend(AZURE), env_secret.KeyVault)
    assert isinstance(env_secret.backend(SNOWFLAKE), env_secret.SnowflakeSecret)
    assert isinstance(env_secret.backend(""), env_secret.EnvFile)
    with pytest.raises(ValueError):
        env_secret.backend("ftp://elsewhere")


def test_the_key_vault_resource_splits_into_vault_url_and_secret_name():
    vault = env_secret.KeyVault(AZURE + "/")
    assert vault.vault_url == "https://pdt-abc.vault.azure.net"
    assert vault.name == "pdt-demo-env"


class Memory(env_secret.Backend):
    values = {"KEEP": "1", "TOKEN": "old"}

    def read(self):
        return dict(self.values)

    def write(self, values):
        type(self).values = values


def test_update_changes_one_var_and_keeps_the_rest(monkeypatch):
    monkeypatch.setattr(env_secret, "backend", lambda resource=None: Memory(""))
    monkeypatch.delenv("PDT_ENV_JSON", raising=False)
    env_secret.update("TOKEN", "new")
    assert Memory.values == {"KEEP": "1", "TOKEN": "new"}
    assert os.environ["TOKEN"] == "new"
    assert json.loads(os.environ["PDT_ENV_JSON"]) == {"KEEP": "1", "TOKEN": "new"}


def test_without_a_cloud_secret_the_nearest_env_file_holds_the_value(tmp_path, monkeypatch):
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    app = tmp_path / "demo"
    app.mkdir()
    (tmp_path / ".env").write_text("SHARED=project\nTOKEN=old\n")
    (app / ".env").write_text("OWN=app\n")
    monkeypatch.chdir(app)
    monkeypatch.delenv(env_secret.RESOURCE_ENV, raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["input"], kwargs["cwd"]))
        return subprocess.CompletedProcess(command, 0, "done\n", "")

    monkeypatch.setattr(env_secret.subprocess, "run", fake_run)

    assert env_secret.deployed() is False
    env_secret.update("TOKEN", "new")
    env_secret.update("FRESH", "x")

    assert dotenv_values(tmp_path / ".env") == {"SHARED": "project", "TOKEN": "new"}
    assert dotenv_values(app / ".env") == {"OWN": "app", "FRESH": "x"}
    assert [(command[3:], value, cwd) for command, value, cwd in calls] == [
        (["secrets", "demo", "set", "TOKEN", "--yes"], "new", tmp_path.resolve()),
        (["secrets", "demo", "set", "FRESH", "--yes"], "x", tmp_path.resolve()),
    ]


def test_a_failed_deployed_update_fails_the_local_update(tmp_path, monkeypatch):
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(env_secret.RESOURCE_ENV, raising=False)
    monkeypatch.setattr(env_secret.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "denied"))
    with pytest.raises(RuntimeError):
        env_secret.update("TOKEN", "new")
    assert dotenv_values(tmp_path / ".env") == {"TOKEN": "new"}


def test_set_puts_the_stdin_value_into_the_deployed_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("rotated\n"))
    written = []
    app = {"name": "demo", "dir": tmp_path, "platform": {"provider": "aws"}}
    code = deploy_common.run_secrets("set", app, json.dumps({"A": "1", "TOKEN": "old"}),
                                     written.append, True, "TOKEN")
    assert code == 0
    assert written == [{"A": "1", "TOKEN": "rotated"}]


def test_set_on_an_undeployed_app_is_a_note_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("rotated"))
    written = []
    app = {"name": "demo", "dir": tmp_path, "platform": {"provider": "aws"}}
    code = deploy_common.run_secrets("set", app, None, written.append, True, "TOKEN")
    assert code == 0 and written == []
    assert "not deployed" in capsys.readouterr().out


def test_the_task_role_may_only_touch_its_own_secret():
    statements = deploy_aws.secret_statements(ARN)
    assert statements == [{"Effect": "Allow",
                           "Action": ["secretsmanager:GetSecretValue",
                                      "secretsmanager:PutSecretValue"],
                           "Resource": ARN}]


def test_a_deployed_job_reads_the_secret_fresh_at_start(monkeypatch):
    from pdt import config
    monkeypatch.setattr(env_secret, "backend", lambda resource=None: Memory(""))
    Memory.values = {"TOKEN": "rotated"}
    monkeypatch.setenv(env_secret.RESOURCE_ENV, AZURE)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({"TOKEN": "cached-at-deploy"}))
    monkeypatch.delenv("TOKEN", raising=False)
    config.load_env_json()
    assert os.environ["TOKEN"] == "rotated"


def test_an_unreadable_secret_leaves_the_mounted_copy_in_place(monkeypatch, capsys):
    from pdt import config

    class Broken(env_secret.Backend):
        def read(self):
            raise PermissionError("denied")

    monkeypatch.setattr(env_secret, "backend", lambda resource=None: Broken(""))
    monkeypatch.setenv(env_secret.RESOURCE_ENV, AZURE)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({"TOKEN": "cached-at-deploy"}))
    monkeypatch.delenv("TOKEN", raising=False)
    config.load_env_json()
    assert os.environ["TOKEN"] == "cached-at-deploy"
    assert "could not read the deployed secret" in capsys.readouterr().out
