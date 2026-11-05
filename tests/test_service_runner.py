import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from service_fixtures import (
    COMMAND, CUSTOMER, INSTALLATION, KEYS, FakePortal, claims, copy_of, token)

from pdt import config, register_cli, service, service_commands, service_runner
from pdt.service_commands import Clock
from pdt.service_runner import Job, Pool, execute
from pdt.utils import autoidm

SECOND = "cmd_20261103T120001Z_0123abcd"


@pytest.fixture
def portal():
    found = FakePortal()
    yield found
    found.close()


@pytest.fixture
def machine(tmp_path, monkeypatch, portal):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    for name in (*autoidm.SECRET_NAMES, "PDT_ENV_JSON", "PDT_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    folder = autoidm.folder()
    folder.mkdir(parents=True)
    register_cli.write_key(folder / f"{CUSTOMER}.key", Ed25519PrivateKey.generate())
    (folder / f"{CUSTOMER}.json").write_text(json.dumps({
        "installation_id": INSTALLATION, "customer_id": CUSTOMER, "api_url": portal.url,
        "command_keys": KEYS}))
    home = tmp_path / "projects"
    root = home / "example"
    (root / "hr").mkdir(parents=True)
    (root / "pdt.yml").write_text(f"platform:\n  provider: windows\nmanaged_services:\n"
                                  f"  customer: {CUSTOMER}\n")
    (root / "hr" / "config.yml").write_text("schedule: daily\n")
    (root / "hr" / "run.py").write_text("print('hi')\n")
    (root / ".env").write_text("FROM_PROJECT=project-value\nBOTH=project-value\n")
    (root / "hr" / ".env").write_text("BOTH=app-value\n")
    service_runner.service_folder().mkdir(parents=True)
    settings = {"version": 1, "customer": CUSTOMER, "projects": str(home),
                "named_projects": [], "backend": "cron", "pdt_version": None, "python": None,
                "proxy": {}}
    service.write_settings(settings)
    monkeypatch.setattr(service_runner, "OUTPUT_SECONDS", 0.2)
    monkeypatch.setattr(service_runner, "STOP_SECONDS", 1)
    monkeypatch.setattr(service, "LOOP_SECONDS", 0.05)
    TICKS.clear()
    return root


class NoConnection:
    def read(self):
        return False, 0, None

    def clear_wanted(self):
        pass

    def start(self, _url):
        pass

    def stop(self):
        pass


TICKS: dict = {}


def tick() -> int:
    if "served" not in TICKS:
        TICKS["served"] = service.machine_served(settings())
        TICKS["served"].connection = NoConnection()
        TICKS["pool"] = Pool()
    found, pool = TICKS["served"], TICKS["pool"]
    state = service.read_state()
    service.check(found, settings(), pool, state)
    service.write_state(state)
    deadline = time.monotonic() + 20
    while (pool.waiting or pool.running) and time.monotonic() < deadline:
        pool.start_ready()
        pool.reap()
        time.sleep(0.05)
    return 0


def due() -> None:
    TICKS["served"].schedule.wanted = True
    TICKS["served"].schedule.last_checkin = 0


def registration():
    return autoidm.machine_registration(CUSTOMER)


def child(monkeypatch, code: str):
    monkeypatch.setattr(service_commands, "command_line",
                        lambda _operation, _args, _python=None: [sys.executable, "-c", code])


def journal(command_id=COMMAND, **fields):
    service_runner.journal_write({"schema": 1, "command_id": command_id, "state": "claimed",
                                  "claim_id": "0" * 32, "pid": None, "process_created": None,
                                  "output_seq": 0, "output_offset": 0, "result": None,
                                  "token": token("run", {"project": "example", "app": "hr",
                                                         "deployed": False}, command_id),
                                  **fields})


def job(root, operation="run", args=None, limit=300, command_id=COMMAND):
    args = args or {"project": "example", "app": "hr", "deployed": False}
    found = claims(operation, args, command_id, limit_seconds=limit)
    journal(command_id)
    return Job(command_id, found, registration(), root)


def run_in_thread(item: Job) -> threading.Thread:
    thread = threading.Thread(target=execute, args=(item,), daemon=True)
    thread.start()
    return thread


def test_a_child_gets_a_list_of_arguments_and_no_shell(machine, portal, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def popen(args, **kwargs):
        seen.setdefault("args", args)
        seen.setdefault("kwargs", kwargs)
        return real([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(service_runner.subprocess, "Popen", popen)
    execute(job(machine))
    assert seen["args"] == [sys.executable, "-m", "pdt.cli", "run", "hr"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert seen["kwargs"]["cwd"] == machine
    assert seen["kwargs"]["env"]["PDT_PROJECT"] == str(machine)


def test_a_command_reports_started_output_and_result(machine, portal, monkeypatch):
    child(monkeypatch, "print('first'); print('second')")
    execute(job(machine))
    assert portal.facts() == ["started", "output", "result"]
    assert "first\nsecond" in portal.output()
    result = portal.result()
    assert (result["outcome"], result["exit_code"], result["run_id"]) == ("done", 0, COMMAND)
    entry = service_runner.journal_read(COMMAND)
    assert entry["result"]["outcome"] == "done" and entry["reported"] is True


def test_a_failed_command_reports_failed(machine, portal, monkeypatch):
    child(monkeypatch, "import sys; sys.exit(3)")
    execute(job(machine))
    result = portal.result()
    assert (result["outcome"], result["exit_code"]) == ("failed", 3)


def test_output_goes_up_while_the_command_runs(machine, portal, monkeypatch):
    child(monkeypatch, "import time\nprint('early', flush=True)\ntime.sleep(1.2)\nprint('late')")
    thread = run_in_thread(job(machine))
    deadline = time.monotonic() + 5
    while "early" not in portal.output() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "early" in portal.output()
    assert "result" not in portal.facts()
    thread.join(10)
    assert "late" in portal.output()
    seqs = [body["seq"] for body in portal.bodies("/v1/commands/report") if body["fact"] == "output"]
    assert seqs == sorted(seqs) and len(seqs) >= 2


def process_runs_soon_stops(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not service_runner.process_runs(pid):
            return True
        time.sleep(0.05)
    return False


TREE = ("import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n")


def test_cancel_stops_the_whole_process_tree(machine, portal, monkeypatch):
    child(monkeypatch, TREE)
    item = job(machine)
    thread = run_in_thread(item)
    deadline = time.monotonic() + 10
    printed = service_runner.output_path(COMMAND)
    while not (printed.is_file() and printed.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(printed.read_text().strip())
    item.cancel.set()
    thread.join(15)
    assert portal.result()["outcome"] == "cancelled"
    assert process_runs_soon_stops(grandchild)


def test_the_run_limit_stops_the_whole_process_tree(machine, portal, monkeypatch):
    child(monkeypatch, TREE)
    item = job(machine, limit=1)
    thread = run_in_thread(item)
    thread.join(15)
    result = portal.result()
    assert result["outcome"] == "failed" and "run limit" in result["message"]
    assert process_runs_soon_stops(int(portal.output().strip()))


def test_stop_tree_on_windows_uses_taskkill(monkeypatch):
    calls = []
    monkeypatch.setattr(service_runner.os, "name", "nt")
    monkeypatch.setattr(service_runner.subprocess, "run", lambda args, **_kwargs: calls.append(args))
    service_runner.stop_tree(1234, lambda: True)
    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]


def test_a_local_run_sends_its_output_folder_as_an_artifact(machine, portal, monkeypatch):
    code = ("import os, pathlib\n"
            "print('extract')\n"
            "folder = pathlib.Path(os.environ['PDT_OUTPUT_DIR'])\n"
            "folder.mkdir(parents=True, exist_ok=True)\n"
            "(folder / 'report.txt').write_text('made it')\n")
    child(monkeypatch, code)
    portal.routes["/v1/uploads"] = lambda _body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": "uploads/a.zip"}, "key": "uploads/a.zip"})
    execute(job(machine))
    assert "extract" in portal.output()
    result = portal.result()
    assert result["outcome"] == "done" and result["artifact_key"] == "uploads/a.zip", result["message"]
    upload = next(body for body in portal.bodies("/v1/uploads") if body["kind"] == "artifact")
    assert (upload["kind"], upload["run_id"]) == ("artifact", COMMAND)
    assert upload["filename"] == service_runner.ARTIFACT_NAME
    assert len(portal.uploads) == 2


def test_the_env_line_of_an_mr_run_states_where_the_values_came_from(tmp_path, monkeypatch):
    monkeypatch.setenv(config.SERVICE_ENV_FILES_ENV, str(tmp_path / ".env"))
    line = " ".join(config.env_file_lines(tmp_path))
    assert "the pdt service passed the values" in line and str(tmp_path / ".env") in line
    assert "terminal" not in line
    monkeypatch.setenv(config.SERVICE_ENV_FILES_ENV, "")
    line = " ".join(config.env_file_lines(tmp_path))
    assert "the pdt service started this command" in line and "terminal" not in line


def test_a_missing_var_in_an_mr_run_names_the_served_project(tmp_path, monkeypatch):
    monkeypatch.setenv(config.SERVICE_ENV_FILES_ENV, "")
    monkeypatch.delenv("NEEDED", raising=False)
    message = config.missing_env({"dir": tmp_path, "name": "hr", "config": {},
                                  "env": {"required": ["NEEDED"]}})
    assert "project that the service serves" in message
    assert str(tmp_path) not in message


def test_the_pool_runs_at_most_four_commands_and_one_for_each_app(machine, portal, monkeypatch):
    pool = Pool()
    started = threading.Event()
    release = threading.Event()

    def hold(_job):
        started.set()
        release.wait(10)

    monkeypatch.setattr(service_runner, "execute", hold)
    ids = [f"cmd_20261103T12000{index}Z_0123abcd" for index in range(7)]
    apps = ["a", "a", "b", "c", "d", "e", "f"]
    for command_id, app in zip(ids, apps):
        pool.add(job(machine, args={"project": "example", "app": app, "deployed": False},
                     command_id=command_id))
    pool.start_ready()
    assert sorted(pool.running) == sorted([ids[0], ids[2], ids[3], ids[4]])
    assert [item.command_id for item in pool.waiting] == [ids[1], ids[5], ids[6]]
    release.set()
    for item in list(pool.running.values()):
        item.thread.join(5)
    pool.start_ready()
    assert sorted(pool.running)[0] == ids[1]
    assert len(pool.running) == 3
    release.set()
    for item in list(pool.running.values()):
        item.thread.join(5)


def test_cancel_of_a_waiting_command_reports_cancelled(machine, portal):
    pool = Pool()
    pool.add(job(machine))
    pool.cancel(COMMAND)
    assert pool.waiting == []
    assert portal.result()["outcome"] == "cancelled"


def test_restart_reports_interrupted_when_the_process_is_gone(machine, portal, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    journal(pid=proc.pid, phase="running", operation="run")
    pool = Pool()
    service_runner.recover(pool, registration(), Clock(), lambda name: machine)
    assert portal.result()["outcome"] == "interrupted"
    assert service_runner.journal_read(COMMAND)["reported"] is True
    assert pool.ids() == []


def test_restart_leaves_a_command_whose_process_runs_and_cancel_can_stop_it(machine, portal):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    try:
        journal(pid=proc.pid, state="started",
                process_created=service_runner.process_created(proc.pid))
        pool = Pool()
        service_runner.recover(pool, registration(), Clock(), lambda name: machine)
        assert portal.bodies("/v1/commands/report") == []
        assert pool.ids() == [COMMAND]
        reaper = threading.Thread(target=proc.wait, daemon=True)
        reaper.start()
        pool.cancel(COMMAND)
        reaper.join(5)
        assert proc.poll() is not None
        pool.running[COMMAND].thread.join(10)
        assert portal.result()["outcome"] == "cancelled"
        pool.reap()
        assert pool.ids() == []
    finally:
        proc.kill()


def test_restart_sends_a_result_that_autoidm_did_not_get(machine, portal):
    result = {"outcome": "done", "exit_code": 0, "message": None, "run_id": None, "artifact_key": None}
    journal(result=result, reported=False, phase="finished")
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert portal.result()["outcome"] == "done"
    assert service_runner.journal_read(COMMAND)["reported"] is True


def test_autoidm_holding_the_result_counts_as_sent(machine, portal):
    portal.routes["/v1/commands/report"] = lambda _body: (409, {"error": "already_finished"})
    result = {"outcome": "done", "exit_code": 0, "message": None, "run_id": None, "artifact_key": None}
    journal(result=result, reported=False, phase="finished")
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert service_runner.journal_read(COMMAND)["reported"] is True


def served() -> service.Served:
    return service.Served(CUSTOMER, registration(), KEYS)


def settings():
    return service.read_settings()


def accept(pool, signed, command_id=COMMAND):
    return service.accept({"command_id": command_id, "token": signed}, served(), settings(), pool)


def test_accept_claims_and_queues_a_good_command(machine, portal):
    pool = Pool()
    assert accept(pool, token())
    assert portal.facts() == ["claimed"]
    assert [item.command_id for item in pool.waiting] == [COMMAND]
    assert pool.waiting[0].project == machine
    assert service_runner.journal_read(COMMAND)["state"] == "claimed"


def test_a_repeated_id_is_refused_and_starts_nothing(machine, portal):
    pool = Pool()
    assert accept(pool, token())
    pool.waiting.clear()
    assert accept(pool, token())
    assert pool.waiting == []
    assert portal.facts() == ["claimed"]


def test_a_refused_command_reports_refused_with_a_plain_message_and_no_claim(machine, portal):
    pool = Pool()
    assert accept(pool, token("list", {"project": "example", "command": "whoami"}))
    assert pool.waiting == []
    assert portal.facts() == ["result"]
    result = portal.result()
    assert result["outcome"] == "refused"
    assert "not one this operation takes" in result["message"]


def test_a_refused_id_is_refused_once(machine, portal):
    signed = token("list", {"project": "other"})
    accept(Pool(), signed)
    accept(Pool(), signed)
    assert portal.facts() == ["result"]


def test_a_command_for_a_project_of_another_customer_is_refused(machine, portal):
    (machine / "pdt.yml").write_text("managed_services:\n  customer: cus_0000000000\n")
    pool = Pool()
    assert accept(pool, token())
    assert pool.waiting == []
    assert "cus_0000000000" in portal.result()["message"]


def test_a_project_with_no_customer_is_refused_with_the_fix(machine, portal):
    (machine / "pdt.yml").write_text("apps: []\n")
    assert accept(Pool(), token())
    assert "pdt register" in portal.result()["message"]


def test_sync_down_can_add_the_customer_to_a_project_that_has_none(machine, portal, monkeypatch):
    (machine / "pdt.yml").write_text("apps: []\n")
    pool = Pool()
    assert accept(pool, token("sync_down", {"project": "example"}))
    item = pool.waiting[0]
    assert item.project == machine
    prepared = service_runner.prepare(item, dict(os.environ))
    assert prepared.cwd == machine
    assert prepared.env[service_runner.SERVICE_CUSTOMER_ENV] == CUSTOMER


def test_a_staff_install_of_a_new_project_runs_for_real_and_is_served(machine, portal, monkeypatch):
    home = machine.parent
    monkeypatch.setenv(service_runner.PROJECTS_HOME_ENV, str(home))
    portal.serve_copies(copy_of("root", {"pdt.yml": b"apps: []\n"}),
                        copy_of("app=hr", {"hr/config.yml": b"schedule: daily\n",
                                           "hr/run.py": b"print('x')\n"}))
    answers = [checkin_answer(portal, [{"command_id": COMMAND, "token": token(
        "sync_down", {"project": "third-project"})}])]
    portal.routes["/v1/service/checkin"] = lambda _body: answers.pop(0) if answers else checkin_answer(portal)
    assert tick() == 0
    assert portal.result()["outcome"] == "done", portal.output()
    assert service.project_customer(home / "third-project") == CUSTOMER
    due()
    assert tick() == 0
    names = [item["name"] for item in portal.bodies("/v1/service/checkin")[-1]["projects"]]
    assert "third-project" in names


def test_a_command_for_a_project_not_served_is_refused(machine, portal):
    assert accept(Pool(), token("list", {"project": "other"}))
    assert "does not serve" in portal.result()["message"]


def test_a_lost_claim_keeps_the_cursor_and_a_retry_sends_the_same_claim_id(machine, portal):
    portal.routes["/v1/commands/report"] = lambda _body: (500, {"error": "boom"})
    pool = Pool()
    assert accept(pool, token()) is False
    assert pool.waiting == []
    kept = service_runner.journal_read(COMMAND)["claim_id"]
    assert len(kept) == 32 and int(kept, 16) >= 0
    portal.routes.pop("/v1/commands/report")
    assert accept(pool, token())
    assert [item.command_id for item in pool.waiting] == [COMMAND]
    assert [body["claim_id"] for body in portal.bodies("/v1/commands/report")] == [kept, kept]
    assert service_runner.journal_read(COMMAND)["claim_id"] == kept


def test_a_doorbell_with_no_object_yet_means_no_change(machine, portal):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    assert tick() == 0
    portal.doorbell_status = 404
    before = len(portal.bodies("/v1/service/checkin"))
    assert tick() == 0
    assert len(portal.bodies("/v1/service/checkin")) == before


def test_the_newest_doorbell_url_replaces_the_last(machine, portal):
    answers = [checkin_answer(portal, doorbell_url=f"{portal.url}/doorbell?v=1")]
    portal.routes["/v1/service/checkin"] = lambda _body: answers.pop(0) if answers else checkin_answer(portal)
    assert tick() == 0
    assert service.read_state()["doorbell_url"].endswith("v=1")
    due()
    assert tick() == 0
    assert service.read_state()["doorbell_url"] == f"{portal.url}/doorbell"


def test_a_check_in_lists_at_most_100_running_ids_with_the_running_ones_first(machine, portal):
    pool = Pool()
    for index in range(120):
        pool.waiting.append(job(machine, command_id=f"cmd_20261103T12{index:04d}Z_0123abcd"))
    pool.running[SECOND] = pool.waiting[0]
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    service.check_in(served(), settings(), pool, {})
    running = portal.bodies("/v1/service/checkin")[0]["running"]
    assert len(running) == 100
    assert running[0] == SECOND


def test_output_of_escaped_text_is_split_with_no_loss(machine, portal, monkeypatch):
    item = job(machine)
    text = "é€" * 60000 + "\x01" * 40000 + "end"
    service_runner.output_path(COMMAND).parent.mkdir(parents=True, exist_ok=True)
    service_runner.output_path(COMMAND).write_bytes(text.encode("utf-8"))
    service_runner.send_all_output(item)
    bodies = [body for body in portal.bodies("/v1/commands/report") if body["fact"] == "output"]
    assert len(bodies) > 1
    assert all(len(json.dumps(body)) < 256 * 1024 for body in bodies)
    assert "".join(body["text"] for body in bodies) == text
    assert [body["seq"] for body in bodies] == list(range(len(bodies)))


def test_a_character_that_a_chunk_boundary_would_cut_stays_whole(monkeypatch):
    monkeypatch.setattr(service_runner, "OUTPUT_LIMIT", 100)
    text, used = service_runner.next_chunk(b"a" * 98 + "€".encode())
    assert used == 98
    assert text == "a" * 98


def test_a_lone_partial_character_is_still_consumed():
    text, used = service_runner.next_chunk("é".encode()[:1])
    assert used == 1 and text


def test_a_long_message_stays_under_the_limit(machine, portal):
    result = {"outcome": "failed", "exit_code": 1, "message": "x" * 9000, "run_id": None,
              "artifact_key": None}
    journal(result=result, reported=False, phase="finished")
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert len(portal.result()["message"]) == 4096


@pytest.mark.parametrize("status, stops", [(400, True), (404, True), (409, True), (401, False),
                                           (429, False),
                                           (500, False)])
def test_a_result_autoidm_refuses_for_good_stops_the_retries(machine, portal, monkeypatch, status,
                                                             stops):
    monkeypatch.setattr(autoidm, "RETRY_LIMIT_SECONDS", 0)
    portal.routes["/v1/commands/report"] = lambda _body: (status, {"error": "x"})
    result = {"outcome": "done", "exit_code": 0, "message": None, "run_id": None, "artifact_key": None}
    journal(result=result, reported=False, phase="finished")
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert service_runner.journal_read(COMMAND)["reported"] is stops


def test_a_claim_another_writer_won_starts_nothing(machine, portal):
    portal.routes["/v1/commands/report"] = lambda _body: (409, {"error": "already_claimed"})
    pool = Pool()
    assert accept(pool, token())
    assert pool.waiting == []


@pytest.mark.parametrize("command_id", ["../../evil", "x", None, 5])
def test_a_command_id_that_could_name_a_path_writes_no_journal_file(machine, portal, command_id, tmp_path):
    assert service.accept({"command_id": command_id, "token": token()}, served(), settings(), Pool())
    assert portal.bodies("/v1/commands/report") == []
    assert not (service_runner.service_folder().parent / "evil.json").exists()


def checkin_answer(portal, commands=(), cancels=(), **changes):
    return (200, {"doorbell_url": f"{portal.url}/doorbell", "poll_seconds": 30,
                  "checkin_seconds": 900, "min_checkin_seconds": 0, "commands": list(commands),
                  "cancels": list(cancels), **changes})


def test_a_403_from_the_doorbell_checks_in(machine, portal):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    assert tick() == 0
    portal.doorbell_status = 403
    before = len(portal.bodies("/v1/service/checkin"))
    assert tick() == 0
    assert len(portal.bodies("/v1/service/checkin")) == before + 1


def test_a_full_answer_checks_in_again(machine, portal):
    answers = [checkin_answer(portal, [{"command_id": f"cmd_20261103T1200{index:02d}Z_0123abcd",
                                        "token": "bad"} for index in range(20)])]
    portal.routes["/v1/service/checkin"] = lambda _body: answers.pop(0) if answers else checkin_answer(portal)
    assert tick() == 0
    assert tick() == 0
    assert len(portal.bodies("/v1/service/checkin")) >= 2


def test_an_app_folder_with_a_bad_name_stays_out_and_delivery_goes_on(machine, portal, capsys):
    bad = machine / "My App"
    bad.mkdir()
    (bad / "config.yml").write_text("schedule: daily\n")
    (bad / "run.py").write_text("print('hi')\n")
    dash = machine / "-dash"
    dash.mkdir()
    (dash / "run.py").write_text("print('hi')\n")
    answers = [checkin_answer(portal, [{"command_id": COMMAND, "token": token()}])]

    def answer(body):
        for project in body["projects"]:
            for name in project["apps"]:
                if not service_commands.NAME.fullmatch(name):
                    return 400, {"error": "bad_request"}
        return answers.pop(0) if answers else checkin_answer(portal)

    portal.routes["/v1/service/checkin"] = answer
    assert tick() == 0
    assert portal.result()["outcome"] == "done"
    assert portal.bodies("/v1/service/checkin")[0]["projects"][0]["apps"] == ["hr"]
    log = (service_runner.service_folder() / "service.log").read_text()
    assert log.count("My App") == 1
    assert "-dash" in log
    assert "My App" in capsys.readouterr().out


def test_a_bad_name_warns_one_time_at_each_start(machine, portal):
    bad = machine / "My App"
    bad.mkdir()
    (bad / "run.py").write_text("print('hi')\n")
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    item = served()
    pool = Pool()
    service._said_once.clear()
    service.check_in(item, settings(), pool, {})
    service.check_in(item, settings(), pool, {})
    assert (service_runner.service_folder() / "service.log").read_text().count("My App") == 1
    service._said_once.clear()
    assert tick() == 0
    assert (service_runner.service_folder() / "service.log").read_text().count("My App") == 2


def test_a_project_folder_with_a_leading_dash_stays_out(machine, portal):
    dash = machine.parent / "-dash"
    dash.mkdir()
    (dash / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")
    assert [item["name"] for item in service.checkin_projects(settings(), CUSTOMER)] == ["example"]


def test_a_cancel_in_a_check_in_stops_the_running_command(machine, portal, monkeypatch):
    child(monkeypatch, TREE)
    calls = []

    def answer(body):
        calls.append(body)
        portal.etag = f'"{len(calls)}"'
        if len(calls) == 1:
            return checkin_answer(portal, [{"command_id": COMMAND, "token": token(
                "run", {"project": "example", "app": "hr", "deployed": False})}], poll_seconds=0.1)
        running = body["running"]
        return checkin_answer(portal, cancels=[
            {"command_id": command_id, "token": token("cancel", command_id=command_id)}
            for command_id in running], poll_seconds=0.1)

    portal.routes["/v1/service/checkin"] = answer
    item, pool = served(), Pool()
    service.check_in(item, settings(), pool, {})
    pool.start_ready()
    printed = service_runner.output_path(COMMAND)
    deadline = time.monotonic() + 10
    while not (printed.is_file() and printed.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    service.check_in(item, settings(), pool, {})
    pool.running[COMMAND].thread.join(15)
    assert portal.result()["outcome"] == "cancelled"


class Steps(threading.Event):

    def __init__(self, steps):
        super().__init__()
        self.remaining = list(steps)

    def wait(self, timeout=None):
        if self.remaining:
            self.remaining.pop(0)()
        else:
            self.set()
        return self.is_set()


def run_loop(steps):
    return service.serve(settings(), Steps(steps))


def test_a_failed_check_in_after_a_change_stays_due(machine, portal, monkeypatch):
    item = served()
    item.connection = NoConnection()
    portal.routes["/v1/service/checkin"] = lambda _body: (500, {"error": "boom"})
    service.check(item, settings(), Pool(), {})
    assert item.schedule.wanted is True and item.schedule.retry_at > time.time()
    item.schedule.retry_at = 0
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    service.check(item, settings(), Pool(), {})
    assert item.schedule.wanted is False


def test_an_install_that_changes_the_projects_checks_in_at_once(machine, portal, monkeypatch, tmp_path):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal, poll_seconds=900)
    fresh = tmp_path / "elsewhere" / "named"
    (fresh / "hr").mkdir(parents=True)
    (fresh / "hr" / "run.py").write_text("print('hi')\n")
    (fresh / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")

    def install():
        service.write_settings({**settings(), "named_projects": [str(fresh)]})

    assert run_loop([install, lambda: None]) == 0
    bodies = portal.bodies("/v1/service/checkin")
    assert [item["name"] for item in bodies[-1]["projects"]] == ["example", "named"]
    assert len(bodies) == 2


def test_the_new_projects_folder_applies_with_no_restart(machine, portal, monkeypatch, tmp_path):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal, poll_seconds=900)

    def move():
        service.write_settings({**settings(), "projects": str(tmp_path / "moved")})

    assert run_loop([move, lambda: None]) == 0
    assert os.environ[service_runner.PROJECTS_HOME_ENV] == str(tmp_path / "moved")


def test_the_service_stops_when_the_settings_are_gone(machine, portal, monkeypatch, capsys):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal, poll_seconds=900)
    steps = [lambda: service.settings_path().unlink()]
    assert run_loop(steps) == 0
    assert "settings are gone" in (service_runner.service_folder() / "service.log").read_text()


def test_a_settings_file_being_replaced_does_not_stop_the_service(machine):
    last = settings()
    service.settings_path().write_text("{ half a file")
    assert service.current_settings(last) is last


def test_one_service_runs_on_a_computer(machine, portal, capsys):
    with service.one_service() as first:
        assert first
        with service.one_service() as second:
            assert not second
        assert service.run(threading.Event()) == 1
    assert portal.seen == []


def test_start_starts_the_installed_service_through_its_manager(machine, monkeypatch, capsys):
    started, running = [], [False]
    monkeypatch.setattr(service.service_access, "is_admin", lambda: True)
    monkeypatch.setitem(service.service_backends.BACKENDS, "cron", service.service_backends.Backend(
        lambda: True, lambda _python, _folder: None, started.append, lambda: None,
        lambda: running[0]))
    assert service.start() == 0
    assert started == [None]
    assert "Started the pdt service through cron." in capsys.readouterr().out
    running[0] = True
    assert service.start() == 0
    assert started == [None]
    assert "already runs through cron" in capsys.readouterr().out
    service.write_settings({**settings(), "backend": None})
    assert service.start() == 1
    assert "no installed pdt service" in capsys.readouterr().out


def test_a_service_with_no_terminal_prints_plain_text_to_its_file(monkeypatch):
    from pdt import console
    monkeypatch.setattr(console, "_console", console._console)
    monkeypatch.setattr(console, "_errors", console._errors)
    found = io.StringIO()
    console.to_file(found)
    console.error(f"no registration: run {console.value('pdt register')}")
    console.warn("a warning")
    assert found.getvalue() == "error: no registration: run pdt register\na warning\n"


def test_status_names_the_service_as_json(machine, portal, monkeypatch, capsys):
    monkeypatch.setitem(service.service_backends.BACKENDS, "cron", service.service_backends.Backend(
        lambda: True, lambda _python, _folder: None, lambda: None, lambda: None, lambda: True))
    journal(operation="run", state="started")
    assert service.status(as_json=True) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["backend"] == "cron" and found["running"] is True and found["installed"] is True
    assert found["projects"] == ["example"]
    assert found["commands"][0]["command_id"] == COMMAND
    assert found["customer"] == CUSTOMER
    assert service.status() == 0
    out = capsys.readouterr().out
    assert "cron (running)" in out and "Last check-in" in out and COMMAND in out


def test_a_service_run_needs_only_a_registration_and_status_sees_it(machine, portal, capsys):
    service.settings_path().unlink()
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal, poll_seconds=900)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "pdt.service_main"],
                            cwd=machine, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not portal.bodies("/v1/service/checkin") and time.monotonic() < deadline:
            time.sleep(0.05)
        body = portal.bodies("/v1/service/checkin")[0]
        assert [item["name"] for item in body["projects"]] == ["example"]
        found = settings()
        assert found["backend"] is None and found["customer"] == CUSTOMER
        assert found["named_projects"] == [str(machine.resolve())]
        assert service.status() == 0
        assert f"Backend: none (running in the foreground, PID {proc.pid})" in capsys.readouterr().out
        assert service.status(as_json=True) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["backend"] == "none" and answer["running"] is True
        assert answer["pid"] == proc.pid and answer["installed"] is False
    finally:
        proc.terminate()
        proc.wait(30)
    assert proc.returncode == 0
    assert service.running_pid() is None
    assert service.status() == 0
    assert "Backend: none (not running)" in capsys.readouterr().out


def test_a_service_run_with_no_registration_says_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "none"))
    assert service.run(threading.Event()) == 1
    assert "pdt register" in capsys.readouterr().out
    assert not service.settings_path().exists()


def test_a_pid_file_of_a_process_that_ended_means_not_running(machine):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    service.pid_path().write_text(json.dumps({"pid": proc.pid}))
    assert service.running_pid() is None
    service.pid_path().write_text("not json")
    assert service.running_pid() is None
    service.pid_path().write_text(json.dumps({"pid": os.getpid()}))
    assert service.running_pid() == os.getpid()


def test_remove_of_a_foreground_service_deletes_the_settings(machine, portal, capsys,
                                                             monkeypatch):
    service.write_settings({**settings(), "backend": None})
    portal.routes["/v1/service/remove"] = lambda _body: (200, {"children": 0})
    monkeypatch.setattr(service.service_access, "is_admin", lambda: True)
    assert service.remove(True) == 0
    assert settings() is None
    assert not autoidm.files(CUSTOMER)[0].exists()
    assert service.remove(True) == 0
    assert "no pdt service" in capsys.readouterr().out


def test_status_with_no_service_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "none"))
    assert service.status() == 0
    assert "pdt service install" in capsys.readouterr().out


SECRET_VALUE = "SECRETSECRETSECRET"


def test_a_new_project_for_sync_down_goes_under_the_projects_folder(machine, portal, monkeypatch):
    monkeypatch.setenv(service_runner.PROJECTS_HOME_ENV, str(machine.parent))
    prepared = service_runner.prepare(job(None, "sync_down", {"project": "fresh"}),
                                      dict(os.environ))
    assert prepared.cwd == machine.parent / "fresh"
    assert prepared.cwd.is_dir()
    assert prepared.env[service_runner.SERVICE_PROJECT_ENV] == "fresh"
    assert prepared.env[service_runner.SERVICE_CUSTOMER_ENV] == CUSTOMER
    assert "PDT_PROJECT" not in prepared.env


def test_a_command_that_finds_no_env_file_names_the_service(machine, portal):
    for operation, args in (("run", None), ("list", {"project": "example"})):
        prepared = service_runner.prepare(job(machine, operation, args), dict(os.environ))
        assert config.SERVICE_ENV_FILES_ENV not in prepared.env
    (machine / ".env").unlink()
    (machine / "hr" / ".env").unlink()
    for operation, args in (("run", None), ("list", {"project": "example"})):
        prepared = service_runner.prepare(job(machine, operation, args), dict(os.environ))
        assert prepared.env[config.SERVICE_ENV_FILES_ENV] == ""


def test_the_service_serves_each_named_project_and_each_folder_in_the_projects_folder(
        machine, tmp_path):
    other = tmp_path / "elsewhere" / "named"
    other.mkdir(parents=True)
    (other / "pdt.yml").write_text("apps: []\n")
    (machine.parent / "not-a-project").mkdir()
    found = service.projects_of({"projects": str(machine.parent), "named_projects": [str(other)]})
    assert found == {"example": machine, "named": other}


def test_a_named_project_that_is_gone_is_left_out(tmp_path):
    assert service.projects_of({"projects": str(tmp_path), "named_projects": [str(tmp_path / "gone")]}) == {}


def test_the_projects_folder_has_a_default(machine):
    assert service.projects_folder({}) == service_runner.service_folder() / "projects"
    fresh = service.projects_folder({}) / "new-one"
    fresh.mkdir(parents=True)
    (fresh / "pdt.yml").write_text("apps: []\n")
    assert service.projects_of({"named_projects": [str(machine)]}) == {"example": machine, "new-one": fresh}


def test_a_new_project_in_the_projects_folder_is_served_with_no_restart(machine, portal):
    portal.routes["/v1/service/checkin"] = lambda _body: checkin_answer(portal)
    state = {}
    pool = Pool()
    service.check_in(served(), settings(), pool, state)
    fresh = machine.parent / "second-project"
    (fresh / "hr").mkdir(parents=True)
    (fresh / "pdt.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")
    service.check_in(served(), settings(), pool, state)
    bodies = portal.bodies("/v1/service/checkin")
    assert [item["name"] for item in bodies[0]["projects"]] == ["example"]
    assert [item["name"] for item in bodies[1]["projects"]] == ["example", "second-project"]
    assert accept(pool, token("list", {"project": "second-project"}))
    assert pool.waiting[0].project == fresh


@pytest.mark.parametrize("wanted, is_project", [("one", True), ("many", False), (None, False)])
def test_an_install_tells_a_project_from_a_projects_folder(tmp_path, monkeypatch, wanted, is_project):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "pdt.yml").write_text("apps: []\n")
    folder, named = service.choose_projects(str(tmp_path / wanted) if wanted else None, {})
    if is_project:
        assert named == [str((tmp_path / "one").resolve())]
        assert folder == str(service_runner.service_folder() / "projects")
    elif wanted:
        assert named == [] and folder == str((tmp_path / "many").resolve())
    else:
        assert named == [] and folder == str(service_runner.service_folder() / "projects")


