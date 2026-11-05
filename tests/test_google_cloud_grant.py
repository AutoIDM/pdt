import subprocess

from test_google_cloud_build import BUILDER, destroy_app, destroy_fake

from pdt import deploy_google_cloud as google
from pdt import grant_templates

GRANT = "pdt-autoidm@my-project.iam.gserviceaccount.com"
RUNNER = "pdt-runner@my-project.iam.gserviceaccount.com"


def script_calls(tmp_path) -> list[str]:
    harness = tmp_path / "grant.sh"
    harness.write_text(
        'gcloud() { case "$1 $2" in "projects describe") echo 123456789012 ;; *) case " $* " in\n'
        '  *" describe "*) return 1 ;; esac; echo "$*" ;; esac; }\n'
        "PDT_PROJECT=my-project PDT_EXTERNAL_ID=0123 PDT_AWS_ACCOUNT=1 PDT_AWS_ROLE=r\n"
        + grant_templates.google_script())
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def test_the_google_grant_makes_the_builder_and_may_act_as_both_job_accounts(tmp_path):
    calls = script_calls(tmp_path)
    created = [call for call in calls if call.startswith("iam service-accounts create")]
    assert any(call.split()[3] == google.BUILDER
               and f"--display-name {google.BUILDER_DISPLAY_NAME} " in call for call in created)
    acts_as = {call.split()[3] for call in calls
               if call.startswith("iam service-accounts add-iam-policy-binding")
               and f"--member serviceAccount:{GRANT}" in call
               and "--role roles/iam.serviceAccountUser" in call}
    assert acts_as == {RUNNER, BUILDER}


def test_destroy_keeps_the_job_accounts_while_the_grant_uses_them(project, monkeypatch, capsys):
    fake = destroy_fake()
    fake.found[("iam", "service-accounts", "list")] += [
        {"email": RUNNER, "displayName": "pdt job runner"},
        {"email": GRANT, "displayName": "AutoIDM access for pdt"}]
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    assert fake.ran("iam", "service-accounts", "delete") == []
    out = capsys.readouterr().out
    assert BUILDER in out and RUNNER in out
