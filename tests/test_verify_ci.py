from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def azure_before_script():
    config = yaml.safe_load((ROOT / "verify" / ".gitlab-ci.yml").read_text())
    return config["verify:azure"]["before_script"]


def test_azure_verify_rejects_an_unset_client_secret():
    assert any(
        'if [ -z "${AZURE_CLIENT_SECRET:-}" ]' in command
        and "ERROR: AZURE_CLIENT_SECRET is not set" in command
        for command in azure_before_script()
        if isinstance(command, str)
    )


def test_azure_verify_passes_dash_leading_secrets_as_one_argument():
    assert (
        'pdt az login --service-principal -u "$AZURE_CLIENT_ID" '
        '--password="$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID"'
        in azure_before_script()
    )