def test_an_install_in_a_project_names_it_and_keeps_the_default_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    project = tmp_path / "here"
    project.mkdir()
    (project / "pdt.yml").write_text("apps: []\n")
    monkeypatch.chdir(project)
    folder, named = service.choose_projects(None, {})
    assert named == [str(project.resolve())]
    assert folder == str(service_runner.service_folder() / "projects")


def test_an_install_moves_settings_that_named_one_project_as_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "old-form"
    project.mkdir()
    (project / "pdt.yml").write_text("apps: []\n")
    folder, named = service.choose_projects(None, {"projects": str(project)})
    assert named == [str(project.resolve())]
    assert folder == str(service_runner.service_folder() / "projects")


def test_a_child_gets_no_registration_from_the_service_environment(tmp_path):
    env = service_runner.child_env({"PATH": "x", autoidm.KEY_ENV: "key", autoidm.ID_ENV: "id"},
                                   tmp_path, {})
    assert autoidm.KEY_ENV not in env and autoidm.ID_ENV not in env
    assert env["PDT_PROJECT"] == str(tmp_path)


def test_the_module_paths_stay_in_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path))
    assert service_runner.journal_path(COMMAND) == Path(tmp_path, "service", "journal", f"{COMMAND}.json")
    assert service.settings_path() == Path(tmp_path, "service", "settings.json")


