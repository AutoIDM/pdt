import coverage as coverage_check
import yaml


def test_the_inventory_names_every_behavior():
    keys, providers = coverage_check.config_keys()
    data = yaml.safe_load(coverage_check.INVENTORY.read_text())
    targets = coverage_check.scenario_names() | coverage_check.app_names()
    assert coverage_check.check(data, coverage_check.cli_commands(),
                                keys | coverage_check.EXTRA_KEYS, providers, targets) == []
    assert coverage_check.matrix_problems(providers) == []
    assert coverage_check.ci_problems() == []


def test_a_missing_row_and_an_unknown_target_are_reported():
    data = {"commands": {"deploy": {"covered_by": ["nowhere"]},
                         "gone": {"uncovered": "retired"}},
            "keys": {"name": {}},
            "providers": {"aws": {"covered_by": ["lifecycle"], "uncovered": "both"}}}
    problems = coverage_check.check(data, {"deploy", "destroy"}, {"name"}, {"aws"},
                                    {"lifecycle"})
    expected = [
        "commands: destroy has no row in coverage.yml",
        "commands: gone is in coverage.yml but not in the code",
        "commands: deploy names nowhere, which is neither a scenario nor an app",
        "keys: name needs covered_by or uncovered, not both or neither",
        "providers: aws needs covered_by or uncovered, not both or neither",
    ] + [f"rules: {name} has no row in coverage.yml"
         for name in sorted(coverage_check.REQUIRED_RULES)]
    assert problems == expected


def test_ci_rejects_a_global_resource_namespace(monkeypatch, tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text("""
variables:
  PDT_RESOURCE_NAMESPACE: $CI_PIPELINE_ID
.verify:
  variables:
    PDT_RESOURCE_NAMESPACE: $CI_PIPELINE_ID
""")
    monkeypatch.setattr(coverage_check, "PROJECT", tmp_path)
    assert "ci: the resource namespace must not affect non-provider jobs" in (
        coverage_check.ci_problems())
