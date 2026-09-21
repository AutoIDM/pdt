import base64
import json
import os

import pytest

from conftest import add_app
from pdt import config, deploy, deploy_common
from pdt.utils import email_auth
from pdt.utils.send_email import auth_env_file

APP_YAML = """\
env:
  required: [PDT_TOKEN]
  one_of:
    - [PDT_SECRET]
    - [PDT_KEY_B64]
  optional: [PDT_NOTE]
"""

VALUES = {"PDT_TOKEN": "t-1", "PDT_SECRET": "s-1", "PDT_NOTE": "n-1"}
NAMES = ("PDT_TOKEN", "PDT_SECRET", "PDT_KEY_B64", "PDT_NOTE", "PDT_ENV_JSON",
         "PDT_KEY_PATH", "PDT_OUT_PATH")


@pytest.fixture(autouse=True)
def restore_environ():
    # python-dotenv writes straight into os.environ, past monkeypatch.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def clear(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def test_an_app_with_no_values_set_reports_both_problems(project, monkeypatch):
    folder = add_app(project, "my-report", APP_YAML)
    clear(monkeypatch)
    config.load_env(folder)
    assert config.check_env(config.merged_app("my-report")["env"]) == [
        "missing required env var PDT_TOKEN",
        "set one of: PDT_SECRET or PDT_KEY_B64",
    ]


def test_a_dot_env_file_the_environment_and_env_json_agree(project, monkeypatch):
    folder = add_app(project, "my-report", APP_YAML)
    spec = config.merged_app("my-report")["env"]

    clear(monkeypatch)
    (folder / ".env").write_text("".join(f"{k}={v}\n" for k, v in VALUES.items()))
    config.load_env(folder)
    from_file = config.check_env(spec)

    clear(monkeypatch)
    (folder / ".env").unlink()
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)
    config.load_env(folder)
    from_environment = config.check_env(spec)

    clear(monkeypatch)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps(VALUES))
    config.load_env(folder)
    from_json = config.check_env(spec)

    assert from_file == from_environment == from_json == []


def test_the_environment_beats_a_dot_env_file_that_sets_the_same_name(project, monkeypatch):
    folder = add_app(project, "my-report", APP_YAML)
    clear(monkeypatch)
    (folder / ".env").write_text("PDT_TOKEN=from-file\n")
    monkeypatch.setenv("PDT_TOKEN", "from-environment")
    config.load_env(folder)
    assert os.environ["PDT_TOKEN"] == "from-environment"


def test_gather_secrets_is_the_same_from_a_file_and_from_the_environment(project, monkeypatch):
    folder = add_app(project, "my-report", APP_YAML)
    app = config.merged_app("my-report")

    clear(monkeypatch)
    (folder / ".env").write_text("".join(f"{k}={v}\n" for k, v in VALUES.items()))
    config.load_env(folder)
    from_file = deploy_common.gather_secrets(app)

    clear(monkeypatch)
    (folder / ".env").unlink()
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)
    config.load_env(folder)
    from_environment = deploy_common.gather_secrets(app)

    assert from_file == from_environment == VALUES


def test_auth_env_file_uses_an_existing_dot_env_even_without_a_terminal(project):
    folder = add_app(project, "my-report")
    (folder / ".env").write_text("PDT_TOKEN=t-1\n")
    assert auth_env_file(folder, interactive=False) == (folder / ".env").resolve()


def test_auth_env_file_names_a_new_dot_env_when_pdt_can_ask(project):
    folder = add_app(project, "my-report")
    assert auth_env_file(folder, interactive=True) == folder / ".env"


def test_auth_env_file_is_none_with_no_dot_env_and_no_terminal(project):
    folder = add_app(project, "my-report")
    assert auth_env_file(folder, interactive=False) is None


