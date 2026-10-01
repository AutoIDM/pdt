"""The score-mrs task hooks: the rule floor, Claude's verdict, labels, merging."""

import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parent.parent
REPO = CI.parent
TASK = CI / "claude-tasks" / "score-mrs"


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GitLabLoader(yaml.SafeLoader):
    pass


GitLabLoader.add_constructor("!reference", lambda load, node: load.construct_sequence(node))


def load_ci(path=".gitlab-ci.yml"):
    return yaml.load((REPO / path).read_text(), Loader=GitLabLoader)


select = load_script(TASK / "select_mrs.py", "score_select")
check = load_script(TASK / "check_mrs.py", "score_check")
runner = load_script(CI / "claude_task.py", "claude_task_for_score")


# --- fixtures -----------------------------------------------------------------

def diff_for(path: str, lines: int = 1, extra: str = "") -> str:
    body = "\n".join(f"+line {n}" for n in range(lines))
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}\n{extra}"


def change(path: str, lines: int = 1, **flags) -> dict:
    base = {"old_path": path, "new_path": path, "new_file": False, "renamed_file": False,
            "deleted_file": False, "diff": diff_for(path, lines, flags.pop("extra", ""))}
    return {**base, **flags}


def mr(iid: int = 1, **overrides) -> dict:
    base = {"iid": iid, "title": f"Change {iid}", "draft": False, "sha": f"sha{iid}",
            "source_branch": f"branch-{iid}", "target_branch": "master",
            "source_project_id": 42, "target_project_id": 42, "labels": [],
            "has_conflicts": False, "blocking_discussions_resolved": True,
            "detailed_merge_status": "mergeable", "changes_count": "1",
            "head_pipeline": {"status": "success", "sha": f"sha{iid}"}}
    return {**base, **overrides}


class FakeResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def install(monkeypatch, routes):
    """urlopen answers from ``routes`` [((method, path substring), payload)] and records calls."""
    calls = []

    def fake_urlopen(request, timeout=0):
        method, path = request.get_method(), request.full_url.split("/api/v4")[-1]
        calls.append((method, path, json.loads(request.data) if request.data else None))
        for (want_method, needle), payload in routes:
            if method == want_method and needle in path:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def arm(monkeypatch, dry_run="false"):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("CI_DEFAULT_BRANCH", "master")
    monkeypatch.setenv("DRY_RUN", dry_run)


def note(marker: str, system=False) -> dict:
    return {"body": marker + "\n**MR tier**", "system": system}


# --- rule floor ---------------------------------------------------------------

def test_path_floors_follow_the_table():
    arch = ["src/pdt/utils/log.py", "src/pdt/deploy.py", "src/pdt/deploy_common.py",
            "src/pdt/config.py", "src/pdt/cli.py", "src/pdt/__init__.py", ".gitlab-ci.yml",
            ".github/workflows/ci.yml", "ci/claude_task.py", "AGENTS.md", "CLAUDE.md",
            "pdt", "pdt.bat"]
    for path in arch:
        assert select.path_floor(path)[0] == "architectural", path
    review = ["src/pdt/deploy_aws.py", "src/pdt/scaffold.py", "src/pdt/gcloud_sdk.py",
              "src/pdt/console.py", "scripts/smoke.py", "tests/conftest.py", "uv.lock",
              ".gitignore", "src/pdt/something_new.py", "Makefile"]
    for path in review:
        assert select.path_floor(path)[0] == "review", path
    simple = ["README.md", "docs/guide.md", "tests/test_cli.py", "ci/tests/test_x.py",
              "src/pdt/examples/hello-world/run.py"]
    for path in simple:
        assert select.path_floor(path) == ("simple", ""), path


def test_a_new_provider_module_is_architectural_and_an_existing_one_is_review():
    assert select.path_floor("src/pdt/deploy_oracle.py", new_file=True)[0] == "architectural"
    assert select.path_floor("src/pdt/deploy_aws.py", new_file=False)[0] == "review"


