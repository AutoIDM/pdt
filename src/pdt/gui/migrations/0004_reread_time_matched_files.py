"""Forget the files of every run matched by time, so the worker reads them again.

The first time-matching rule gave a run without an end time every folder
written after it, so a failed Azure run listed hundreds of files that
belong to later runs. The bounded rule that replaced it only applies when
a run is read, and a read run is not read again by itself.
"""

from django.db import migrations


def forget_time_matched_files(apps, _schema_editor):
    Run = apps.get_model("gui", "Run")
    Artifact = apps.get_model("gui", "Artifact")
    matched = Run.objects.filter(artifacts_by_time=True)
    Artifact.objects.filter(run__in=matched).delete()
    matched.update(artifacts_synced_at=None, artifacts_by_time=False)


class Migration(migrations.Migration):
    dependencies = [("gui", "0003_app_history_synced_at")]
    operations = [migrations.RunPython(forget_time_matched_files, migrations.RunPython.noop)]