def test_saving_authorization_creates_no_file_in_a_ci_checkout(project, monkeypatch):
    folder = add_app(project, "my-report")
    clear(monkeypatch)
    monkeypatch.delenv("PDT_ENV_SECRET_RESOURCE", raising=False)
    monkeypatch.delenv(email_auth.CACHE_ENV, raising=False)
    before = sorted(path.name for path in folder.iterdir())

    env_file = auth_env_file(folder, interactive=False)
    assert env_file is None
    email_auth._save_cache(env_file, {"client_id": "abc"})

    assert sorted(path.name for path in folder.iterdir()) == before
    assert not (folder / ".env").exists()
    cached = json.loads(base64.b64decode(os.environ[email_auth.CACHE_ENV]))
    assert cached == {"client_id": "abc"}


def refuse_input(prompt=""):
    raise AssertionError("confirm asked a question with no one to answer")


def test_confirm_says_how_to_proceed_when_there_is_no_terminal(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", refuse_input)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert deploy.confirm(["do a thing"], assume_yes=False) is False
    assert "--yes" in capsys.readouterr().out


def test_confirm_asks_nothing_on_a_build_server_with_a_terminal(monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("builtins.input", refuse_input)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert deploy.confirm(["do a thing"], assume_yes=False) is False
    assert "--yes" in capsys.readouterr().out


def test_confirm_treats_a_person_pressing_ctrl_d_as_no_without_advice(monkeypatch, capsys):
    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", raise_eof)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert deploy.confirm(["do a thing"], assume_yes=False) is False
    assert "--yes" not in capsys.readouterr().out


def test_confirm_with_assume_yes_returns_true_without_calling_input(monkeypatch):
    monkeypatch.setattr("builtins.input", refuse_input)
    assert deploy.confirm(["do a thing"], assume_yes=True) is True


def test_a_build_server_with_a_terminal_still_gets_no_dot_env(project, monkeypatch):
    folder = add_app(project, "my-report")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert auth_env_file(folder) is None


PATH_YAML = """\
env:
  required: [PDT_KEY_PATH]
  optional: [PDT_OUT_PATH]
"""


def test_a_path_var_that_names_a_file_becomes_its_base64(project, monkeypatch):
    folder = add_app(project, "my-report", PATH_YAML)
    (folder / "key.pem").write_bytes(b"secret")
    clear(monkeypatch)
    monkeypatch.setenv("PDT_KEY_PATH", "key.pem")
    monkeypatch.setenv("PDT_OUT_PATH", "out/")
    config.load_env(folder)
    assert deploy_common.gather_secrets(config.merged_app("my-report")) == {
        "PDT_KEY_B64": base64.b64encode(b"secret").decode("ascii"),
        "PDT_OUT_PATH": "out/",
    }


def test_a_path_var_with_no_file_is_a_plain_value(project, monkeypatch, capsys):
    folder = add_app(project, "my-report", PATH_YAML)
    (folder / "key.pem").write_bytes(b"secret")
    clear(monkeypatch)
    monkeypatch.setenv("PDT_KEY_PATH", "key.pem")
    monkeypatch.setenv("PDT_OUT_PATH", ".pdt/db.duckdb")
    config.load_env(folder)
    values = deploy_common.gather_secrets(config.merged_app("my-report"))
    assert values["PDT_OUT_PATH"] == ".pdt/db.duckdb"
    assert "PDT_OUT_PATH is not a file here" in capsys.readouterr().out


def test_the_bundle_is_checked_against_the_spec_after_conversion(project, monkeypatch):
    folder = add_app(project, "my-report", PATH_YAML)
    clear(monkeypatch)
    monkeypatch.setenv("PDT_KEY_PATH", "key.pem")
    config.load_env(folder)
    values = deploy_common.gather_secrets(config.merged_app("my-report"))
    assert values == {"PDT_KEY_PATH": "key.pem"}


def test_check_env_accepts_b64_in_place_of_path():
    spec = {"required": ["PDT_KEY_PATH"], "one_of": [["PDT_CERT_PATH"]]}
    assert config.check_env(spec, {"PDT_KEY_B64": "x", "PDT_CERT_B64": "y"}) == []
    assert config.check_env(spec, {}) == [
        "missing required env var PDT_KEY_PATH",
        "set one of: PDT_CERT_PATH",
    ]
