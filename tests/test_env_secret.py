import json
import os

import pytest
from dotenv import dotenv_values

from pdt import deploy_aws
from pdt.utils import env_secret

ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:pdt-demo-env-AbCdEf"
GOOGLE = "projects/my-project/secrets/pdt-demo-env"
AZURE = "https://pdt-abc.vault.azure.net/secrets/pdt-demo-env"


def test_the_resource_name_picks_the_secret_store():
    assert isinstance(env_secret.backend(ARN), env_secret.SecretsManager)
    assert isinstance(env_secret.backend(GOOGLE), env_secret.SecretManager)
    assert isinstance(env_secret.backend(AZURE), env_secret.KeyVault)
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
    assert env_secret.value("TOKEN") == "new"
    assert env_secret.value("MISSING") is None


def test_without_a_cloud_secret_the_nearest_env_file_holds_the_value(tmp_path, monkeypatch):
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    app = tmp_path / "demo"
    app.mkdir()
    (tmp_path / ".env").write_text("SHARED=project\nTOKEN=old\n")
    (app / ".env").write_text("OWN=app\n")
    monkeypatch.chdir(app)
    monkeypatch.delenv(env_secret.RESOURCE_ENV, raising=False)

    assert env_secret.deployed() is False
    assert env_secret.value("TOKEN") == "old"
    env_secret.update("TOKEN", "new")
    env_secret.update("FRESH", "x")

    assert dotenv_values(tmp_path / ".env") == {"SHARED": "project", "TOKEN": "new"}
    assert dotenv_values(app / ".env") == {"OWN": "app", "FRESH": "x"}


def test_the_task_role_may_only_touch_its_own_secret():
    statements = deploy_aws.secret_statements(ARN)
    assert statements == [{"Effect": "Allow",
                           "Action": ["secretsmanager:GetSecretValue",
                                      "secretsmanager:PutSecretValue"],
                           "Resource": ARN}]
