"""The score-prs task hooks: the rule floor, Claude's verdict, labels, merging."""

import importlib.util
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parent.parent
REPO = CI.parent
TASK = CI / "claude-tasks" / "score-prs"
API = "https://api.github.example"
FULL_NAME = "AutoIDM/pdt"


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


select = load_script(TASK / "select_prs.py", "score_select")
check = load_script(TASK / "check_prs.py", "score_check")
runner = load_script(CI / "claude_task.py", "claude_task_for_score")


# --- fixtures -----------------------------------------------------------------

def patch_for(lines: int = 1, extra: str = "") -> str:
    body = "\n".join(f"+line {n}" for n in range(lines))
    return f"@@ -1 +1 @@\n{body}\n{extra}"


def change(path: str, lines: int = 1, status: str = "modified", **fields) -> dict:
    base = {"filename": path, "status": status, "changes": lines,
            "patch": patch_for(lines, fields.pop("extra", ""))}
    return {**base, **fields}


def pr(number: int = 1, head_repo: str = FULL_NAME, **overrides) -> dict:
    base = {"number": number, "title": f"Change {number}", "draft": False,
            "head": {"ref": f"branch-{number}", "sha": f"sha{number}",
                     "repo": {"full_name": head_repo}},
            "base": {"ref": "main", "repo": {"full_name": FULL_NAME}},
            "labels": [], "mergeable": True, "mergeable_state": "clean", "changed_files": 1}
    return {**base, **overrides}


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


GREEN = [run("test")]


class FakeResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def refuse(code: int):
    """A route answer that fails the call with an HTTP error."""
    def answer(body):
        raise urllib.error.HTTPError(API, code, "refused", {}, io.BytesIO(b'{"message":"no"}'))
    return answer


def install(monkeypatch, routes):
    """urlopen answers from ``routes`` [((method, path substring), payload)] and records calls.

    A substring never matches a longer number, so /pulls/1 does not answer
    /pulls/10. A callable payload gets the request body and returns the
    answer or raises.
    """
    calls = []

    def fake_urlopen(request, timeout=0):
        method, path = request.get_method(), request.full_url.removeprefix(API)
        body = json.loads(request.data) if request.data else None
        calls.append((method, path, body))
        for (want_method, needle), payload in routes:
            if method == want_method and re.search(re.escape(needle) + r"(?!\d)", path):
                return FakeResponse(payload(body) if callable(payload) else payload)
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def arm(monkeypatch, dry_run="false"):
    monkeypatch.setenv("GH_TOKEN", "ghp-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", FULL_NAME)
    monkeypatch.setenv("GITHUB_API_URL", API)
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"{API}/graphql")
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("DRY_RUN", dry_run)


def comment(marker: str, login: str = "pdt-bot") -> dict:
    return {"body": marker + "\n**PR tier**", "user": {"login": login}}


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
    floor, _ = select.rule_floor(pr(), [change("src/pdt/deploy_oracle.py", status="added")])
    assert floor == "architectural"


def test_pyproject_dependency_lines_are_architectural_and_a_version_bump_is_review():
    dep = patch_for(extra='+    "boto3",\n')
    assert select.path_floor("pyproject.toml", diff=dep)[0] == "architectural"
    bump = '@@ -1 +1 @@\n-version = "1.0"\n+version = "1.1"\n'
    assert select.path_floor("pyproject.toml", diff=bump) == ("review", "pyproject.toml changed")


def test_highest_floor_wins_and_reasons_name_the_file():
    floor, reasons = select.rule_floor(pr(), [change("README.md"), change("src/pdt/cli.py"),
                                              change("src/pdt/scaffold.py")])
    assert floor == "architectural"
    assert reasons == ["src/pdt/cli.py is the command line"]


def test_a_simple_floor_says_why():
    assert select.rule_floor(pr(), [change("README.md")]) == (
        "simple", ["only documentation, tests, or example apps changed"])


