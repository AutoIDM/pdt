from pdt.deploy import confirm
from pdt.deploy_common import CostEstimate


def test_confirm_prints_plan_then_cost(capsys):
    cost = CostEstimate([("Task Scheduler on this Windows computer", 0.0)],
                        "no cloud charges")
    assert confirm(["create task pdt-x"], assume_yes=True, cost=cost)
    out = capsys.readouterr().out
    assert out.index("Plan:") < out.index("create task pdt-x") < out.index("Estimated monthly cost")
    assert "total" in out and "$USD   0.00" in out
