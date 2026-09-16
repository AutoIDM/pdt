from pdt.deploy import confirm
from pdt.deploy_common import CostEstimate
from pdt import deploy_aws_fargate


def test_confirm_prints_plan_then_cost(capsys):
    cost = CostEstimate([("Task Scheduler on this Windows computer", 0.0)],
                        "no cloud charges")
    assert confirm(["create task pdt-x"], assume_yes=True, cost=cost)
    out = capsys.readouterr().out
    assert out.index("Plan:") < out.index("create task pdt-x") < out.index("Estimated monthly cost")
    assert "total" in out and "$   0.00" in out


def test_aws_scheduled_store_cost_estimate_reads_the_logs_client(monkeypatch):
    logs = object()
    calls = []
    monkeypatch.setattr(deploy_aws_fargate, "recent_stream_seconds",
                        lambda client, name: calls.append((client, name)) or 60)
    monkeypatch.setattr(deploy_aws_fargate, "list_price", lambda *args, **kwargs: 0)

    deploy_aws_fargate.cost_estimate_for(
        logs, {"log_group": "/ecs/pdt-report"}, "us-east-1", "0 * * * *", True, (0, 0))

    assert calls == [(logs, "/ecs/pdt-report")]