MR_ARGS = {"project": "example", "app": "hr", "mr_iid": "12", "sha": "a" * 40,
           "app_args": "--full"}


def serve_mr(portal, files=None, commit=MR_ARGS["sha"]):
    entry, data = copy_of("mr=12", files or {
        "pdt.yml": f"managed_services:\n  customer: {CUSTOMER}\n".encode(),
        "hr/config.yml": b"schedule: daily\n", "hr/run.py": b"print('mr')\n"})
    portal.serve_copies(({**entry, "commit": commit}, data))


def test_run_mr_runs_the_copy_in_its_own_folder_with_the_project_env(machine, portal,
                                                                       monkeypatch):
    serve_mr(portal)
    code = ("import os, pathlib\n"
            "print('file', pathlib.Path('hr/run.py').read_text().strip())\n"
            "print('env', os.path.exists('.env'), os.path.exists('hr/.env'))\n"
            "print('project', os.environ['FROM_PROJECT'][::-1], os.environ['BOTH'][::-1])\n"
            "print('name', pathlib.Path('.pdt-project-name').read_text().strip())\n"
            "print('trigger', os.environ['PDT_TRIGGER'], os.environ['PDT_MR_IID'])\n"
            "print('args', os.environ['PDT_APP_ARGS'], os.environ['PDT_RUN_ID'])\n"
            "print('lock', os.environ.get('PDT_LOCK_PROJECT'))\n"
            "folder = pathlib.Path(os.environ['PDT_OUTPUT_DIR'])\n"
            "folder.mkdir(parents=True, exist_ok=True)\n"
            "(folder / 'out.txt').write_text('made')\n")
    child(monkeypatch, code)
    monkeypatch.setenv(config.LOCK_PROJECT_ENV, str(machine))
    portal.routes["/v1/uploads"] = lambda _body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": "k"}, "key": "k"})
    execute(job(machine, "run_mr", MR_ARGS))
    output = portal.output()
    result = portal.result()
    assert result["outcome"] == "done", output + str(result)
    assert "file print('mr')" in output
    assert "env False False" in output
    assert "project eulav-tcejorp eulav-ppa" in output
    assert "name example" in output
    assert "trigger mr 12" in output
    assert f"args --full {COMMAND}" in output
    assert "lock None" in output
    assert result["artifact_key"] == "k" and result["log_key"] == "k"
    assert portal.bodies("/v1/copies/get")[0]["unit"] == "mr=12"
    assert not (service_runner.service_folder() / "work" / COMMAND).exists()
    assert (machine / "hr" / "run.py").read_text() == "print('hi')\n"


