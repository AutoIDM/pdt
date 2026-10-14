import botocore.credentials
import pytest

from pdt import deploy_aws


def test_an_aws_login_profile_without_awscrt_names_the_workaround(tmp_path, monkeypatch, capsys):
    aws_config = tmp_path / "config"
    aws_config.write_text("[profile console]\n"
                          "login_session = arn:aws:iam::123456789012:user/admin\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setattr(botocore.credentials, "EC", None)
    with pytest.raises(SystemExit) as exit_info:
        deploy_aws.ensure_session({"platform": {"region": "us-east-1", "profile": "console"}})
    assert exit_info.value.code == 1
    said = capsys.readouterr().out
    assert "AWS profile console signs in with aws login" in said
    assert "[profile console-export]" in said
    assert "credential_process = aws configure export-credentials --profile console" in said