def test_pyproject_dependency_lines_are_architectural_and_a_version_bump_is_review():
    dep = diff_for("pyproject.toml", extra='+    "boto3",\n')
    assert select.path_floor("pyproject.toml", diff=dep)[0] == "architectural"
    bump = '--- a/pyproject.toml\n+++ b/pyproject.toml\n-version = "1.0"\n+version = "1.1"\n'
    assert select.path_floor("pyproject.toml", diff=bump) == ("review", "pyproject.toml changed")


def test_highest_floor_wins_and_reasons_name_the_file():
    floor, reasons = select.rule_floor(mr(), [change("README.md"), change("src/pdt/cli.py"),
                                              change("src/pdt/scaffold.py")])
    assert floor == "architectural"
    assert reasons == ["src/pdt/cli.py is the command line"]


def test_a_simple_floor_says_why():
    assert select.rule_floor(mr(), [change("README.md")]) == (
        "simple", ["only documentation, tests, or example apps changed"])


def test_a_few_tested_lines_in_a_covered_file_floor_at_simple():
    small = change("src/pdt/deploy_common.py", lines=4)
    test = change("tests/test_dockerfile.py")
    floor, reasons = select.rule_floor(mr(), [small, test])
    assert floor == "simple" and "src/pdt/deploy_common.py" in reasons[0]
    assert select.rule_floor(mr(), [small])[0] == "architectural"
    assert select.rule_floor(mr(), [small, change("ci/tests/test_x.py")])[0] == "architectural"
    five = change("src/pdt/deploy_common.py", lines=5)
    assert select.rule_floor(mr(), [five, test])[0] == "architectural"
    gated = change("src/pdt/cli.py", extra="+token = 1\n")
    assert select.rule_floor(mr(), [gated, test])[0] == "architectural"
    for path in ("src/pdt/utils/log.py", ".gitlab-ci.yml", "AGENTS.md", "pdt"):
        assert select.rule_floor(mr(), [change(path), test])[0] == "architectural", path


def test_deleted_renamed_and_large_files_bump_simple_to_review():
    for flag in ("deleted_file", "renamed_file", "too_large", "collapsed"):
        floor, reasons = select.rule_floor(mr(), [change("README.md", **{flag: True})])
        assert floor == "review", flag
        assert reasons and "README.md" in reasons[0], flag


def test_a_truncated_diff_or_a_fork_bumps_to_review():
    assert select.rule_floor(mr(changes_count="12+"), [change("README.md")]) == (
        "review", ["GitLab truncated the diff"])
    assert select.rule_floor(mr(source_project_id=7), [change("README.md")]) == (
        "review", ["comes from a fork"])


def test_size_caps():
    assert select.rule_floor(mr(), [change("README.md", lines=200)])[0] == "simple"
    floor, reasons = select.rule_floor(mr(), [change("README.md", lines=201)])
    assert floor == "review" and "201 lines" in reasons[0]
    five = [change(f"docs/{n}.md") for n in range(5)]
    assert select.rule_floor(mr(), five)[0] == "simple"
    floor, reasons = select.rule_floor(mr(), five + [change("docs/6.md")])
    assert floor == "review" and "6 files" in reasons[0]


def test_hard_gate_words_bump_to_review_and_name_the_word():
    files = [change("tests/test_x.py", extra="+    token = 'abc'\n")]
    floor, reasons = select.rule_floor(mr(), files)
    assert floor == "review" and reasons == ["tests/test_x.py mentions `token`"]
    assert select.hard_gate_hits("+x = 1\n-y = os.environ['A']\n+++ b/x") == ["os.environ"]


def test_diff_id_ignores_file_order_and_tracks_content():
    a, b = change("a.md"), change("b.md")
    assert select.diff_id([a, b]) == select.diff_id([b, a])
    assert select.diff_id([a]) != select.diff_id([change("a.md", lines=2)])
    assert len(select.diff_id([a])) == 12


