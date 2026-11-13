from conftest import add_app
from pdt import config, profiles
from pdt.config import merged_app, validate_app
from pdt.profiles import job, web

WINDOWS = "platform:\n  provider: windows\n"
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
WEB_APP = """\
app_profile: web
web:
  port: 8050
  hostname: sales.example.com
  tls_cert: certs/sales.pem
  tls_key: certs/sales.key
  auth:
    provider: google
    allowed_domains: [example.com]
storage:
  read: [warehouse]
"""


def test_an_app_with_no_profile_is_a_job(project):
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["app_profile"] == "job"
    assert merged_app("my-report")["storage"] is True
    assert merged_app("my-report")["storage_read"] == []
    assert validate_app("my-report") == []


def test_a_web_app_on_windows_validates(project):
    (project / "pdt.yml").write_text(WINDOWS)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", WEB_APP)
    assert validate_app("dashboard") == []
    app = merged_app("dashboard")
    assert app["app_profile"] == "web"
    assert app["storage"] is True
    assert app["storage_read"] == ["warehouse"]
    assert web.port(app) == 8050
    assert web.url(app) == "https://sales.example.com/"


def test_an_unknown_profile_is_refused(project):
    add_app(project, "my-report", "app_profile: batch\n")
    assert any("app_profile must be one of: job, web" in p for p in validate_app("my-report"))


def test_a_web_app_rejects_a_schedule(project):
    (project / "pdt.yml").write_text(WINDOWS)
    add_app(project, "dashboard", WEB_APP + "schedule: hourly\n")
    assert any("a web app runs all the time; remove the key" in p
               for p in validate_app("dashboard"))


def test_a_job_rejects_the_web_and_auth_sections(project):
    add_app(project, "my-report", "schedule: daily\nweb:\n  port: 80\n")
    assert any("only an app with app_profile: web uses this section" in p
               for p in validate_app("my-report"))


def test_a_web_app_says_who_may_use_it(project):
    (project / "pdt.yml").write_text(WINDOWS)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", WEB_APP.replace("  auth:\n    provider: google\n"
                                                  "    allowed_domains: [example.com]\n", ""))
    assert any("web.auth: public serves everyone" in p for p in validate_app("dashboard"))
    add_app(project, "dashboard", WEB_APP.replace("provider: google", "provider: github"))
    assert any("web.auth.provider must be public, google, or microsoft" in p
               for p in validate_app("dashboard"))


def test_a_public_web_app_has_no_allow_lists(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", CLOUD_WEB_APP.replace("provider: google", "provider: public"))
    assert any("a public app has no sign-in" in p for p in validate_app("dashboard"))
    add_app(project, "dashboard", "app_profile: web\nweb:\n  auth: public\n")
    assert validate_app("dashboard") == []
    app = merged_app("dashboard")
    assert app["auth"] == {"provider": "public"} and web.is_public(app)
    assert web.required_env(app) == []


def test_each_cloud_gate_is_a_known_pair(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", CLOUD_WEB_APP)
    assert validate_app("dashboard") == []
    app = merged_app("dashboard")
    assert web.gate(app, "google-cloud") == "Identity-Aware Proxy"
    assert web.required_env(app) == []
    assert web.health_path(app) == "/" and web.url(app) == ""
    add_app(project, "dashboard", CLOUD_WEB_APP.replace("provider: google", "provider: microsoft"))
    assert any("microsoft sign-in is not yet available on the google-cloud provider" in p
               for p in validate_app("dashboard"))
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    assert any("not yet available on the azure provider" in p for p in validate_app("dashboard"))


def test_a_web_app_on_windows_needs_a_hostname_and_a_certificate(project):
    (project / "pdt.yml").write_text(WINDOWS)
    add_app(project, "dashboard", "app_profile: web\nweb:\n  auth:\n    provider: google\n"
                                  "    allowed_domains: [example.com]\n")
    assert any("needs hostname, tls_cert, tls_key" in p for p in validate_app("dashboard"))


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
    (folder / "pdt.yml").write_text("app_profile: web\nweb:\n  port: 80\n  auth: public\n")
    assert "site" in config.app_folders()
    assert validate_app("site") == []
    assert web.port(merged_app("site")) == 80
    (folder / "pdt.yml").unlink()
    assert "site" not in config.app_folders()


def test_storage_read_must_name_another_app_in_the_project(project):
    (project / "pdt.yml").write_text(WINDOWS)
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


def test_the_web_profile_adds_the_gate_env_vars_on_windows(project):
    from pdt.config import env_spec
    (project / "pdt.yml").write_text(WINDOWS)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "dashboard", WEB_APP)
    required = env_spec(merged_app("dashboard"))["required"]
    assert web.ENV_CLIENT_ID in required and web.ENV_COOKIE_SECRET in required
    assert web.ENV_SERVICE_ACCOUNT_PATH not in required
    add_app(project, "grouped", WEB_APP.replace("allowed_domains: [example.com]", "allowed_groups: [sales]"))
    required = env_spec(merged_app("grouped"))["required"]
    assert web.ENV_SERVICE_ACCOUNT_PATH in required and web.ENV_ADMIN_EMAIL in required


def test_every_profile_answers_every_concern_for_every_provider():
    from pdt.config import PROVIDERS
    for name in profiles.NAMES:
        answers = profiles.module(name).ANSWERS
        assert set(answers) == set(PROVIDERS), name
        for provider, concerns in answers.items():
            assert set(concerns) == set(profiles.CONCERN_NAMES), (name, provider)
            assert all(text for text in concerns.values()), (name, provider)


def test_the_job_profile_is_the_default_and_owns_the_schedule():
    assert profiles.DEFAULT == job.NAME
    assert "schedule" in job.KEYS
    assert job.KEYS.isdisjoint(web.KEYS)


def test_a_top_level_auth_section_points_at_web(project):
    (project / "pdt.yml").write_text(GOOGLE_CLOUD)
    add_app(project, "dashboard", "app_profile: web\nauth: public\n")
    assert any("auth" in p and "web.auth" in p for p in validate_app("dashboard"))
