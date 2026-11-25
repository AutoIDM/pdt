import io
import json

from pdt import deploy_aws_batch


class Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"TOKEN": "old"})}


def test_aws_set_stores_the_value_under_the_env_var_name(project, monkeypatch):
    written = {}
    clients = {"sts": None, "iam": None, "secretsmanager": Secrets()}
    monkeypatch.setattr(deploy_aws_batch, "ensure_session", lambda app: None)
    monkeypatch.setattr(deploy_aws_batch, "aws_settings", lambda app, session: ("1", "us-east-2"))
    monkeypatch.setattr(deploy_aws_batch, "batch_clients", lambda session: clients)
    monkeypatch.setattr(deploy_aws_batch, "preflight", lambda *args: ("1", lambda: None))
    monkeypatch.setattr(deploy_aws_batch, "resource_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(deploy_aws_batch, "ensure_secret",
                        lambda client, name, payload: written.update({name: json.loads(payload)}))
    monkeypatch.setattr("sys.stdin", io.StringIO("new\n"))
    assert deploy_aws_batch.secrets({"name": "demo", "platform": {"provider": "aws"}}, "set", True,
                                    "TOKEN") == 0
    assert written == {"pdt-demo-env": {"TOKEN": "new"}}
