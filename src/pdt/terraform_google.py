"""Terraform configuration for Google Cloud Run jobs."""

from __future__ import annotations


from pdt.terraform import configuration as terraform_configuration


def address(kind: str, name: str) -> str:
    return f"google_{kind}.{name}"


def configuration(provider_settings: dict, resources: dict,
                  data: dict | None = None, outputs: dict | None = None) -> dict:
    return terraform_configuration("google", provider_settings, resources, data, outputs)


def api_resources(project: str) -> dict:
    services = (
        "artifactregistry.googleapis.com",
        "cloudbuild.googleapis.com",
        "cloudscheduler.googleapis.com",
        "iam.googleapis.com",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
        "serviceusage.googleapis.com",
        "storage.googleapis.com",
    )
    return {
        "google_project_service": {
            "pdt": {
                "for_each": {service: service for service in services},
                "project": project,
                "service": "${each.value}",
                "disable_on_destroy": False,
            },
        },
    }


def global_resources(project: str, service_account: str) -> dict:
    account_id = service_account.split("@", 1)[0]
    resources = api_resources(project)
    if service_account != f"pdt-runner@{project}.iam.gserviceaccount.com":
        return resources
    resources["google_service_account"] = {
        "runner": {
            "project": project,
            "account_id": account_id,
            "display_name": "pdt job runner",
            "description": "Managed by PDT",
            "depends_on": ["google_project_service.pdt"],
        },
    }
    return resources


def shared_resources(project: str, region: str, repository: str) -> dict:
    return {
        "google_artifact_registry_repository": {
            "images": {
                "project": project,
                "location": region,
                "repository_id": repository,
                "format": "DOCKER",
                "labels": {"managed-by": "pdt"},
                "description": "Managed by PDT",
            },
        },
    }


def app_resources(app_name: str, project: str, region: str, cron: str,
                  timezone: str, image: str, service_account: str,
                  secret: str, has_secret: bool,
                  oauth_cache_updates: bool, storage_url: str | None = None) -> dict:
    job_name = f"pdt-{app_name}"
    env = []
    if has_secret:
        env.append({
            "name": "PDT_ENV_JSON",
            "value_source": {"secret_key_ref": {"secret": secret, "version": "latest"}},
        })
    if oauth_cache_updates:
        env.append({
            "name": "PDT_ENV_SECRET_RESOURCE",
            "value": f"projects/{project}/secrets/{secret}",
        })
    if storage_url:
        env.append({"name": "PDT_STORAGE_URL", "value": storage_url})
    container = {
        "image": image,
        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
    }
    if env:
        container["env"] = env
    resources = {
        "google_cloud_run_v2_job": {
            "job": {
                "project": project,
                "location": region,
                "name": job_name,
                "labels": {"managed-by": "pdt"},
                "deletion_protection": False,
                "template": {"template": {
                    "service_account": service_account,
                    "max_retries": 1,
                    "containers": [container],
                }},
            },
        },
        "google_cloud_run_v2_job_iam_member": {
            "invoker": {
                "project": project,
                "location": region,
                "name": "${google_cloud_run_v2_job.job.name}",
                "role": "roles/run.invoker",
                "member": f"serviceAccount:{service_account}",
            },
        },
        "google_cloud_scheduler_job": {
            "schedule": {
                "project": project,
                "region": region,
                "name": job_name,
                "description": f"Managed by PDT app {app_name}",
                "schedule": cron,
                "time_zone": timezone,
                "http_target": {
                    "uri": (f"https://{region}-run.googleapis.com/apis/run.googleapis.com"
                            f"/v1/namespaces/{project}/jobs/{job_name}:run"),
                    "http_method": "POST",
                    "oauth_token": {"service_account_email": service_account},
                },
                "depends_on": ["google_cloud_run_v2_job_iam_member.invoker"],
            },
        },
    }
    if has_secret:
        resources["google_secret_manager_secret"] = {
            "env": {
                "project": project,
                "secret_id": secret,
                "labels": {"managed-by": "pdt"},
                "replication": {"auto": {}},
            },
        }
        roles = ["roles/secretmanager.secretAccessor"]
        if oauth_cache_updates:
            roles.append("roles/secretmanager.secretVersionAdder")
        resources["google_secret_manager_secret_iam_member"] = {
            f"runner_{index}": {
                "project": project,
                "secret_id": "${google_secret_manager_secret.env.secret_id}",
                "role": role,
                "member": f"serviceAccount:{service_account}",
            }
            for index, role in enumerate(roles)
        }
        resources["google_cloud_run_v2_job"]["job"]["depends_on"] = [
            f"google_secret_manager_secret_iam_member.runner_{index}"
            for index in range(len(roles))
        ]
    return resources