def test_a_scheduled_run_and_an_mr_run_of_one_app_hold_their_locks_at_once(machine, portal,
                                                                            monkeypatch):
    serve_mr(portal)
    prepared = service_runner.prepare(job(machine, "run_mr", MR_ARGS), dict(os.environ))
    assert config.LOCK_PROJECT_ENV not in prepared.env
    assert prepared.cwd != machine
    monkeypatch.setenv(config.LOCK_PROJECT_ENV, str(machine))
    with config.run_lock("hr"):
        monkeypatch.setenv(config.LOCK_PROJECT_ENV, prepared.env["PDT_PROJECT"])
        with config.run_lock("hr"):
            pass


def test_an_mr_copy_of_another_customer_does_not_run(machine, portal, monkeypatch):
    serve_mr(portal, {"pdt.yml": b"managed_services:\n  customer: cus_0000000000\n",
                      "hr/run.py": b"x"})
    child(monkeypatch, "raise SystemExit('this must not run')")
    execute(job(machine, "run_mr", MR_ARGS))
    result = portal.result()
    assert result["outcome"] == "failed" and "cus_0000000000" in result["message"]
    assert "started" not in portal.facts()


@pytest.mark.parametrize("commit", [None, "b" * 40])
def test_an_mr_copy_of_another_commit_does_not_run(machine, portal, monkeypatch, commit):
    serve_mr(portal, commit=commit)
    child(monkeypatch, "raise SystemExit('this must not run')")
    execute(job(machine, "run_mr", MR_ARGS))
    result = portal.result()
    assert result["outcome"] == "refused"
    assert f"names commit {MR_ARGS['sha']}" in result["message"]
    assert "started" not in portal.facts()