def test_a_few_tested_lines_in_a_covered_file_floor_at_simple():
    small = change("src/pdt/deploy_common.py", lines=4)
    test = change("tests/test_dockerfile.py")
    floor, reasons = select.rule_floor(pr(), [small, test])
    assert floor == "simple" and "src/pdt/deploy_common.py" in reasons[0]
    assert select.rule_floor(pr(), [small])[0] == "architectural"
    assert select.rule_floor(pr(), [small, change("ci/tests/test_x.py")])[0] == "architectural"
    five = change("src/pdt/deploy_common.py", lines=5)
    assert select.rule_floor(pr(), [five, test])[0] == "architectural"
    gated = change("src/pdt/cli.py", extra="+token = 1\n")
    assert select.rule_floor(pr(), [gated, test])[0] == "architectural"
    for path in ("src/pdt/utils/log.py", ".gitlab-ci.yml", "AGENTS.md", "pdt"):
        assert select.rule_floor(pr(), [change(path), test])[0] == "architectural", path


def test_deleted_renamed_and_large_files_bump_simple_to_review():
    no_patch = {"filename": "README.md", "status": "modified", "changes": 9000}
    for file in (change("README.md", status="removed"),
                 change("README.md", status="renamed", previous_filename="OLD.md"),
                 no_patch):
        floor, reasons = select.rule_floor(pr(), [file])
        assert floor == "review", file
        assert reasons and "README.md" in reasons[0], file
    pure_rename = {"filename": "docs/b.md", "status": "renamed", "changes": 0,
                   "previous_filename": "docs/a.md"}
    assert select.rule_floor(pr(), [pure_rename]) == ("review", ["docs/b.md is renamed"])


def test_a_truncated_diff_or_a_fork_bumps_to_review():
    assert select.rule_floor(pr(changed_files=12), [change("README.md")]) == (
        "review", ["GitHub truncated the diff"])
    assert select.rule_floor(pr(head_repo="stranger/pdt"), [change("README.md")]) == (
        "review", ["comes from a fork"])
    assert not select.same_repo(pr(head={"ref": "x", "sha": "s", "repo": None}))


def test_size_caps():
    assert select.rule_floor(pr(), [change("README.md", lines=200)])[0] == "simple"
    floor, reasons = select.rule_floor(pr(), [change("README.md", lines=201)])
    assert floor == "review" and "201 lines" in reasons[0]
    five = [change(f"docs/{n}.md") for n in range(5)]
    assert select.rule_floor(pr(changed_files=5), five)[0] == "simple"
    floor, reasons = select.rule_floor(pr(changed_files=6), five + [change("docs/6.md")])
    assert floor == "review" and "6 files" in reasons[0]


def test_hard_gate_words_bump_to_review_and_name_the_word():
    files = [change("tests/test_x.py", extra="+    token = 'abc'\n")]
    floor, reasons = select.rule_floor(pr(), files)
    assert floor == "review" and reasons == ["tests/test_x.py mentions `token`"]
    assert select.hard_gate_hits("+x = 1\n-y = os.environ['A']\n+++ b/x") == ["os.environ"]


def test_diff_id_ignores_file_order_and_tracks_content():
    a, b = change("a.md"), change("b.md")
    assert select.diff_id([a, b]) == select.diff_id([b, a])
    assert select.diff_id([a]) != select.diff_id([change("a.md", lines=2)])
    assert len(select.diff_id([a])) == 12


def test_marker_round_trip_and_newest_comment_by_the_token_user_wins():
    marker = select.format_marker("abc123def456", "simple", "review", "raised")
    assert select.parse_marker(marker + "\nmore") == {
        "diff": "abc123def456", "floor": "simple", "tier": "review", "claude": "raised"}
    assert check.format_marker("a", "b", "c", "d") == select.format_marker("a", "b", "c", "d")
    assert select.parse_marker("<!-- pdt-pr-score v1 diff=x -->") is None
    assert select.parse_marker("hello") is None
    comments = [comment(select.format_marker("old", "simple", "review", "raised")),
                {"body": "looks fine", "user": {"login": "someone"}},
                comment(select.format_marker("new", "simple", "simple", "agree")),
                comment(select.format_marker("forged", "simple", "simple", "agree"), "stranger")]
    assert select.previous_score(comments, "pdt-bot")["diff"] == "new"
    assert select.previous_score([{"body": "nothing", "user": {"login": "pdt-bot"}}],
                                 "pdt-bot") is None