def test_marker_round_trip_and_newest_bot_note_wins():
    marker = select.format_marker("abc123def456", "simple", "review", "raised")
    assert select.parse_marker(marker + "\nmore") == {
        "diff": "abc123def456", "floor": "simple", "tier": "review", "claude": "raised"}
    assert check.format_marker("a", "b", "c", "d") == select.format_marker("a", "b", "c", "d")
    assert select.parse_marker("<!-- pdt-mr-score v1 diff=x -->") is None
    assert select.parse_marker("hello") is None
    notes = [note("added 1 commit", system=True), {"body": "looks fine"},
             note(select.format_marker("new", "simple", "simple", "agree")),
             note(select.format_marker("old", "simple", "review", "raised"))]
    assert select.previous_score(notes)["diff"] == "new"
    assert select.previous_score([{"body": "nothing"}]) is None


def test_merge_blockers_name_each_gate():
    assert select.merge_blockers(mr()) == []
    assert select.merge_blockers(mr(draft=True)) == ["still a draft"]
    assert select.merge_blockers(mr(has_conflicts=True)) == ["has conflicts"]
    assert select.merge_blockers(mr(blocking_discussions_resolved=False)) == [
        "unresolved discussions"]
    assert select.merge_blockers(mr(head_pipeline=None)) == ["pipeline missing"]
    assert select.merge_blockers(mr(head_pipeline={"status": "failed", "sha": "sha1"})) == [
        "pipeline failed"]
    assert select.merge_blockers(mr(head_pipeline={"status": "success", "sha": "older"})) == [
        "pipeline ran on an older commit"]
    assert select.merge_blockers(mr(detailed_merge_status="ci_must_pass")) == [
        "merge status ci_must_pass"]
    assert select.merge_blockers(mr(source_project_id=7)) == ["comes from a fork"]


def test_wanted_keeps_drafts_and_drops_forks():
    assert select.wanted(mr(), "42")
    assert select.wanted(mr(draft=True), "42")
    assert not select.wanted(mr(source_project_id=7), "42")


def test_a_draft_is_scored_once_and_again_only_when_ready_with_a_new_diff():
    scored_old = {"diff": "old", "floor": "simple", "tier": "simple", "claude": "agree"}
    unavailable = {**scored_old, "claude": "unavailable"}
    assert not select.already_scored(mr(draft=True), None, "new")
    assert select.already_scored(mr(draft=True), scored_old, "new")
    assert not select.already_scored(mr(draft=True), unavailable, "new")
    assert not select.already_scored(mr(draft=False), scored_old, "new")
    assert select.already_scored(mr(draft=False), scored_old, "old")


# --- Claude's verdict ---------------------------------------------------------

def test_parse_verdict_takes_the_last_fenced_block_or_a_bare_object():
    fenced = 'Thinking.\n```json\n{"tier": "simple", "reasons": ["x"]}\n```\nmore\n' \
             '```json\n{"tier": "review", "reasons": [" agree with floor ", "b.py changed"]}\n```'
    assert check.parse_verdict({"result": fenced}) == {
        "tier": "review", "reasons": ["agree with floor", "b.py changed"]}
    bare = 'Done. {"tier": "architectural", "reasons": ["cli.py"]}'
    assert check.parse_verdict({"result": bare})["tier"] == "architectural"


def test_parse_verdict_rejects_bad_output():
    assert check.parse_verdict({"result": "no json here"}) is None
    assert check.parse_verdict({"result": '{"tier": "huge", "reasons": []}'}) is None
    assert check.parse_verdict({"result": '{"tier": "simple", "reasons": "x"}'}) is None
    assert check.parse_verdict({"result": '{"tier": "simple"'}) is None
    assert check.parse_verdict(
        {"is_error": True, "result": '{"tier": "simple", "reasons": []}'}) is None


