from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / "verify" / ".gitlab-ci.yml").read_text()


def test_azure_verify_rejects_an_unset_client_secret():
    assert 'if [ -z "${AZURE_CLIENT_SECRET:-}" ]' in CI
    assert "ERROR: AZURE_CLIENT_SECRET is not set" in CI


def test_azure_verify_passes_dash_leading_secrets_as_one_argument():
    assert (
        'pdt az login --service-principal -u "$AZURE_CLIENT_ID" '
        '--password="$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID"'
        in CI
    )
