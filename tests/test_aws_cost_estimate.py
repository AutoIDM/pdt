from pdt import deploy_aws_batch
from pdt.deploy_common import CostEstimate


def test_redeploy_estimate_reads_recent_runs_from_the_logs_client(monkeypatch):
    seen = []

    def recent_stream_seconds(logs, log_group):
        seen.append(logs)
        return 120.0

    monkeypatch.setattr(deploy_aws_batch, "recent_stream_seconds", recent_stream_seconds)
    monkeypatch.setattr(deploy_aws_batch, "list_price", lambda *args, **kwargs: 0.0)
    fake_logs = object()

    estimate = deploy_aws_batch.cost_estimate_for(
        fake_logs, {"log_group": "/pdt/app"}, "us-east-1", "0 0 * * *", True, None)

    assert seen == [fake_logs]
    assert isinstance(estimate, CostEstimate)
