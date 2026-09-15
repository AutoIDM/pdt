from conftest import add_app

from pdt.config import validate, validate_app


def test_platform_inside_config_says_where_it_belongs(project):
    add_app(project, "my-report", "config:\n  platform:\n    provider: aws\n")
    problems = validate_app("my-report")
    assert problems == [
        "my-report/config.yml: config: 'platform' belongs in the top level of pdt.yml "
        "or of the app's config.yml, not here"]


def test_schedule_inside_the_root_platform_block_says_where_it_belongs(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  schedule: daily\n")
    add_app(project, "my-report")
    assert validate() == [
        "pdt.yml: platform: 'schedule' belongs in the top level of the app's config.yml, "
        "or its apps: entry in pdt.yml, not here"]


def test_platform_key_at_the_top_of_config_yml_says_where_it_belongs(project):
    add_app(project, "my-report", "region: eastus\n")
    assert validate_app("my-report") == [
        "my-report/config.yml: 'region' belongs in the platform: section, not here"]


def test_env_list_inside_platform_says_where_it_belongs(project):
    add_app(project, "my-report", "platform:\n  required: [A]\n")
    assert validate_app("my-report") == [
        "my-report/config.yml: platform: 'required' belongs in the env: section, not here"]


def test_unknown_keys_are_errors_in_every_section(project):
    (project / "pdt.yml").write_text(
        "colour: red\nplatform:\n  provider: azure\n  colour: red\n"
        "apps:\n  - name: my-report\n    colour: red\n    env:\n      colour: red\n")
    add_app(project, "my-report", "colour: red\nplatform:\n  colour: red\n")
    assert validate() == [
        "pdt.yml: unknown key 'colour'",
        "pdt.yml: platform: unknown key 'colour'",
        "pdt.yml: apps entry 'my-report': unknown key 'colour'",
        "pdt.yml: apps entry 'my-report': env: unknown key 'colour'",
        "my-report/config.yml: unknown key 'colour'",
        "my-report/config.yml: platform: unknown key 'colour'",
    ]


def test_a_section_that_is_not_a_mapping_is_an_error_not_a_crash(project):
    add_app(project, "my-report", "platform: aws\nenv:\n  required: A\n")
    assert validate_app("my-report") == [
        "my-report/config.yml: platform: must be a mapping of key: value lines",
        "my-report/config.yml: env: required must be a list of env var names",
    ]


def test_apps_must_be_a_list(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\napps:\n  my-report: {}\n")
    add_app(project, "my-report")
    assert validate() == ["pdt.yml: apps must be a list, one `- name: <app>` per app"]


def test_free_form_config_keys_still_pass(project):
    add_app(project, "my-report", "config:\n  region: mars\n  name: x\n  greeting: hi\n")
    assert validate_app("my-report") == []
