import coverage as coverage_check
import yaml


def test_the_inventory_names_every_behavior():
    keys, providers = coverage_check.config_keys()
    data = yaml.safe_load(coverage_check.INVENTORY.read_text())
    targets = coverage_check.scenario_names() | coverage_check.app_names()
    assert coverage_check.check(data, coverage_check.cli_commands(),
                                keys | coverage_check.EXTRA_KEYS, providers, targets) == []


def test_a_missing_row_and_an_unknown_target_are_reported():
    data = {"commands": {"deploy": {"covered_by": ["nowhere"]},
                         "gone": {"uncovered": "retired"}},
            "keys": {"name": {}},
            "providers": {"aws": {"covered_by": ["lifecycle"], "uncovered": "both"}}}
    problems = coverage_check.check(data, {"deploy", "destroy"}, {"name"}, {"aws"},
                                    {"lifecycle"})
    assert problems == [
        "commands: destroy has no row in coverage.yml",
        "commands: gone is in coverage.yml but not in the code",
        "commands: deploy names nowhere, which is neither a scenario nor an app",
        "keys: name needs covered_by or uncovered, not both or neither",
        "providers: aws needs covered_by or uncovered, not both or neither",
    ]
