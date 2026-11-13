from conftest import add_app
from pdt import config, profiles
from pdt.config import merged_app, validate_app
from pdt.profiles import job, web

GOOGLE_CLOUD = "platform:\n  provider: google-cloud\n"
CLOUD_WEB_APP = """\
app_profile: web
web:
  port: 8050
  auth:
    provider: google
    allowed_domains: [example.com]
storage:
  read: [warehouse]
"""
WEB_APP = CLOUD_WEB_APP


def test_an_app_with_no_profile_is_a_job(project):
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["app_profile"] == "job"
    assert merged_app("my-report")["storage"] is True
    assert merged_app("my-report")["storage_read"] == []
    assert validate_app("my-report") == []


def test_a_web_app_validates(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", WEB_APP)
    assert validate_app("dashboard") == []
    app = merged_app("dashboard")
    assert app["app_profile"] == "web"
    assert app["storage"] is True
    assert app["storage_read"] == ["warehouse"]
    assert web.port(app) == 8050


def test_an_unknown_profile_is_refused(project):
    add_app(project, "my-report", "app_profile: batch\n")
    assert any("app_profile must be one of: job, web" in p for p in validate_app("my-report"))


def test_a_web_app_rejects_a_schedule(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "dashboard", WEB_APP + "schedule: hourly\n")
    assert any("a web app runs all the time; remove the key" in p
               for p in validate_app("dashboard"))


def test_a_job_rejects_the_web_and_auth_sections(project):
    add_app(project, "my-report", "schedule: daily\nweb:\n  port: 80\n")
    assert any("only an app with app_profile: web uses this section" in p
               for p in validate_app("my-report"))


def test_a_web_app_says_who_may_use_it(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", WEB_APP.replace("  auth:\n    provider: google\n"
                                                  "    allowed_domains: [example.com]\n", ""))
    assert any("web.auth.provider must be google" in p
               for p in validate_app("dashboard"))
    add_app(project, "dashboard", WEB_APP.replace("provider: google", "provider: github"))
    assert any("web.auth.provider must be google" in p
               for p in validate_app("dashboard"))


def test_a_web_app_is_never_open_to_everyone(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", CLOUD_WEB_APP.replace("provider: google", "provider: public"))
    assert any("web.auth.provider must be google" in p
               for p in validate_app("dashboard"))
    add_app(project, "dashboard", "app_profile: web\nweb:\n  auth: public\n")
    assert any("web.auth: must be a mapping" in p for p in validate_app("dashboard"))
    add_app(project, "dashboard", "app_profile: web\nweb:\n  auth:\n    provider: google\n")
    assert any("list who may enter in allowed_domains, allowed_groups, or both" in p
               for p in validate_app("dashboard"))


def test_web_resources_are_checked(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    base = CLOUD_WEB_APP.replace("web:\n", "web:\n  resources:\n    RES\n", 1)
    add_app(project, "dashboard", base.replace("RES", "cpu: 2\n    memory: 4GiB\n    timeout: 900"))
    assert validate_app("dashboard") == []
    assert web.resources(merged_app("dashboard"))["max_instances"] == 3
    add_app(project, "dashboard", base.replace("RES", "cpu: 1\n    memory: 8GiB"))
    assert any("1 vCPU needs from 512MiB to 4GiB" in p for p in validate_app("dashboard"))
    add_app(project, "dashboard", base.replace("RES", "cpu: 4"))
    assert any("4 vCPU needs from 2GiB to 16GiB" in p for p in validate_app("dashboard"))
    add_app(project, "dashboard", base.replace("RES", "cpu: 3\n    memory: 1TB\n    disk: 9"))
    problems = validate_app("dashboard")
    assert any("web.resources.cpu must be one of 1, 2, 4, 6, 8" in p for p in problems)
    assert any("web.resources.memory must be a size" in p for p in problems)
    assert any("disk" in p for p in problems)
    add_app(project, "dashboard", base.replace("RES", "min_instances: 4\n    max_instances: 2"))
    assert any("min_instances must not be more than max_instances" in p
               for p in validate_app("dashboard"))


def test_each_cloud_gate_is_a_known_pair(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", CLOUD_WEB_APP)
    assert validate_app("dashboard") == []
    app = merged_app("dashboard")
    assert web.gate(app, "google-cloud") == "Identity-Aware Proxy"
    assert web.health_path(app) == "/"
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    assert any("not yet available on the azure provider" in p for p in validate_app("dashboard"))
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    assert any("not yet available on the windows provider" in p for p in validate_app("dashboard"))



def test_a_web_app_is_refused_on_aws_for_now(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  account: '123456789012'\n")
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", CLOUD_WEB_APP)
    assert any("not yet available on the aws provider" in p for p in validate_app("dashboard"))


def test_a_container_with_a_config_file_is_a_web_app(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    folder = project / "site"
    folder.mkdir()
    (folder / "Dockerfile").write_text("FROM nginx:alpine\n")
    (folder / "pdt.yml").write_text("app_profile: web\nweb:\n  port: 80\n  auth:\n"
                                    "    provider: google\n    allowed_domains: [example.com]\n")
    assert "site" in config.app_folders()
    assert validate_app("site") == []
    assert web.port(merged_app("site")) == 80
    (folder / "pdt.yml").unlink()
    assert "site" not in config.app_folders()


def test_storage_read_must_name_another_app_in_the_project(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "dashboard", WEB_APP.replace("[warehouse]", "[dashboard, nowhere]"))
    problems = validate_app("dashboard")
    assert any("storage.read names 'dashboard', which is this app" in p for p in problems)
    assert any("'nowhere', which is not an app of this project" in p for p in problems)


def test_storage_mapping_shape_is_checked(project):
    add_app(project, "my-report", "schedule: daily\nstorage:\n  enabled: maybe\n  read: warehouse\n"
                                  "  size: 3\n")
    problems = validate_app("my-report")
    assert any("storage.enabled must be true or false" in p for p in problems)
    assert any("storage.read must be a list of app names" in p for p in problems)
    assert any("unknown key 'size'" in p for p in problems)




def test_the_job_profile_is_the_default_and_owns_the_schedule():
    assert profiles.DEFAULT == job.NAME
    assert "schedule" in job.KEYS
    assert job.KEYS.isdisjoint(web.KEYS)


def test_a_top_level_auth_section_points_at_web(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "dashboard", "app_profile: web\nauth: public\n")
    assert any("auth" in p and "web.auth" in p for p in validate_app("dashboard"))


def test_a_job_takes_its_schedule_from_the_job_section_or_the_top_level(project):
    add_app(project, "report", "job:\n  schedule: hourly\n  timezone: America/Chicago\n")
    app = merged_app("report")
    assert app["schedule"] == "hourly" and app["timezone"] == "America/Chicago"
    assert validate_app("report") == []
    add_app(project, "old", "schedule: daily\n")
    assert merged_app("old")["schedule"] == "daily" and validate_app("old") == []
    add_app(project, "report", "job:\n  schedule: hourly\n  retries: 3\n")
    assert any("unknown key 'retries'" in p for p in validate_app("report"))