def test_merge_blockers_name_each_gate():
    assert select.merge_blockers(pr(), GREEN, [], 0) == []
    assert select.merge_blockers(pr(draft=True, mergeable_state="draft"), GREEN, [], 0) == [
        "still a draft"]
    assert select.merge_blockers(pr(mergeable=False, mergeable_state="dirty"), GREEN, [], 0) == [
        "has conflicts"]
    assert select.merge_blockers(pr(), GREEN, [], 2) == ["unresolved review threads"]
    assert select.merge_blockers(pr(), [], [], 0) == ["no checks ran on the head commit"]
    assert select.merge_blockers(pr(), [run("test", conclusion="failure")], [], 0) == [
        "check test failure"]
    assert select.merge_blockers(pr(), [run("test", "in_progress", None)], [], 0) == [
        "check test in_progress"]
    assert select.merge_blockers(pr(), [run("lint", conclusion="skipped"), *GREEN], [], 0) == []
    pending = [{"context": "external", "state": "pending"}]
    assert select.merge_blockers(pr(), GREEN, pending, 0) == ["status external pending"]
    assert select.merge_blockers(pr(), [], [{"context": "e", "state": "success"}], 0) == []
    assert select.merge_blockers(pr(mergeable_state="blocked"), GREEN, [], 0) == [
        "merge state blocked"]
    assert select.merge_blockers(pr(mergeable=None, mergeable_state="unknown"), GREEN, [], 0) == [
        "merge state unknown"]
    assert select.merge_blockers(pr(mergeable_state="unstable"), GREEN, [], 0) == []
    assert select.merge_blockers(pr(head_repo="stranger/pdt"), GREEN, [], 0) == [
        "comes from a fork"]


def test_the_score_job_does_not_block_its_own_merge():
    own = run(select.OWN_CHECK, "in_progress", None)
    assert select.merge_blockers(pr(), [own, *GREEN], [], 0) == []
    assert select.merge_blockers(pr(), [own], [], 0) == ["no checks ran on the head commit"]


def test_a_draft_is_scored_once_and_again_only_when_ready_with_a_new_diff():
    scored_old = {"diff": "old", "floor": "simple", "tier": "simple", "claude": "agree"}
    unavailable = {**scored_old, "claude": "unavailable"}
    assert not select.already_scored(pr(draft=True), None, "new")
    assert select.already_scored(pr(draft=True), scored_old, "new")
    assert not select.already_scored(pr(draft=True), unavailable, "new")
    assert not select.already_scored(pr(draft=False), scored_old, "new")
    assert select.already_scored(pr(draft=False), scored_old, "old")


def test_next_link_follows_the_link_header():
    header = ('<https://api.github.com/x?page=2>; rel="next", '
              '<https://api.github.com/x?page=5>; rel="last"')
    assert select.next_link(header) == "https://api.github.com/x?page=2"
    assert select.next_link('<https://api.github.com/x?page=1>; rel="prev"') == ""
    assert select.next_link("") == ""


def test_paginate_reads_every_page(monkeypatch):
    pages = {f"{API}/x?per_page=100": ({"check_runs": [1, 2]},
                                        {"Link": f'<{API}/x?page=2>; rel="next"'}),
             f"{API}/x?page=2": ({"check_runs": [3]}, {})}
    monkeypatch.setattr(select, "http", lambda method, url, headers: pages[url])
    env = {"GH_TOKEN": "t", "GITHUB_API_URL": API}
    assert select.paginate(env, "/x?per_page=100", "check_runs") == [1, 2, 3]


