from pdt.deploy_google_cloud import not_found


def test_a_missing_cloud_run_job_counts_as_not_found():
    assert not_found("ERROR: (gcloud.run.jobs.describe) Cannot find job [pdt-app].")


def test_the_not_found_wordings_still_count():
    assert not_found("ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [x] not found.")


def test_a_permission_error_still_raises():
    assert not not_found("ERROR: (gcloud.run.jobs.describe) PERMISSION_DENIED")
