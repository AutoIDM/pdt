import pytest

from pdt import deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud


def stop(*args):
    raise SystemExit(1)


@pytest.mark.parametrize("module,stubs,label", [
    (deploy_azure_container_apps, {"azure_settings": lambda app: {}, "preflight": stop}, "Azure"),
    (deploy_aws_batch, {"ensure_session": stop}, "AWS"),
    (deploy_google_cloud, {"project_region": lambda app: ("p", "r"), "preflight": stop},
     "Google Cloud"),
])
@pytest.mark.parametrize("command", [
    lambda module: module.destroy({"name": "report"}, True),
    lambda module: module.secrets({"name": "report"}, "diff", True),
])
def test_the_sign_in_check_says_what_it_does(monkeypatch, capsys, module, stubs, label, command):
    for name, stub in stubs.items():
        monkeypatch.setattr(module, name, stub)
    with pytest.raises(SystemExit):
        command(module)
    assert f"Checking your {label} sign-in..." in capsys.readouterr().out
