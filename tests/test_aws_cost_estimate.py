from pdt import deploy_aws_batch


def test_redeploy_estimate_reads_recent_runs_from_the_logs_client(monkeypatch, waits):
    seen = []

    def recent_stream_seconds(logs, log_group):
        seen.append((logs, list(waits)))
        return 120.0

    monkeypatch.setattr(deploy_aws_batch, "recent_stream_seconds", recent_stream_seconds)
    monkeypatch.setattr(deploy_aws_batch, "list_price", lambda *args, **kwargs: 0.0)
    fake_logs = object()

    deploy_aws_batch.cost_estimate_for(
        fake_logs, {"log_group": "/pdt/app"}, "us-east-1", "0 0 * * *", True, None, "USD")

    assert seen == [(fake_logs,
                     ["reading: past runs in CloudWatch log group [bold not dim]/pdt/app[/]"])]