@pytest.mark.parametrize("pin, runs", [("0.1.13", False), ("0.1.14", True), ("0.2.0", True)])
def test_an_mr_run_of_an_app_that_pins_an_older_pdt_is_refused(machine, portal, monkeypatch,
                                                                pin, runs):
    run_py = (f'# /// script\n# dependencies = ["pdt-cli[apps]=={pin}"]\n# ///\n'
              "print('mr')\n").encode()
    serve_mr(portal, {"pdt.yml": f"managed_services:\n  customer: {CUSTOMER}\n".encode(),
                      "hr/config.yml": b"schedule: daily\n", "hr/run.py": run_py})
    child(monkeypatch, "pass")
    portal.routes["/v1/uploads"] = lambda _body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": "k"}, "key": "k"})
    execute(job(machine, "run_mr", MR_ARGS))
    result = portal.result()
    if runs:
        assert result["outcome"] == "done", result
        return
    assert result["outcome"] == "refused"
    assert result["message"].startswith(f"hr/run.py line 2 pins pdt-cli {pin}.")
    assert f"pdt-cli {service_runner.MR_STATE_VERSION} or newer" in result["message"]
    assert "\nNext steps:\n  pdt upgrade  " in result["message"]


@pytest.mark.parametrize("dependencies, old", [
    ("dependencies = ['pdt-cli[apps]==0.1.13']", "0.1.13"),
    ('dependencies = [\n#     "requests",\n#     "pdt_cli <= 0.1.12",\n# ]', "0.1.12"),
    ('dependencies = ["pdt-cli<0.1.14"]', "0.1.14"),
    ('dependencies = ["PDT-CLI[apps] === 0.1.9 ; python_version >= \'3.12\'"]', "0.1.9"),
    ('dependencies = ["pdt-cli>=0.1.10"]', None),
    ('dependencies = ["pdt-cli~=0.1.13"]', None),
    ('dependencies = ["pdt-cli-extra==0.1.0"]', None),
    ('dependencies = ["pdt-cli[apps]==0.1.14"]', None),
])
def test_the_mr_pin_check_reads_the_script_metadata(tmp_path, dependencies, old):
    (tmp_path / "hr").mkdir()
    (tmp_path / "hr" / "run.py").write_text(
        f"import os\n# /// script\n# requires-python = \">=3.12\"\n# {dependencies}\n# ///\n",
        encoding="utf-8")
    problem = service_runner.mr_copy_problem({"commit": "a" * 40}, tmp_path, "hr", "a" * 40)
    if old is None:
        assert problem == ""
        return
    line = 6 if "\n" in dependencies else 4
    assert problem.startswith(f"hr/run.py line {line} pins pdt-cli {old}.")