def test_combine_raises_never_lowers_and_defaults_to_review_without_claude():
    raised = check.combine("simple", ["docs only"], {"tier": "review", "reasons": ["a.py"]})
    assert (raised.tier, raised.claude, raised.reasons) == ("review", "raised", ["a.py"])
    kept = check.combine("architectural", ["cli.py"], {"tier": "simple",
                                                        "reasons": ["agree with floor"]})
    assert (kept.tier, kept.claude, kept.reasons) == ("architectural", "agree", ["cli.py"])
    none_simple = check.combine("simple", ["docs only"], None)
    assert (none_simple.tier, none_simple.claude) == ("review", "unavailable")
    assert "needs a human" in none_simple.reasons[-1]
    none_arch = check.combine("architectural", ["cli.py"], None)
    assert (none_arch.tier, none_arch.claude) == ("architectural", "unavailable")


def test_status_and_note_body():
    arch = check.Score("architectural", "review", ["touches deploy dispatch"], "raised")
    assert check.status_for(arch) == {"status": "needs_human",
                                      "message": "needs discussion: touches deploy dispatch"}
    assert check.status_for(check.Score("review", "review", [], "agree")) == {
        "status": "ok", "message": "tier::review"}
    assert check.status_for(check.Score("review", "simple", [], "unavailable"))["status"] == \
        "needs_human"
    body = check.note_body("abc", arch, "http://job")
    assert body.splitlines()[0] == check.format_marker("abc", "review", "architectural", "raised")
    assert "`tier::architectural`" in body and "Claude raised it" in body
    assert "- touches deploy dispatch" in body and "http://job" in body
    assert check.MERGE_FOOTER not in body and check.FOOTER in body
    simple = check.note_body("abc", check.Score("simple", "simple", [], "agree"), "")
    assert check.MERGE_FOOTER in simple


def test_label_change_leaves_exactly_one_tier_label():
    assert check.label_change([], "simple") == (["tier::simple"], [])
    assert check.label_change(["tier::review", "bug"], "simple") == (["tier::simple"],
                                                                     ["tier::review"])
    assert check.label_change(["tier::simple"], "simple") == ([], [])


# --- select hook end to end ---------------------------------------------------

