"""force_merge.py: when a labelled MR gets a new pipeline, and which events run it."""

import importlib.util
import sys
from pathlib import Path

from test_score_task import job_runs, trigger

SCRIPT = Path(__file__).resolve().parent.parent / "force_merge.py"

spec = importlib.util.spec_from_file_location("force_merge", SCRIPT)
force = importlib.util.module_from_spec(spec)
sys.modules["force_merge"] = force
spec.loader.exec_module(force)


def mr(labels=("force-merge",), state="opened", pipeline=True):
    return {"iid": 96, "state": state, "labels": list(labels),
            "head_pipeline": {"id": 1} if pipeline else None}


GATED = [{"name": "build"}, {"name": "verify"}, {"name": "verify:aws"}]
BYPASSED = [{"name": "build"}, {"name": "test"}]


def test_a_labelled_mr_whose_pipeline_holds_the_gate_gets_a_new_pipeline():
    assert force.decision(mr(), GATED)


def test_a_pipeline_without_the_gate_is_left_alone():
    # The bypass already happened, or the MR touches no verify path.
    assert force.decision(mr(), BYPASSED) == ""


def test_no_label_means_nothing_happens():
    assert force.decision(mr(labels=["tier::review"]), GATED) == ""


def test_a_closed_or_merged_mr_is_left_alone():
    assert force.decision(mr(state="merged"), GATED) == ""
    assert force.decision(mr(state="closed"), GATED) == ""


def test_a_labelled_mr_with_no_pipeline_gets_one():
    assert force.decision(mr(pipeline=False), []) == "no pipeline yet"


def test_the_job_runs_on_every_webhook_update_event_and_nothing_else():
    # A label change arrives as an update with no Draft change.
    assert job_runs("force-merge", trigger("update"))
    assert job_runs("force-merge", trigger("update", "true", "false"))
    for variables in (trigger("open"), trigger("reopen"), trigger("close"), trigger("merge")):
        assert not job_runs("force-merge", variables), variables
    assert not job_runs("force-merge", {"CI_PIPELINE_SOURCE": "schedule", "mode": "score_mrs"})
    assert not job_runs("force-merge", {"CI_PIPELINE_SOURCE": "merge_request_event"})


def test_settings_need_the_mr_number():
    env = {"GITLAB_TOKEN": "t", "CI_PROJECT_ID": "1"}
    assert force.settings_from_env(env) is None
    settings = force.settings_from_env({**env, "MR_IID": "96", "DRY_RUN": "false"})
    assert settings.iid == "96" and not settings.dry_run
    assert force.settings_from_env({**env, "MR_IID": "96"}).dry_run