def test_an_mr_with_no_copy_is_refused(machine, portal, monkeypatch):
    portal.serve_copies()
    child(monkeypatch, "raise SystemExit('this must not run')")
    execute(job(machine, "run_mr", MR_ARGS))
    result = portal.result()
    assert result["outcome"] == "refused" and "mr=12" in result["message"]


def test_a_bad_mr_zip_reports_failed_and_leaves_no_folder(machine, portal, monkeypatch):
    entry, _data = copy_of("mr=12", {"a.txt": b"x"})
    portal.zips[entry["sha256"]] = b"not a zip"
    portal.routes["/v1/copies/get"] = lambda _body: (200, {"units": [
        {**entry, "url": f"{portal.url}/zip/{entry['sha256']}"}]})
    child(monkeypatch, "raise SystemExit('this must not run')")
    execute(job(machine, "run_mr", MR_ARGS))
    result = portal.result()
    assert result["outcome"] == "failed"
    assert not (service_runner.service_folder() / "work" / COMMAND).exists()


def test_secrets_set_gets_the_value_once_and_gives_it_on_stdin_masked(machine, portal, monkeypatch):
    (machine / "hr" / "config.yml").write_text("schedule: daily\nenv:\n  required: [TOKEN]\n")
    portal.routes["/v1/commands/input"] = lambda _body: (200, {"value": "hunter2-secret"})
    child(monkeypatch, "import sys\nprint('got', sys.stdin.read())\n")
    execute(job(machine, "secrets_set", {"project": "example", "app": "hr", "name": "TOKEN"}))
    asked = portal.bodies("/v1/commands/input")
    assert asked == [{"version": 1, "command_id": COMMAND, "claim_id": "0" * 32}]
    assert portal.result()["outcome"] == "done"
    assert "got ***" in portal.output() and "hunter2" not in portal.output()


