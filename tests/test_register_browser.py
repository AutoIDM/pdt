import json

import pytest
from service_fixtures import CUSTOMER, INSTALLATION, KEYS, FakePortal

from pdt import config, grants, register_cli
from pdt.utils import autoidm

CODES = {"user_code": "KQ7M-2RTX", "device_code": "a" * 32, "interval": 1, "expires_in": 900}


@pytest.fixture
def portal(tmp_path, monkeypatch):
    for name in (*autoidm.SECRET_NAMES, "PDT_ENV_JSON", "PDT_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(register_cli.time, "sleep", lambda _seconds: None)
    found = FakePortal()
    found.opened = []
    monkeypatch.setattr(register_cli.webbrowser, "open", found.opened.append)
    found.routes["/v1/device/start"] = lambda _body: (200, {**CODES, "url": f"{found.url}/connect?code=KQ7M-2RTX"})
    yield found
    found.close()


APPROVED = {"state": "approved", "installation_id": INSTALLATION, "customer_id": CUSTOMER,
            "email": "admin@example.com", "command_keys": KEYS, "grants": []}


def poll_answers(portal, *answers):
    remaining = list(answers)
    portal.routes["/v1/device/poll"] = lambda _body: remaining.pop(0) if len(remaining) > 1 else remaining[0]


def test_the_browser_flow_prints_the_code_waits_and_saves_the_keys(portal, capsys, monkeypatch):
    poll_answers(portal, (200, {"state": "waiting"}), (200, APPROVED))
    keys_at_start = []
    route = portal.routes["/v1/device/start"]

    def start(body):
        keys_at_start.extend(path.name for path in autoidm.folder().glob("*.key"))
        return route(body)

    portal.routes["/v1/device/start"] = start
    assert register_cli.register(None, portal.url + "/") == 0
    out = capsys.readouterr().out
    assert f"{portal.url}/connect?code=KQ7M-2RTX" in out
    assert "KQ7M-2RTX" in out and "admin@example.com" in out
    assert portal.opened == [f"{portal.url}/connect?code=KQ7M-2RTX"]
    assert keys_at_start == [register_cli.DEVICE_KEY]
    start_body = portal.bodies("/v1/device/start")[0]
    assert set(start_body) == {"public_key", "hostname", "pdt_version", "platform", "places",
                               "projects", "kind", "ci"}
    assert (start_body["kind"], start_body["ci"]) == ("machine", None)
    info, key = autoidm.files(CUSTOMER)
    assert json.loads(info.read_text()) == {
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "api_url": portal.url,
        "command_keys": KEYS, "command_keys_version": 1}
    assert sorted(path.name for path in autoidm.folder().glob("*.key")) == [key.name]
    assert autoidm.public_key(autoidm.load_key(key.read_bytes())) == start_body["public_key"]
    assert portal.seen[-1][2].startswith("Bearer ")
    assert portal.bodies("/v1/device/confirm") == [{"version": 1, "user_code": "KQ7M-2RTX"}]


def test_a_retry_after_a_failed_start_sends_the_same_public_key(portal):
    route = portal.routes["/v1/device/start"]
    portal.routes["/v1/device/start"] = lambda _body: (500, {"error": "boom"})
    assert register_cli.register(None, portal.url) == 1
    portal.routes["/v1/device/start"] = route
    poll_answers(portal, (200, APPROVED))
    assert register_cli.register(None, portal.url) == 0
    bodies = portal.bodies("/v1/device/start")
    assert bodies[0]["public_key"] == bodies[1]["public_key"]


def test_confirm_comes_only_after_poll_says_approved(portal):
    answers = [(200, {"state": "waiting"}), (200, {"state": "waiting"}), (200, APPROVED)]
    order = []

    def poll(_body):
        order.append("poll")
        return answers.pop(0)

    def confirm(_body):
        order.append("confirm")
        return 200, {}

    portal.routes["/v1/device/poll"] = poll
    portal.routes["/v1/device/confirm"] = confirm
    assert register_cli.register(None, portal.url) == 0
    assert order == ["poll", "poll", "poll", "confirm"]


def test_a_denied_request_registers_nothing(portal, capsys):
    poll_answers(portal, (200, {"state": "denied"}))
    assert register_cli.register(None, portal.url) == 1
    assert "denied" in capsys.readouterr().out
    assert not autoidm.files(CUSTOMER)[0].exists()


def test_an_expired_code_says_to_run_again(portal, capsys):
    poll_answers(portal, (404, {"error": "unknown_code"}))
    assert register_cli.register(None, portal.url) == 1
    assert "expired" in capsys.readouterr().out


def test_a_poll_that_gets_a_server_error_tries_again(portal):
    poll_answers(portal, (503, {"error": "busy"}), (200, APPROVED))
    assert register_cli.register(None, portal.url) == 0


def test_a_bad_customer_id_saves_nothing(portal, capsys):
    poll_answers(portal, (200, {**APPROVED, "customer_id": "../evil"}))
    assert register_cli.register(None, portal.url) == 1
    assert not list(autoidm.folder().glob("cus_*"))


def test_an_answer_with_a_page_that_is_not_https_opens_nothing(portal, capsys):
    portal.routes["/v1/device/start"] = lambda _body: (200, {**CODES, "url": "http://portal.example/connect"})
    assert register_cli.register(None, portal.url) == 1
    assert portal.opened == []
    assert "not https" in capsys.readouterr().out


def test_each_place_chosen_in_the_browser_is_granted(portal, monkeypatch):
    place = "aws:123456789012:us-east-2"
    poll_answers(portal, (200, {**APPROVED, "grants": [place]}))
    added = []
    monkeypatch.setattr(register_cli, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(grants, "add", lambda *args: added.append(args) or 0)
    assert register_cli.register(None, portal.url) == 0
    assert added[0][:3] == (place, False, False)
    assert added[0][3].installation_id == INSTALLATION


def test_a_person_with_no_places_chosen_gets_the_question(portal, monkeypatch):
    place = "aws:123456789012:us-east-2"
    poll_answers(portal, (200, APPROVED))
    added = []
    monkeypatch.setattr(register_cli, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(grants, "places_of_project", lambda: [place])
    monkeypatch.setattr(grants, "add", lambda *args: added.append(args) or 0)
    assert register_cli.register(None, portal.url) == 0
    assert added[0][:3] == (place, False, False)


def test_yes_prints_the_grant_command_and_grants_nothing(portal, monkeypatch, capsys):
    place = "aws:123456789012:us-east-2"
    poll_answers(portal, (200, APPROVED))
    monkeypatch.setattr(register_cli, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(grants, "places_of_project", lambda: [place])
    monkeypatch.setattr(grants, "add", lambda *_a: pytest.fail("made a grant"))
    assert register_cli.register(None, portal.url, True) == 0
    assert f"pdt grant add {place}" in capsys.readouterr().out


def test_no_one_at_a_terminal_gets_no_question_and_no_grant(portal, monkeypatch):
    poll_answers(portal, (200, APPROVED))
    monkeypatch.setattr(register_cli, "can_prompt", lambda _interactive: False)
    monkeypatch.setattr(grants, "places_of_project", lambda: ["aws:123456789012:us-east-2"])
    monkeypatch.setattr(grants, "add", lambda *_a: pytest.fail("made a grant"))
    assert register_cli.register(None, portal.url) == 0


def test_the_code_flow_also_saves_the_command_keys(portal):
    portal.routes["/v1/register"] = lambda _body: (200, {
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "command_keys": KEYS})
    assert register_cli.register("KQ7M-2RTX", portal.url) == 0
    assert json.loads(autoidm.files(CUSTOMER)[0].read_text())["command_keys"] == KEYS


def test_a_retry_of_the_same_registration_adds_the_keys(portal):
    answer = {"installation_id": INSTALLATION, "customer_id": CUSTOMER}
    portal.routes["/v1/register"] = lambda _body: (200, answer)
    assert register_cli.register("KQ7M-2RTX", portal.url) == 0
    key = autoidm.load_key(autoidm.files(CUSTOMER)[1].read_bytes())
    pending = autoidm.folder() / "pending-x.key"
    register_cli.write_key(pending, key)
    assert register_cli.save_registration({**answer, "command_keys": KEYS}, key, portal.url, pending) == 0
    assert json.loads(autoidm.files(CUSTOMER)[0].read_text())["command_keys"] == KEYS


def test_a_registration_of_another_customer_than_the_service_is_revoked(portal, capsys):
    from pdt import service
    service.write_settings({"version": 1, "customer": "cus_0000000000", "projects": None,
                            "named_projects": [], "backend": None})
    poll_answers(portal, (200, APPROVED))
    assert register_cli.register(None, portal.url) == 1
    assert len(portal.bodies("/v1/service/remove")) == 1
    assert not autoidm.files(CUSTOMER)[0].exists() and not autoidm.files(CUSTOMER)[1].exists()
    out = " ".join(capsys.readouterr().out.split())
    assert "One computer serves one customer" in out


def test_the_project_gets_the_customer(portal, tmp_path, monkeypatch):
    project = tmp_path / "example"
    project.mkdir()
    (project / config.PROJECT_FILE).write_text("apps: []\n")
    monkeypatch.chdir(project)
    poll_answers(portal, (200, APPROVED))
    assert register_cli.register(None, portal.url) == 0
    assert CUSTOMER in (project / config.PROJECT_FILE).read_text()
    assert portal.bodies("/v1/device/start")[0]["projects"] == ["example"]
