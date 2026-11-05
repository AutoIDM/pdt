import ast
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from service_fixtures import COMMAND, CUSTOMER, INSTALLATION, KEYS, PRIVATE_PEM, claims, sign, token

from pdt import service_commands
from pdt.service_commands import OPERATIONS, Refused, command_line, verify


def check(signed, command_id=COMMAND, keys=KEYS, now=None):
    return verify(signed, command_id, INSTALLATION, CUSTOMER, keys,
                  time.time() if now is None else now)


def test_a_good_token_passes_and_its_limit_never_passes_the_table():
    found = check(token("deploy", {"project": "example", "app": "hr"}, limit_seconds=999999))
    assert found["operation"] == "deploy"
    assert found["limit_seconds"] == OPERATIONS["deploy"].limit_seconds


def test_a_bad_signature_is_refused():
    other = generate_private_key(SECP256R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    with pytest.raises(Refused, match="failed its check"):
        check(sign(claims(), key=other))


def test_a_changed_body_is_refused():
    head, _body, signature = token().split(".")
    _h, body, _s = token("destroy", {"project": "example", "app": "hr"}).split(".")
    with pytest.raises(Refused, match="failed its check"):
        check(f"{head}.{body}.{signature}")


def test_an_unknown_key_is_refused():
    with pytest.raises(Refused, match="does not know"):
        check(sign(claims(), kid="other"))


def test_a_token_with_another_algorithm_is_refused():
    secret = b"x" * 64
    with pytest.raises(Refused, match="failed its check"):
        check(sign(claims(), key=secret, alg="HS256"))
    unsigned = jwt.encode(claims(), None, algorithm="none", headers={"kid": "key-1"})
    with pytest.raises(Refused, match="failed its check"):
        check(unsigned)


def test_the_public_key_used_as_an_hmac_secret_is_refused():
    head = jwt.utils.base64url_encode(b'{"alg":"HS256","kid":"key-1"}').decode()
    with pytest.raises(Refused):
        check(f"{head}.e30.AAAA")


def test_a_wrong_audience_is_refused():
    with pytest.raises(Refused, match="another installation"):
        check(token(aud="ins_0000000000"))


def test_a_wrong_customer_is_refused():
    with pytest.raises(Refused, match="another customer"):
        check(token(customer_id="cus_0000000000"))


def test_an_expired_token_is_refused():
    with pytest.raises(Refused, match="expired"):
        check(token(exp=int(time.time()) - 3600))


def test_a_missing_expiry_is_refused():
    payload = claims()
    del payload["exp"]
    with pytest.raises(Refused):
        check(sign(payload))


def test_a_token_for_another_command_is_refused():
    with pytest.raises(Refused, match="another command"):
        check(token(), command_id="cmd_20261103T120000Z_ffffffff")


@pytest.mark.parametrize("command_id", ["../../x", "cmd_1", "", None, "cmd_20261103T120000Z_0123abcd\n"])
def test_a_bad_command_id_is_refused(command_id):
    with pytest.raises(Refused, match="command ID"):
        check(token(), command_id=command_id)


@pytest.mark.parametrize("name", ["shell", "exec", "__class__", "", 5, None])
def test_an_unknown_operation_is_refused(name):
    with pytest.raises(Refused, match="does not know the operation"):
        check(token(name))


@pytest.mark.parametrize("operation, args, says", [
    ("list", {}, "missing"),
    ("list", {"project": "example", "extra": "x"}, "not one this operation takes"),
    ("list", {"project": "example", "command": "whoami"}, "not one this operation takes"),
    ("list", {"project": "../up"}, "form"),
    ("list", {"project": "a b"}, "form"),
    ("list", {"project": ".."}, "form"),
    ("list", {"project": "example\n"}, "form"),
    ("list", {"project": 5}, "form"),
    ("list", {"project": ["example"]}, "form"),
    ("deploy", {"project": "example", "app": "--all"}, "form"),
    ("deploy", {"project": "example", "app": "-y"}, "form"),
    ("deploy", {"project": "example", "app": "a;rm -rf /"}, "form"),
    ("deploy", {"project": "example", "app": "$(whoami)"}, "form"),
    ("logs", {"project": "example", "app": "hr", "run": "1234567"}, "form"),
    ("logs", {"project": "example", "app": "hr", "run": "--full"}, "form"),
    ("run", {"project": "example", "app": "hr", "deployed": "yes"}, "true or false"),
    ("run", {"project": "example", "app": "hr"}, "missing"),
    ("sync_down", {"project": "example", "snapshot_id": "a" * 64}, "not one this operation takes"),
    ("secrets_set", {"project": "example", "app": "hr", "name": "1BAD"}, "form"),
    ("secrets_set", {"project": "example", "app": "hr", "name": "A" * 129}, "form"),
    ("run_mr", {"project": "example", "app": "hr", "mr_iid": "12", "sha": "abc"}, "form"),
    ("run_mr", {"project": "example", "app": "hr", "mr_iid": "x", "sha": "a" * 40}, "form"),
    ("run_mr", {"project": "example", "app": "hr", "mr_iid": "12", "sha": "a" * 40,
                "app_args": "--a\n--b"}, "control character"),
    ("run_mr", {"project": "example", "app": "hr", "mr_iid": "12", "sha": "a" * 40,
                "app_args": "x" * 1025}, "at most 1024"),
    ("artifacts", {"project": "example", "app": "hr"}, "missing"),
    ("list", "not an object", "not an object"),
])
def test_a_bad_arg_is_refused(operation, args, says):
    with pytest.raises(Refused, match=says):
        check(token(operation, args))


@pytest.mark.parametrize("operation, args, argv", [
    ("list", {"project": "p"}, ["list", "--json"]),
    ("validate", {"project": "p", "app": "hr"}, ["validate"]),
    ("runs", {"project": "p", "app": "hr"}, ["runs", "hr", "--json"]),
    ("logs", {"project": "p", "app": "hr"}, ["logs", "hr"]),
    ("logs", {"project": "p", "app": "hr", "run": "3"}, ["logs", "hr", "3"]),
    ("health", {"project": "p"}, ["health", "--json"]),
    ("plan", {"project": "p", "app": "hr"}, ["deploy", "hr", "--plan"]),
    ("run", {"project": "p", "app": "hr", "deployed": False}, ["run", "hr"]),
    ("run", {"project": "p", "app": "hr", "deployed": True}, ["run", "hr", "--deployed", "--follow"]),
    ("kill", {"project": "p", "app": "hr"}, ["run", "hr", "--kill"]),
    ("pause", {"project": "p", "app": "hr"}, ["pause", "hr"]),
    ("unpause", {"project": "p", "app": "hr"}, ["unpause", "hr"]),
    ("deploy", {"project": "p", "app": "hr"}, ["deploy", "hr", "--yes"]),
    ("destroy", {"project": "p", "app": "hr"}, ["destroy", "hr", "--yes"]),
    ("secrets_set", {"project": "p", "app": "hr", "name": "TOKEN"}, ["secrets", "hr", "set", "TOKEN"]),
    ("sync_up", {"project": "p"}, ["sync", "up", "--yes"]),
    ("sync_up", {"project": "p", "app": "hr"}, ["sync", "up", "hr", "--yes"]),
    ("sync_down", {"project": "p"}, ["sync", "down", "--yes"]),
    ("sync_down", {"project": "p", "app": "hr"}, ["sync", "down", "hr", "--yes"]),
    ("run_mr", {"project": "p", "app": "hr", "mr_iid": "12", "sha": "a" * 40,
                "app_args": "--full"}, ["run", "hr"]),
])
def test_each_operation_maps_to_one_command_line_of_pdt(operation, args, argv):
    assert service_commands.args_problem(operation, args) == ""
    assert command_line(operation, args) == [sys.executable, "-m", "pdt.cli", *argv]


def test_artifacts_runs_the_run_output_module():
    args = {"project": "p", "app": "hr", "run": "3"}
    assert command_line("artifacts", args) == [sys.executable, "-m", "pdt.run_output", "hr", "3"]


def test_the_table_is_the_spec_table():
    rows = {name: (operation.expires_seconds, operation.limit_seconds,
                   operation.runner_limit_seconds)
            for name, operation in OPERATIONS.items()}
    assert rows == {
        "list": (3600, 300, 300), "validate": (3600, 300, 300), "runs": (3600, 300, 300),
        "logs": (3600, 300, 300), "health": (3600, 300, 300), "artifacts": (3600, 900, 900),
        "plan": (3600, 900, 900), "run": (3600, 21600, 3300), "kill": (3600, 300, 300),
        "pause": (86400, 900, 900), "unpause": (86400, 900, 900),
        "deploy": (86400, 3600, 3300), "destroy": (900, 3600, 3300),
        "secrets_set": (900, 300, 300), "sync_up": (86400, 900, None),
        "sync_down": (86400, 900, None), "run_mr": (3600, 21600, 3300)}
    exclusive = {name for name, operation in OPERATIONS.items() if operation.exclusive}
    assert exclusive == {"sync_up", "sync_down", "deploy", "destroy"}


def test_no_arg_value_reaches_the_command_line_except_the_validated_fields():
    for name, operation in OPERATIONS.items():
        safe = {"project": "projectname", "app": "appname", "run": "12", "deployed": True,
                "name": "TOKEN", "mr_iid": "12", "sha": "a" * 40, "app_args": "APPARGSVALUE",
                "timezone": "TIMEZONEVALUE"}
        argv = operation.argv({key: safe[key] for key in (*operation.required, *operation.optional)})
        assert "projectname" not in argv, name
        assert "APPARGSVALUE" not in argv, name
        assert "TIMEZONEVALUE" not in argv, name


def test_no_service_module_starts_a_shell():
    source = Path(service_commands.__file__).parent
    for name in ("service.py", "service_commands.py", "service_runner.py", "service_backends.py",
                 "hosted_runner.py"):
        for node in ast.walk(ast.parse((source / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                pytest.fail(f"{name} passes shell=")
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen"):
                pytest.fail(f"{name} uses os.{node.attr}")


def test_the_private_key_is_not_a_pinned_key():
    assert all(PRIVATE_PEM.decode() != key["pem"] for key in KEYS)