def test_secrets_set_of_a_name_outside_the_env_spec_is_refused(machine, portal, monkeypatch):
    child(monkeypatch, "raise SystemExit('this must not run')")
    execute(job(machine, "secrets_set", {"project": "example", "app": "hr", "name": "PATH"}))
    result = portal.result()
    assert result["outcome"] == "refused" and "env spec" in result["message"]
    assert portal.bodies("/v1/commands/input") == []


def test_artifacts_uploads_the_folder_that_the_child_fills(machine, portal, monkeypatch):
    child(monkeypatch, "import pathlib, sys\n"
                       "(pathlib.Path(sys.argv[1]) / 'report.csv').write_text('a,b')\n")
    portal.routes["/v1/uploads"] = lambda _body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": "art"}, "key": "art"})
    execute(job(machine, "artifacts", {"project": "example", "app": "hr", "run": "3"}))
    result = portal.result()
    assert result["outcome"] == "done" and result["artifact_key"] == "art"
    assert not (service_runner.service_folder() / "work" / COMMAND).exists()


def test_masking_reads_the_env_files_again_when_the_command_starts(machine, portal, monkeypatch):
    item = job(machine)
    item.known_values = service_runner.known_values_of(machine, "hr")
    (machine / "hr" / ".env").write_text("BOTH=set-after-the-claim\n")
    child(monkeypatch, "print('new set-after-the-claim old app-value')")
    execute(item)
    assert "new *** old ***" in portal.output()