def run_select(monkeypatch, capsys, mrs, notes, dry_run="false"):
    arm(monkeypatch, dry_run)
    fetches = []
    monkeypatch.setattr(select.subprocess, "run",
                        lambda cmd, **kw: fetches.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    # The list endpoint leaves out what the merge gates read; the single GET has it.
    slim = [{k: v for k, v in item.items() if k not in ("_files", "head_pipeline",
                                                        "changes_count")}
            for item in mrs.values()]
    routes = [(("GET", "merge_requests?state=opened"), slim)]
    for iid, item in mrs.items():
        routes.append((("GET", f"/merge_requests/{iid}/diffs"), item["_files"]))
        routes.append((("GET", f"/merge_requests/{iid}/notes"), notes.get(iid, [])))
        routes.append((("PUT", f"/merge_requests/{iid}/merge"), {"state": "merged"}))
        routes.append((("POST", f"/merge_requests/{iid}/notes"), {"id": 1}))
        routes.append((("GET", f"/merge_requests/{iid}"), {k: v for k, v in item.items()
                                                          if k != "_files"}))
    calls = install(monkeypatch, routes)
    assert select.main() == 0
    out, err = capsys.readouterr()
    return json.loads(out), err, calls, fetches


def scored(files, tier, floor="simple", claude="agree"):
    return [note(select.format_marker(select.diff_id(files), floor, tier, claude))]


def test_select_merges_scored_simple_mrs_and_emits_the_rest(monkeypatch, capsys):
    docs = [change("README.md")]
    mrs = {1: {**mr(1), "_files": docs},
           2: {**mr(2, head_pipeline={"status": "failed", "sha": "sha2"}), "_files": docs},
           3: {**mr(3), "_files": [change("docs/new.md")]},
           4: {**mr(4), "_files": [change("src/pdt/deploy_aws.py")]},
           5: {**mr(5), "_files": docs},
           6: {**mr(6, draft=True), "_files": docs},
           7: {**mr(7, draft=True), "_files": [change("docs/changed.md")]}}
    notes = {1: scored(docs, "simple"), 2: scored(docs, "simple"),
             4: scored([change("src/pdt/deploy_aws.py")], "review"),
             5: scored(docs, "review", claude="unavailable"),
             6: scored(docs, "simple"), 7: scored(docs, "simple")}
    items, err, calls, fetches = run_select(monkeypatch, capsys, mrs, notes)

    assert [item["iid"] for item in items] == [3, 5]
    assert "draft       !6" in err and "draft       !7" in err and "not merged" in err
    assert items[0]["floor"] == "simple" and items[0]["floor_reason_list"] == [
        "only documentation, tests, or example apps changed"]
    assert items[0]["files"] == "docs/new.md" and items[0]["previous"] is None
    assert items[1]["previous"]["claude"] == "unavailable"
    merges = [(m, p, b) for m, p, b in calls if m in ("PUT", "POST")]
    assert merges == [
        ("PUT", "/projects/42/merge_requests/1/merge",
         {"sha": "sha1", "should_remove_source_branch": True, "squash": False}),
        ("POST", "/projects/42/merge_requests/1/notes", {"body": select.MERGE_NOTE}),
    ]
    assert "merged      !1" in err and "blocked     !2" in err and "pipeline failed" in err
    assert ("GET", "/projects/42/merge_requests/1", None) in calls
    assert "review      !4" in err and "score       !3" in err
    assert fetches[0][:4] == ["git", "fetch", "--quiet", "origin"]
    assert "+refs/heads/branch-3:refs/remotes/origin/branch-3" in fetches[0]


def test_select_dry_run_writes_nothing(monkeypatch, capsys):
    docs = [change("README.md")]
    items, err, calls, _ = run_select(monkeypatch, capsys, {1: {**mr(1), "_files": docs}},
                                      {1: scored(docs, "simple")}, dry_run="true")
    assert items == []
    assert all(method == "GET" for method, _, _ in calls)
    assert "would merge !1" in err


def test_select_reports_a_refused_merge_and_continues(monkeypatch, capsys):
    arm(monkeypatch)
    monkeypatch.setattr(select.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    docs = [change("README.md")]

    def refuse(request, timeout=0):
        path = request.full_url.split("/api/v4")[-1]
        if path.endswith("/merge"):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b'{"message":"403 Forbidden"}'))
        if "state=opened" in path or path.endswith("/merge_requests/1"):
            return FakeResponse([mr(1)] if "state=opened" in path else mr(1))
        if "/diffs?" in path:
            return FakeResponse(docs)
        return FakeResponse(scored(docs, "simple"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert select.main() == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == []
    assert "not merged  !1" in err and "GITLAB_TOKEN may not be allowed" in err


def test_select_names_missing_variables(monkeypatch, capsys):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    assert select.main() == 1
    assert "GITLAB_TOKEN" in capsys.readouterr().err


# --- a webhook run looks at one MR --------------------------------------------

def test_a_webhook_run_scores_only_the_mr_in_the_event(monkeypatch, capsys):
    monkeypatch.setenv("MR_IID", "2")
    mrs = {1: {**mr(1), "_files": [change("docs/a.md")]},
           2: {**mr(2), "_files": [change("docs/b.md")]},
           3: {**mr(3), "_files": [change("docs/c.md")]}}
    items, err, calls, fetches = run_select(monkeypatch, capsys, mrs, {})
    assert [item["iid"] for item in items] == [2]
    assert "webhook event for !2: looking at that MR only" in err
    assert "1 open MR(s)" in err
    touched = {p for _, p, _ in calls if "/merge_requests/" in p}
    assert touched == {"/projects/42/merge_requests/2",
                       "/projects/42/merge_requests/2/diffs?per_page=100&page=1",
                       "/projects/42/merge_requests/2/notes?sort=desc&order_by=created_at"
                       "&per_page=100&page=1"}
    assert fetches == [["git", "fetch", "--quiet", "origin",
                        "+refs/heads/master:refs/remotes/origin/master",
                        "+refs/heads/branch-2:refs/remotes/origin/branch-2"]]


def test_a_webhook_run_for_an_mr_that_is_no_longer_open_lists_nothing(monkeypatch, capsys):
    monkeypatch.setenv("MR_IID", "9")
    items, err, calls, fetches = run_select(monkeypatch, capsys, {1: {**mr(1), "_files": []}}, {})
    assert items == [] and fetches == []
    assert "not an open MR of this project" in err
    assert [p for _, p, _ in calls] == [
        "/projects/42/merge_requests?state=opened&target_branch=master&per_page=100&page=1"]


def test_a_run_without_an_event_sweeps_every_open_mr(monkeypatch, capsys):
    monkeypatch.delenv("MR_IID", raising=False)
    mrs = {1: {**mr(1), "_files": [change("docs/a.md")]},
           2: {**mr(2), "_files": [change("docs/b.md")]}}
    items, err, _, _ = run_select(monkeypatch, capsys, mrs, {})
    assert [item["iid"] for item in items] == [1, 2]
    assert "webhook event" not in err
    monkeypatch.setenv("MR_IID", "  ")
    items, _, _, _ = run_select(monkeypatch, capsys, mrs, {})
    assert [item["iid"] for item in items] == [1, 2]


# --- which events run which job -----------------------------------------------

def job_runs(job: str, variables: dict) -> bool:
    """Evaluate one job's rules the way GitLab would for these variables.

    Only what those rules use is understood: ``$X == "v"``, ``$X != "v"`` and
    ``$X == null``, joined with ``&&``, and a rule with no ``if`` that matches
    everything. The first matching rule decides; no match means the job does
    not run.
    """
    ci = load_ci()
    for rule in ci[job]["rules"]:
        matched = True
        for clause in rule["if"].split(" && ") if "if" in rule else ():
            if " != " in clause:
                name, _, value = clause.partition(" != ")
                matched &= variables.get(name.strip("$ ")) != value.strip('"')
            else:
                name, _, value = clause.partition(" == ")
                expected = None if value == "null" else value.strip('"')
                matched &= variables.get(name.strip("$ ")) == expected
        if matched:
            return rule.get("when") != "never"
    return False


def trigger(action, before="null", after="null"):
    """The variables the webhook's template sends; a missing Draft change renders as null."""
    return {"CI_PIPELINE_SOURCE": "trigger", "mode": "score_mrs", "MR_IID": "5",
            "MR_ACTION": action, "MR_DRAFT_BEFORE": before, "MR_DRAFT_AFTER": after}


SCORED_EVENTS = [trigger("open"), trigger("reopen"), trigger("update", "true", "false")]
# Pushes, rebases, labels, edits, going to Draft, approvals, closing, merging.
SKIPPED_EVENTS = [trigger("update"), trigger("update", "false", "true"),
                  trigger("update", "true", "true"), trigger("approved"),
                  trigger("close"), trigger("merge"), trigger("")]


def test_a_webhook_event_scores_only_when_the_mr_opens_or_leaves_draft():
    for variables in SCORED_EVENTS:
        assert job_runs("score-mrs", variables), variables
        assert not job_runs("score-mrs-skip", variables), variables
    for variables in SKIPPED_EVENTS:
        assert not job_runs("score-mrs", variables), variables


def test_every_webhook_event_gets_a_pipeline_so_the_hook_never_sees_a_4xx():
    # The trigger API answers 400 when a pipeline has no job or workflow rules
    # filter it out, and GitLab disables a webhook after four 4xx in a row.
    ci = load_ci()
    assert "workflow" not in ci, "workflow rules would filter trigger pipelines out"
    for variables in SCORED_EVENTS + SKIPPED_EVENTS:
        assert job_runs("score-mrs", variables) != job_runs("score-mrs-skip", variables), variables


def test_the_skip_job_never_runs_outside_a_score_mrs_trigger():
    for variables in [{"CI_PIPELINE_SOURCE": "merge_request_event"},
                      {"CI_PIPELINE_SOURCE": "push", "CI_COMMIT_BRANCH": "master"},
                      {"CI_PIPELINE_SOURCE": "schedule", "mode": "score_mrs"},
                      {"CI_PIPELINE_SOURCE": "schedule", "mode": "close_stale_drafts"},
                      {"CI_PIPELINE_SOURCE": "web", "mode": "score_mrs"},
                      {"CI_PIPELINE_SOURCE": "web"},
                      {"CI_PIPELINE_SOURCE": "trigger", "mode": "other"}]:
        assert not job_runs("score-mrs-skip", variables), variables
    assert job_runs("score-mrs", {"CI_PIPELINE_SOURCE": "schedule", "mode": "score_mrs"})
    assert job_runs("score-mrs", {"CI_PIPELINE_SOURCE": "web", "mode": "score_mrs"})


# --- check hook end to end ----------------------------------------------------

def run_check(monkeypatch, capsys, item, result, routes=(), dry_run="false"):
    arm(monkeypatch, dry_run)
    calls = install(monkeypatch, list(routes))
    payload = {"item": item, "result": result, "job_url": "http://job"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert check.main() == 0
    out, err = capsys.readouterr()
    return json.loads(out), err, calls


def item_for_check(**overrides):
    base = select.item_for(mr(3), "abc123def456", "simple",
                           ["only documentation, tests, or example apps changed"],
                           [change("README.md")], None)
    return {**base, **overrides}


def verdict(tier, *reasons):
    return {"result": f'```json\n{json.dumps({"tier": tier, "reasons": list(reasons)})}\n```'}


def test_check_creates_the_label_sets_it_and_comments(monkeypatch, capsys):
    routes = [(("GET", "/labels?search=tier::"), []), (("POST", "/projects/42/labels"), {}),
              (("PUT", "/merge_requests/3"), {}), (("POST", "/merge_requests/3/notes"), {})]
    status, err, calls = run_check(monkeypatch, capsys, item_for_check(),
                                   verdict("review", "agree with floor", "README.md rewords"),
                                   routes)
    assert status == {"status": "ok", "message": "tier::review"}
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/projects/42/labels?search=tier::&per_page=100"),
        ("POST", "/projects/42/labels"),
        ("PUT", "/projects/42/merge_requests/3"),
        ("POST", "/projects/42/merge_requests/3/notes"),
    ]
    assert calls[1][2]["name"] == "tier::review"
    assert calls[2][2] == {"add_labels": "tier::review"}
    body = calls[3][2]["body"]
    assert body.startswith(check.format_marker("abc123def456", "simple", "review", "raised"))
    assert "- README.md rewords" in body and "agree with floor" not in body


def test_check_is_quiet_when_nothing_changed(monkeypatch, capsys):
    item = item_for_check(labels=["tier::simple"], previous={
        "diff": "abc123def456", "floor": "simple", "tier": "simple", "claude": "agree"})
    status, err, calls = run_check(monkeypatch, capsys, item, verdict("simple", "agree with floor"))
    assert status["status"] == "ok" and calls == []


def test_check_swaps_a_stale_tier_label(monkeypatch, capsys):
    item = item_for_check(labels=["tier::review", "bug"], previous={
        "diff": "old", "floor": "simple", "tier": "review", "claude": "raised"})
    routes = [(("GET", "/labels?search=tier::"), [{"name": "tier::simple"}]),
              (("PUT", "/merge_requests/3"), {}), (("POST", "/merge_requests/3/notes"), {})]
    _, _, calls = run_check(monkeypatch, capsys, item, verdict("simple", "agree with floor"), routes)
    assert [m for m, _, _ in calls] == ["GET", "PUT", "POST"]
    assert calls[1][2] == {"add_labels": "tier::simple", "remove_labels": "tier::review"}


def test_check_reports_architectural_as_needs_human(monkeypatch, capsys):
    routes = [(("GET", "/labels?search=tier::"), [{"name": "tier::architectural"}]),
              (("PUT", "/merge_requests/3"), {}), (("POST", "/merge_requests/3/notes"), {})]
    status, _, _ = run_check(monkeypatch, capsys, item_for_check(),
                             verdict("architectural", "README.md documents a removed command"),
                             routes)
    assert status == {"status": "needs_human",
                      "message": "needs discussion: README.md documents a removed command"}


def test_check_without_a_verdict_scores_review_and_needs_a_human(monkeypatch, capsys):
    routes = [(("GET", "/labels?search=tier::"), [{"name": "tier::review"}]),
              (("PUT", "/merge_requests/3"), {}), (("POST", "/merge_requests/3/notes"), {})]
    status, _, calls = run_check(monkeypatch, capsys, item_for_check(),
                                 {"is_error": True, "result": "budget exceeded"}, routes)
    assert status["status"] == "needs_human" and "no verdict" in status["message"]
    assert calls[1][2] == {"add_labels": "tier::review"}
    assert "claude=unavailable" in calls[2][2]["body"]


def test_check_dry_run_writes_nothing_but_still_reports(monkeypatch, capsys):
    status, err, calls = run_check(monkeypatch, capsys, item_for_check(),
                                   verdict("review", "x"), dry_run="true")
    assert status["status"] == "ok" and calls == []
    assert "would set label tier::review" in err and "would post" in err


# --- task folder and CI -------------------------------------------------------

def test_shipped_score_task_loads_and_is_read_only():
    task = runner.load_task("score-mrs")
    assert task.model == "opus"
    assert task.select.name == "select_mrs.py" and task.check.name == "check_mrs.py"
    assert task.permission_mode == "dontAsk"
    assert not any(tool in task.allowed_tools for tool in ("Edit", "Write"))
    assert not any("push" in tool for tool in task.allowed_tools)
    item = select.item_for(mr(9), "abc", "simple", ["docs"], [change("README.md")], None)
    prompt = runner.render_prompt(task.prompt, item)
    assert "!9" in prompt and "origin/master...origin/branch-9" in prompt
    assert '{"tier": "simple" | "review" | "architectural"' in prompt


def test_score_mrs_job_runs_on_a_webhook_trigger_and_its_schedule():
    ci = load_ci()
    assert "$CLAUDE_TASK_ARGS" in ci[".claude-task"]["script"][0]
    job = ci["score-mrs"]
    assert job["extends"] == ".claude-task"
    assert job["variables"]["CLAUDE_TASK"] == "score-mrs"
    assert job["needs"] == []
    rules = {rule["if"]: rule.get("variables", {}) for rule in job["rules"]}
    assert not [k for k in rules if "CI_DEFAULT_BRANCH" in k], "no run on every merge"
    trigger = [v for k, v in rules.items() if '"trigger"' in k and "score_mrs" in k]
    assert len(trigger) == 3, "open, reopen, and leaving Draft"
    assert all(v == {"DRY_RUN": "false"} for v in trigger)
    schedule = [v for k, v in rules.items() if '"schedule"' in k and "score_mrs" in k]
    assert schedule == [{"DRY_RUN": "false"}]
    web = [(k, v) for k, v in rules.items() if '"web"' in k and "score_mrs" in k]
    assert [v for k, v in web if 'DRY_RUN == "false"' in k] == [{}]
    assert [v for k, v in web if "DRY_RUN" not in k] == [{"CLAUDE_TASK_ARGS": "--dry-run"}]


def test_a_mode_pipeline_runs_only_its_own_task():
    for path in (".gitlab-ci.yml", "verify/.gitlab-ci.yml"):
        ci = load_ci(path)
        for name, job in ci.items():
            if not isinstance(job, dict) or "rules" not in job:
                continue
            for rule in job["rules"]:
                condition = rule.get("if", "") if isinstance(rule, dict) else ""
                if "$mode ==" in condition and "$mode == null" not in condition:
                    continue  # the job a mode pipeline is for
                if "CI_DEFAULT_BRANCH" in condition or '"schedule"' in condition:
                    assert "$mode == null" in condition, f"{path}: {name}"
