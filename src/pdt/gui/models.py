"""What the GUI keeps between page loads.

Config (schedule, provider, pause, enabled) is read from the project's
files on every request and never stored: the files are the truth. Runs
and log lines are stored, because the provider forgets them after its
retention period and fetching them costs a cloud call.

A Project row is the path of one pdt project. Several projects can share
one database, which is how a future multi-project view starts.
"""

from django.db import models


class Project(models.Model):
    path = models.CharField(max_length=1024, unique=True)


class App(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="apps")
    name = models.CharField(max_length=200)
    synced_at = models.DateTimeField(null=True)
    sync_error = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("project", "name")]


class Run(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="runs")
    run_id = models.CharField(max_length=500)
    started = models.DateTimeField()
    ended = models.DateTimeField(null=True)
    status = models.CharField(max_length=20)
    exit_code = models.IntegerField(null=True)
    logs_synced_at = models.DateTimeField(null=True)
    logs_error = models.TextField(blank=True, default="")
    artifacts_synced_at = models.DateTimeField(null=True)
    artifacts_error = models.TextField(blank=True, default="")
    # True when the files were found by the time they were written, because
    # the job ran a pdt older than 0.1.3 and named its folder at random.
    artifacts_by_time = models.BooleanField(default=False)

    class Meta:
        unique_together = [("app", "run_id")]
        ordering = ["-started"]

    @property
    def key(self) -> str:
        """The id a runs/ folder in the app's storage ends with (see pdt.utils.storage)."""
        return self.run_id.rsplit("/", 1)[-1]


class LogLine(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="lines")
    seq = models.IntegerField()
    time = models.DateTimeField(null=True)
    level = models.CharField(max_length=10, blank=True, default="")
    message = models.TextField()

    class Meta:
        ordering = ["seq"]


class Artifact(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="artifacts")
    path = models.CharField(max_length=1024)
    size = models.BigIntegerField(null=True)

    class Meta:
        ordering = ["path"]


class Timing(models.Model):
    """One page view or one pdt command and how long it took.

    A page's time includes the pdt commands it waited for, so the two kinds
    side by side on the Stats page show where the time goes.
    """
    kind = models.CharField(max_length=10)
    name = models.CharField(max_length=200)
    detail = models.CharField(max_length=1024)
    started = models.DateTimeField()
    ms = models.IntegerField()
    ok = models.BooleanField()

    class Meta:
        ordering = ["-started"]