def test_token_login_falls_back_to_the_actions_bot(monkeypatch):
    arm(monkeypatch)
    install(monkeypatch, [(("GET", "/user"), {"login": "pdt-bot"})])
    assert select.token_login(dict(select.os.environ)) == "pdt-bot"
    install(monkeypatch, [(("GET", "/user"), refuse(403))])
    assert select.token_login(dict(select.os.environ)) == "github-actions[bot]"


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
    body = check.comment_body("abc", arch, "http://job")
    assert body.splitlines()[0] == check.format_marker("abc", "review", "architectural", "raised")
    assert "`tier::architectural`" in body and "Claude raised it" in body
    assert "- touches deploy dispatch" in body and "http://job" in body
    assert check.MERGE_FOOTER not in body and check.FOOTER in body
    simple = check.comment_body("abc", check.Score("simple", "simple", [], "agree"), "")
    assert check.MERGE_FOOTER in simple


def test_label_change_leaves_exactly_one_tier_label():
    assert check.label_change([], "simple") == (["tier::simple"], [])
    assert check.label_change(["tier::review", "bug"], "simple") == (["tier::simple"],
                                                                     ["tier::review"])
    assert check.label_change(["tier::simple"], "simple") == ([], [])


# --- select hook end to end ---------------------------------------------------

def threads(*resolved: bool) -> dict:
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [{"isResolved": r} for r in resolved],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}


