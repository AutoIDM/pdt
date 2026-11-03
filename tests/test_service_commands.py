import ast
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from service_fixtures import COMMAND, CUSTOMER, INSTALLATION, KEYS, PRIVATE_PEM, claims, sign, token

from pdt import config, service_commands
from pdt.service_commands import OPERATIONS, Refused, command_line, load_preset, verify


def check(signed, maximum="manage", worker=False, command_id=COMMAND, keys=KEYS):
    return verify(signed, command_id, INSTALLATION, CUSTOMER, keys, maximum, worker)


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


@pytest.mark.parametrize("name, level, maximum", [
    ("deploy", "manage", "run"), ("run", "run", "view"), ("sync_up", "manage", "view")])
def test_a_command_above_the_maximum_level_is_refused(name, level, maximum):
    args = {"project": "example"}
    if "app" in OPERATIONS[name].required:
        args["app"] = "hr"
    if name == "run":
        args["deployed"] = False
    with pytest.raises(Refused, match=f"at most the level {maximum}, and {name} needs {level}"):
        check(token(name, args), maximum=maximum)


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
    ("sync_down", {"project": "example", "snapshot_id": "abc"}, "form"),
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
    ("pause", {"project": "p", "app": "hr"}, ["pause", "hr"]),
    ("unpause", {"project": "p", "app": "hr"}, ["unpause", "hr"]),
    ("sync_up", {"project": "p"}, ["sync", "up", "--yes"]),
    ("sync_down", {"project": "p", "snapshot_id": "a" * 64}, ["sync", "down", "--snapshot", "a" * 64]),
    ("run_snapshot", {"project": "p", "snapshot_id": "a" * 64, "app": "hr"}, ["run", "hr"]),
    ("deploy", {"project": "p", "app": "hr"}, ["deploy", "hr", "--yes"]),
    ("destroy", {"project": "p", "app": "hr"}, ["destroy", "hr", "--yes"]),
])
def test_each_operation_maps_to_one_command_line_of_pdt(operation, args, argv):
    assert service_commands.args_problem(OPERATIONS[operation], args) == ""
    assert command_line(operation, args) == [sys.executable, "-m", "pdt.cli", *argv]


def test_the_table_is_the_contract_table():
    rows = {name: (operation.level, operation.expires_seconds, operation.limit_seconds)
            for name, operation in OPERATIONS.items()}
    assert rows == {
        "list": ("view", 3600, 300), "validate": ("view", 3600, 300), "runs": ("view", 3600, 300),
        "logs": ("view", 3600, 300), "health": ("view", 3600, 300), "plan": ("view", 3600, 900),
        "run": ("run", 3600, 21600), "pause": ("run", 86400, 900), "unpause": ("run", 86400, 900),
        "sync_up": ("manage", 86400, 900), "sync_down": ("manage", 86400, 900),
        "run_snapshot": ("manage", 3600, 21600), "deploy": ("manage", 86400, 3600),
        "destroy": ("manage", 900, 3600)}


def test_no_arg_value_reaches_the_command_line_except_the_validated_fields():
    args = {"project": "PROJECTVALUE", "app": "APPVALUE", "preset": "PRESETVALUE",
            "deployed": False}
    assert command_line("run", args)[3:] == ["run", "APPVALUE"]
    for name, operation in OPERATIONS.items():
        safe = {"project": "projectname", "app": "appname", "run": "12", "deployed": True,
                "snapshot_id": "a" * 64, "preset": "presetname"}
        argv = operation.argv({key: safe[key] for key in (*operation.required, *operation.optional)})
        assert "projectname" not in argv, name
        assert "presetname" not in argv, name