def test_a_secret_larger_than_a_pipe_reaches_the_child(machine, portal, monkeypatch):
    (machine / "hr" / "config.yml").write_text("schedule: daily\nenv:\n  required: [TOKEN]\n")
    portal.routes["/v1/commands/input"] = lambda _body: (200, {"value": "k" * 32768})
    child(monkeypatch, "import sys\nprint('got', len(sys.stdin.read()))\n")
    thread = run_in_thread(job(machine, "secrets_set",
                               {"project": "example", "app": "hr", "name": "TOKEN"}))
    thread.join(20)
    assert not thread.is_alive()
    assert "got 32768" in portal.output()


def test_cancel_of_a_deployed_windows_run_stops_its_task(machine, monkeypatch):
    (machine / ".pdt").mkdir(exist_ok=True)
    (machine / ".pdt" / "state").write_text(json.dumps({"platforms": {"hr": {"provider": "windows"}}}))
    calls = []
    monkeypatch.setattr(service_runner.subprocess, "run", lambda args, **_kwargs: (
        calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr="")))
    found = job(machine, args={"project": "example", "app": "hr", "deployed": True})
    assert service_runner.stop_deployed(found) == "pdt stopped the Windows task pdt-hr."
    assert "Stop-ScheduledTask -TaskName 'pdt-hr' -ErrorAction Stop" in calls[0]


def test_kill_starts_beside_a_run_of_its_app_and_beside_four_commands(machine, portal,
                                                                      monkeypatch):
    pool = Pool()
    release = threading.Event()
    monkeypatch.setattr(service_runner, "execute", lambda _job: release.wait(10))
    ids = [f"cmd_20261103T12000{index}Z_0123abcd" for index in range(5)]
    for command_id, app in zip(ids, ["hr", "b", "c", "d"]):
        pool.add(job(machine, args={"project": "example", "app": app, "deployed": False},
                     command_id=command_id))
    pool.add(job(machine, "kill", {"project": "example", "app": "hr"}, command_id=ids[4]))
    pool.start_ready()
    assert sorted(pool.running) == sorted(ids)
    release.set()
    for item in list(pool.running.values()):
        item.thread.join(5)


def test_in_project_holds_one_project_at_a_time(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    entered = threading.Event()
    seen = []

    def first():
        with service_commands.in_project(tmp_path / "a"):
            entered.set()
            time.sleep(0.5)
            seen.append(os.environ["PDT_PROJECT"])

    thread = threading.Thread(target=first)
    thread.start()
    entered.wait(5)
    with service_commands.in_project(tmp_path / "b"):
        time.sleep(1)
    thread.join(5)
    assert seen == [str(tmp_path / "a")]


def test_a_result_waits_until_its_output_is_sent(machine, portal, monkeypatch):
    down = [True]
    portal.routes["/v1/commands/report"] = lambda body: (
        (500, {"error": "x"}) if body["fact"] == "output" and down[0] else (200, {}))
    child(monkeypatch, "print('the last line')")
    execute(job(machine))
    assert "result" not in portal.facts()
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert "result" not in portal.facts()
    assert service_runner.journal_read(COMMAND)["reported"] is False
    down[0] = False
    service_runner.recover(Pool(), registration(), Clock(), lambda name: machine)
    assert "the last line" in portal.output()
    assert portal.facts()[-1] == "result"
    assert service_runner.journal_read(COMMAND)["reported"] is True


def test_a_report_answered_503_returns_at_once(machine, portal, monkeypatch):
    monkeypatch.setattr(autoidm, "RETRY_LIMIT_SECONDS", 2)
    portal.routes["/v1/commands/report"] = lambda _body: (503, {"error": "busy"})
    item = job(machine)
    service_runner.output_path(COMMAND).parent.mkdir(parents=True, exist_ok=True)
    service_runner.output_path(COMMAND).write_text("line\n")
    started = time.monotonic()
    assert service_runner.send_output(item, final=True) is False
    assert time.monotonic() - started < 1


def test_artifacts_uploads_its_log(machine, portal, monkeypatch):
    child(monkeypatch, "import pathlib, sys\n"
                       "print('wrote report.csv')\n"
                       "(pathlib.Path(sys.argv[1]) / 'report.csv').write_text('a,b')\n")
    portal.routes["/v1/uploads"] = lambda body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": body["kind"]}, "key": body["kind"]})
    execute(job(machine, "artifacts", {"project": "example", "app": "hr", "run": "3"}))
    result = portal.result()
    assert (result["artifact_key"], result["log_key"]) == ("artifact", "log")


def admin_check(monkeypatch, answer):
    checked = []
    monkeypatch.setattr(service.service_access, "is_admin", lambda: True)
    monkeypatch.setattr(service.service_access, "writable_by_others",
                        lambda folder: checked.append(folder) or answer)
    return checked


def test_a_project_that_another_account_can_change_is_refused(machine, portal, monkeypatch):
    admin_check(monkeypatch, ("BUILTIN\\Users", machine / "hr" / "run.py"))
    pool = Pool()
    assert accept(pool, token())
    assert pool.waiting == []
    assert str(machine / "hr" / "run.py") in portal.result()["message"]


def test_a_project_is_checked_before_its_first_command_only(machine, portal, monkeypatch):
    checked = admin_check(monkeypatch, None)
    found, pool = served(), Pool()
    for command_id in (COMMAND, SECOND):
        service.accept({"command_id": command_id, "token": token(command_id=command_id)}, found,
                       settings(), pool)
    assert len(pool.waiting) == 2
    assert checked == [machine]


def test_the_start_check_covers_the_projects_folder(machine, monkeypatch):
    checked = admin_check(monkeypatch, None)
    assert service.access_problem(settings()) is False
    assert checked == [machine.parent]


def test_a_start_check_that_cannot_finish_stops_the_service(machine, monkeypatch):
    def too_many(folder):
        raise service.service_access.AccessError(f"{folder} holds more than 50000 files")
    monkeypatch.setattr(service.service_access, "is_admin", lambda: True)
    monkeypatch.setattr(service.service_access, "writable_by_others", too_many)
    assert service.access_problem(settings()) is True