def run_select(monkeypatch, capsys, prs, comments, dry_run="false"):
    arm(monkeypatch, dry_run)
    fetches = []
    monkeypatch.setattr(select.subprocess, "run",
                        lambda cmd, **kw: fetches.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    # The list endpoint leaves out what the merge gates read; the single GET has it.
    slim = [{k: v for k, v in item.items()
             if not k.startswith("_") and k not in ("mergeable", "mergeable_state",
                                                    "changed_files")}
            for item in prs.values()]
    by_number = {number: item for number, item in prs.items()}
    routes = [(("GET", "/user"), {"login": "pdt-bot"}),
              (("GET", "/pulls?state=open"), slim),
              (("POST", "/graphql"), lambda body: threads(
                  *by_number[body["variables"]["number"]].get("_threads", ())))]
    for number, item in prs.items():
        base = f"/repos/{FULL_NAME}"
        routes += [
            (("GET", f"{base}/pulls/{number}/files"), item["_files"]),
            (("GET", f"{base}/issues/{number}/comments"), comments.get(number, [])),
            (("GET", f"{base}/commits/sha{number}/check-runs"),
             {"total_count": 1, "check_runs": item.get("_checks", GREEN)}),
            (("GET", f"{base}/commits/sha{number}/status"), {"state": "success", "statuses": []}),
            (("PUT", f"{base}/pulls/{number}/merge"), {"merged": True}),
            (("POST", f"{base}/issues/{number}/comments"), {"id": 1}),
            (("DELETE", f"{base}/git/refs/heads/branch-{number}"), {}),
            (("GET", f"{base}/pulls/{number}"),
             {k: v for k, v in item.items() if not k.startswith("_")}),
        ]
    calls = install(monkeypatch, routes)
    assert select.main() == 0
    out, err = capsys.readouterr()
    return json.loads(out), err, calls, fetches


def scored(files, tier, floor="simple", claude="agree", login="pdt-bot"):
    return [comment(select.format_marker(select.diff_id(files), floor, tier, claude), login)]


def test_select_merges_scored_simple_prs_and_emits_the_rest(monkeypatch, capsys):
    docs = [change("README.md")]
    prs = {1: {**pr(1), "_files": docs},
           2: {**pr(2), "_files": docs, "_checks": [run("test", conclusion="failure")]},
           3: {**pr(3), "_files": [change("docs/new.md")]},
           4: {**pr(4), "_files": [change("src/pdt/deploy_aws.py")]},
           5: {**pr(5), "_files": docs},
           6: {**pr(6, draft=True), "_files": docs},
           7: {**pr(7, draft=True), "_files": [change("docs/changed.md")]},
           8: {**pr(8), "_files": docs},
           9: {**pr(9, head_repo="stranger/pdt"), "_files": docs},
           10: {**pr(10), "_files": docs, "_threads": (True, False)}}
    comments = {1: scored(docs, "simple"), 2: scored(docs, "simple"),
                4: scored([change("src/pdt/deploy_aws.py")], "review"),
                5: scored(docs, "review", claude="unavailable"),
                6: scored(docs, "simple"), 7: scored(docs, "simple"),
                8: scored(docs, "simple", login="stranger"), 9: scored(docs, "simple"),
                10: scored(docs, "simple")}
    items, err, calls, fetches = run_select(monkeypatch, capsys, prs, comments)

    assert [item["number"] for item in items] == [3, 5, 8]
    assert "draft       #6" in err and "draft       #7" in err and "not merged" in err
    assert items[0]["floor"] == "simple" and items[0]["floor_reason_list"] == [
        "only documentation, tests, or example apps changed"]
    assert items[0]["files"] == "docs/new.md" and items[0]["previous"] is None
    assert items[1]["previous"]["claude"] == "unavailable"
    assert items[2]["previous"] is None, "a stranger's marker never counts"
    writes = [(m, p, b) for m, p, b in calls if m in ("PUT", "POST", "DELETE") and p != "/graphql"]
    assert writes == [
        ("PUT", f"/repos/{FULL_NAME}/pulls/1/merge", {"sha": "sha1", "merge_method": "merge"}),
        ("POST", f"/repos/{FULL_NAME}/issues/1/comments", {"body": select.MERGE_NOTE}),
        ("DELETE", f"/repos/{FULL_NAME}/git/refs/heads/branch-1", None),
    ]
    assert "merged      #1" in err and "blocked     #2" in err and "check test failure" in err
    assert "blocked     #10" in err and "unresolved review threads" in err
    assert ("GET", f"/repos/{FULL_NAME}/pulls/1", None) in calls
    assert "review      #4" in err and "score       #3" in err
    assert "fork        #9" in err
    assert not [p for _, p, _ in calls if "/9" in p], "a fork's PR is never read or written"
    assert fetches[0][:4] == ["git", "fetch", "--quiet", "origin"]
    assert "+refs/heads/branch-3:refs/remotes/origin/branch-3" in fetches[0]
    assert not [ref for ref in fetches[0] if "branch-9" in ref]


def test_select_dry_run_writes_nothing(monkeypatch, capsys):
    docs = [change("README.md")]
    items, err, calls, _ = run_select(monkeypatch, capsys, {1: {**pr(1), "_files": docs}},
                                      {1: scored(docs, "simple")}, dry_run="true")
    assert items == []
    assert all(method == "GET" or path == "/graphql" for method, path, _ in calls)
    assert "would merge #1" in err


def test_select_reports_a_refused_merge_and_continues(monkeypatch, capsys):
    arm(monkeypatch)
    monkeypatch.setattr(select.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    docs = [change("README.md")]
    install(monkeypatch, [
        (("GET", "/user"), {"login": "pdt-bot"}),
        (("GET", "/pulls?state=open"), [pr(1)]),
        (("PUT", "/pulls/1/merge"), refuse(403)),
        (("GET", "/pulls/1/files"), docs),
        (("GET", "/issues/1/comments"), scored(docs, "simple")),
        (("GET", "/check-runs"), {"check_runs": GREEN}),
        (("GET", "/status"), {"statuses": []}),
        (("POST", "/graphql"), threads()),
        (("GET", "/pulls/1"), pr(1)),
    ])
    assert select.main() == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == []
    assert "not merged  #1" in err and "GH_TOKEN may not be allowed" in err


def test_select_names_missing_variables(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", FULL_NAME)
    assert select.main() == 1
    assert "GH_TOKEN" in capsys.readouterr().err


# --- a pull_request run looks at one PR ---------------------------------------

def test_a_pull_request_run_scores_only_the_pr_in_the_event(monkeypatch, capsys):
    monkeypatch.setenv("PR_NUMBER", "2")
    prs = {1: {**pr(1), "_files": [change("docs/a.md")]},
           2: {**pr(2), "_files": [change("docs/b.md")]},
           3: {**pr(3), "_files": [change("docs/c.md")]}}
    items, err, calls, fetches = run_select(monkeypatch, capsys, prs, {})
    assert [item["number"] for item in items] == [2]
    assert "pull_request event for #2: looking at that PR only" in err
    assert "1 open PR(s)" in err
    touched = {p for _, p, _ in calls if p.startswith(f"/repos/{FULL_NAME}/") and "state=open" not in p}
    assert touched == {f"/repos/{FULL_NAME}/pulls/2",
                       f"/repos/{FULL_NAME}/pulls/2/files?per_page=100",
                       f"/repos/{FULL_NAME}/issues/2/comments?per_page=100"}
    assert fetches == [["git", "fetch", "--quiet", "origin",
                        "+refs/heads/main:refs/remotes/origin/main",
                        "+refs/heads/branch-2:refs/remotes/origin/branch-2"]]


def test_a_pull_request_run_for_a_pr_that_is_no_longer_open_lists_nothing(monkeypatch, capsys):
    monkeypatch.setenv("PR_NUMBER", "9")
    items, err, calls, fetches = run_select(monkeypatch, capsys, {1: {**pr(1), "_files": []}}, {})
    assert items == [] and fetches == []
    assert "not an open PR of this repository" in err
    assert [p for _, p, _ in calls] == [
        "/user", f"/repos/{FULL_NAME}/pulls?state=open&base=main&per_page=100"]


def test_a_run_without_an_event_sweeps_every_open_pr(monkeypatch, capsys):
    monkeypatch.delenv("PR_NUMBER", raising=False)
    prs = {1: {**pr(1), "_files": [change("docs/a.md")]},
           2: {**pr(2), "_files": [change("docs/b.md")]}}
    items, err, _, _ = run_select(monkeypatch, capsys, prs, {})
    assert [item["number"] for item in items] == [1, 2]
    assert "pull_request event" not in err
    monkeypatch.setenv("PR_NUMBER", "  ")
    items, _, _, _ = run_select(monkeypatch, capsys, prs, {})
    assert [item["number"] for item in items] == [1, 2]


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
    base = select.item_for(pr(3), "abc123def456", "simple",
                           ["only documentation, tests, or example apps changed"],
                           [change("README.md")], None)
    return {**base, **overrides}


def verdict(tier, *reasons):
    return {"result": f'```json\n{json.dumps({"tier": tier, "reasons": list(reasons)})}\n```'}


BASE = f"/repos/{FULL_NAME}"


def label_routes(exists: bool):
    return [(("POST", f"{BASE}/issues/3/labels"), []),
            (("DELETE", f"{BASE}/issues/3/labels/"), []),
            (("POST", f"{BASE}/issues/3/comments"), {}),
            (("GET", f"{BASE}/labels/"), {"name": "x"} if exists else refuse(404)),
            (("POST", f"{BASE}/labels"), {})]


def test_check_creates_the_label_sets_it_and_comments(monkeypatch, capsys):
    status, err, calls = run_check(monkeypatch, capsys, item_for_check(),
                                   verdict("review", "agree with floor", "README.md rewords"),
                                   label_routes(exists=False))
    assert status == {"status": "ok", "message": "tier::review"}
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", f"{BASE}/labels/tier%3A%3Areview"),
        ("POST", f"{BASE}/labels"),
        ("POST", f"{BASE}/issues/3/labels"),
        ("POST", f"{BASE}/issues/3/comments"),
    ]
    assert calls[1][2] == {"name": "tier::review", "color": "d4a72c",
                           "description": "pdt PR score: review"}
    assert calls[2][2] == {"labels": ["tier::review"]}
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
    _, _, calls = run_check(monkeypatch, capsys, item, verdict("simple", "agree with floor"),
                            label_routes(exists=True))
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", f"{BASE}/labels/tier%3A%3Asimple"),
        ("POST", f"{BASE}/issues/3/labels"),
        ("DELETE", f"{BASE}/issues/3/labels/tier%3A%3Areview"),
        ("POST", f"{BASE}/issues/3/comments"),
    ]
    assert calls[1][2] == {"labels": ["tier::simple"]}


def test_check_reports_architectural_as_needs_human(monkeypatch, capsys):
    status, _, _ = run_check(monkeypatch, capsys, item_for_check(),
                             verdict("architectural", "README.md documents a removed command"),
                             label_routes(exists=True))
    assert status == {"status": "needs_human",
                      "message": "needs discussion: README.md documents a removed command"}


def test_check_without_a_verdict_scores_review_and_needs_a_human(monkeypatch, capsys):
    status, _, calls = run_check(monkeypatch, capsys, item_for_check(),
                                 {"is_error": True, "result": "budget exceeded"},
                                 label_routes(exists=True))
    assert status["status"] == "needs_human" and "no verdict" in status["message"]
    assert calls[1][2] == {"labels": ["tier::review"]}
    assert "claude=unavailable" in calls[2][2]["body"]


def test_check_dry_run_writes_nothing_but_still_reports(monkeypatch, capsys):
    status, err, calls = run_check(monkeypatch, capsys, item_for_check(),
                                   verdict("review", "x"), dry_run="true")
    assert status["status"] == "ok" and calls == []
    assert "would set label tier::review" in err and "would post" in err


# --- task folder and workflow -------------------------------------------------

def test_shipped_score_task_loads_and_is_read_only():
    task = runner.load_task("score-prs")
    assert task.model == "opus"
    assert task.select.name == "select_prs.py" and task.check.name == "check_prs.py"
    assert task.permission_mode == "dontAsk"
    assert not any(tool in task.allowed_tools for tool in ("Edit", "Write"))
    assert not any("push" in tool for tool in task.allowed_tools)
    item = select.item_for(pr(9), "abc", "simple", ["docs"], [change("README.md")], None)
    prompt = runner.render_prompt(task.prompt, item)
    assert "#9" in prompt and "origin/main...origin/branch-9" in prompt
    assert '{"tier": "simple" | "review" | "architectural"' in prompt


def workflow(name: str) -> dict:
    loaded = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())
    loaded["on"] = loaded.pop(True)  # YAML 1.1 reads the bare key on as true
    return loaded