def test_no_service_module_starts_a_shell():
    source = Path(service_commands.__file__).parent
    for name in ("service.py", "service_commands.py", "service_runner.py", "service_backends.py"):
        for node in ast.walk(ast.parse((source / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                pytest.fail(f"{name} passes shell=")
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen"):
                pytest.fail(f"{name} uses os.{node.attr}")


def worker_token(operation="deploy", **changes):
    return token(operation, {"project": "p", "app": "hr", "snapshot_id": "a" * 64},
                 grant={"place": "aws:123456789012:us-east-2",
                        "role": "arn:aws:iam::123456789012:role/pdt-autoidm"}, **changes)


def test_a_worker_takes_a_grant_and_a_snapshot():
    assert check(worker_token(), worker=True)["grant"]["role"].endswith("pdt-autoidm")


def test_a_machine_refuses_a_token_with_a_grant():
    with pytest.raises(Refused, match="not one this operation takes"):
        check(worker_token())
    with pytest.raises(Refused, match="not a cloud worker"):
        check(token("deploy", {"project": "p", "app": "hr"}, grant={"place": "x"}))


@pytest.mark.parametrize("grant", [
    None, {"place": "aws:1:r"}, {"place": "bad", "role": "r"}, {"place": "aws:1:r", "role": "a b"}])
def test_a_worker_refuses_a_token_with_no_valid_grant(grant):
    args = {"project": "p", "app": "hr", "snapshot_id": "a" * 64}
    with pytest.raises(Refused, match="no valid grant"):
        check(token("deploy", args, grant=grant), worker=True)


@pytest.mark.parametrize("operation, args", [
    ("run_snapshot", {"project": "p", "snapshot_id": "a" * 64, "app": "hr"}),
    ("sync_up", {"project": "p", "snapshot_id": "a" * 64}),
    ("sync_down", {"project": "p", "snapshot_id": "a" * 64}),
    ("logs", {"project": "p", "app": "hr", "snapshot_id": "a" * 64}),
])
def test_a_worker_does_not_run_customer_code_copy_files_or_open_logs(operation, args):
    grant = {"place": "aws:1:r", "role": "r"}
    with pytest.raises(Refused, match="does not run"):
        check(token(operation, args, grant=grant), worker=True)


def test_a_worker_runs_only_the_deployed_job():
    grant = {"place": "aws:1:r", "role": "r"}
    args = {"project": "p", "app": "hr", "deployed": False, "snapshot_id": "a" * 64}
    with pytest.raises(Refused, match="only the deployed job"):
        check(token("run", args, grant=grant), worker=True)


def write_presets(root: Path, text: str) -> None:
    (root / config.PROJECT_FILE).write_text(f"service:\n  presets:\n{text}", encoding="utf-8")


@pytest.mark.parametrize("name", [
    "PATH", "Path", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
    "UV_INDEX_URL", "PDT_INSTALLATION_KEY", "PDT_INSTALLATION_ID", "pythonpath"])
def test_the_preset_refusal_list(tmp_path, name):
    write_presets(tmp_path, f"    bad:\n      env:\n        {name}: x\n")
    with pytest.raises(Refused, match="may not set"):
        load_preset(tmp_path, "bad")


def test_a_preset_gives_fixed_values_and_says_whether_it_sends_artifacts(tmp_path):
    write_presets(tmp_path, "    rebuild:\n      env:\n        PDT_STEPS: extract transform\n"
                            "        COUNT: 5\n      artifacts: true\n")
    assert load_preset(tmp_path, "rebuild") == {
        "env": {"PDT_STEPS": "extract transform", "COUNT": "5"}, "artifacts": True}


@pytest.mark.parametrize("text, says", [
    ("    other:\n      artifacts: true\n", "no service preset named wanted"),
    ("    wanted:\n      artifacts: maybe\n", "must hold only"),
    ("    wanted:\n      command: whoami\n", "must hold only"),
    ("    wanted:\n      env:\n        A: [1, 2]\n", "other than one value"),
    ("    wanted:\n      env:\n        A-B: x\n", "not an env var name"),
])
def test_a_preset_that_breaks_a_rule_is_refused(tmp_path, text, says):
    write_presets(tmp_path, text)
    with pytest.raises(Refused, match=says):
        load_preset(tmp_path, "wanted")


def test_a_project_with_no_service_section_has_no_preset(tmp_path):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n", encoding="utf-8")
    with pytest.raises(Refused, match="no service preset"):
        load_preset(tmp_path, "wanted")


def test_validation_takes_a_service_section():
    assert "service" in config.ROOT_KEYS


def test_the_private_key_is_not_a_pinned_key():
    assert all(PRIVATE_PEM.decode() != key["pem"] for key in KEYS)