def test_score_prs_workflow_runs_on_the_three_pr_events_a_schedule_and_by_hand():
    flow = workflow("score-prs.yml")
    assert flow["on"]["pull_request"] == {"types": ["opened", "reopened", "ready_for_review"]}
    assert flow["on"]["schedule"]
    assert flow["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert flow["concurrency"] == {"group": "score-prs", "cancel-in-progress": False}
    assert flow["permissions"] == {"contents": "write", "pull-requests": "write",
                                   "issues": "write", "checks": "read", "statuses": "read"}
    assert list(flow["jobs"]) == [select.OWN_CHECK]
    job = flow["jobs"][select.OWN_CHECK]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert "secrets.PDT_BOT_TOKEN || github.token" in job["env"]["GH_TOKEN"]
    assert "secrets.CLAUDE_TOKEN" in job["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert "default_branch" in checkout["with"]["ref"], "the rules come from the default branch"
    score = next(step for step in job["steps"] if "claude_task.py" in step.get("run", ""))
    assert "score-prs" in score["run"] and "--dry-run" in score["run"]
    assert score["run"].rstrip().endswith("|| [ $? -eq 2 ]")
    upload = job["steps"][-1]
    assert upload["if"] == "always()" and upload["with"]["retention-days"] == 30


def test_gitlab_ci_runs_no_claude_task():
    ci = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text())
    for name in (".claude-task", "rebase-mrs", "score-mrs", "score-mrs-skip",
                 "close_stale_drafts"):
        assert name not in ci, name
