# Keeping files between runs

A scheduled job runs, writes some files, and stops. Then the container it ran in is thrown away, and any local files go with it. So pdt gives every app a persistent folder in your cloud account (or on Windows). Deploy the app, destroy it, deploy it again: the folder and everything in it stay intact.

pdt creates one bucket per cloud account, named `pdt-data-` plus a short code, and gives each app its own folder inside it. When your app runs in the cloud, `PDT_STORAGE_URL` points at that folder. A Windows scheduled task points it at `%ProgramData%\pdt\APP\storage\`. When you run `pdt run APP` on your own computer, it points at `.pdt/storage/APP/` inside your project instead. Your app code is the same in every place.

## Reading and writing files in Python

Write and read files with `pdt.utils.storage`:

```python
from pdt.utils import storage

store = storage.store()
with store.open("report.csv", "wb") as f:
    f.write(b"name,count\n")
```

`store.fs()` gives you the full [fsspec](https://filesystem-spec.readthedocs.io/) filesystem, rooted at your app's folder. Output from one run goes under `store.run_folder()`, which names a new `runs/<time>-<id>/` folder each run.

## Syncing state between runs

Some jobs need files from the last run, such as a Meltano bookmark or a local SQLite/DuckDB database. Keep those in the `state` folder that `storage.sync()` hands you, and put anything a person may want to look at afterwards in its `output` folder:

```python
from pdt.utils import storage

with storage.sync() as run:
    # do the work; keep anything the next run needs inside run.state/
    (run.output / "report.csv").write_text("name,count\n")
```

`sync` copies `state/` down from your app's folder and locks it, so a second copy of your job cannot run at the same time and mix up the files. When the block ends, it uploads `run.output` to this run's `runs/` folder, checks that nobody else changed the state files, saves them, and unlocks. When the block raises, or the run is interrupted, it still uploads `run.output`, unlocks, and leaves `state/` as it was, so the next run starts from the last good state. An app whose state is safe to keep at any point, such as a Meltano bookmark or an OAuth token that rotates on every login, passes `storage.sync(push_state_on_error=True)` so a failed run keeps what it saved. Both local folders live under `.pdt/runs/` in the app folder and stay there for inspection.

The same steps by hand are `store.pull("state/", folder)`, which returns a lease, then `store.push(folder, "state/", lease)` on success or `store.abort(lease)` on failure.

A run that dies without reaching either call leaves the lock behind. The next run takes it over right away when it can tell the holder is gone: the holder ran on the same computer and its process has ended, or it carries the same `PDT_RUN_ID`. Otherwise it waits 30 minutes, or you release the lock yourself with `pdt storage APP unlock`, which asks first.

For PowerShell jobs, pdt manages `$env:PDT_OUTPUT_DIR` and `$env:PDT_STATE_DIR` automatically; see [PowerShell apps](writing-apps.md#powershell-apps).

## Inspecting, downloading, and querying files from the CLI

From your own computer, `pdt storage` reads your app's files using your cloud sign-in:

```
pdt storage my-report ls
pdt storage my-report ls runs/ --recursive
pdt storage my-report get runs/2026-10-21-abc/report.csv
pdt storage my-report --open
```

To query CSV, Parquet, or JSON files directly with DuckDB SQL, put storage-relative paths or glob patterns in single quotes inside the query. pdt downloads the matching files into a temporary folder, rewrites the paths, and prints the result as a table:

```
pdt storage my-report query "SELECT * FROM 'runs/*/report.csv' LIMIT 20"
pdt storage my-report query "SELECT status, count(*) FROM 'report.csv' GROUP BY status"
```

`pdt storage APP destroy` is the only command that deletes stored files, and it asks first. An app that needs no storage sets `storage: false` in its `pdt.yml`.

## Reading another app's files

An app can read the folders of the apps listed under `storage: read:` in its `pdt.yml`. It cannot change them.

```yaml
storage:
  read:
    - other-app
```

- **Scheduled jobs (Azure only):** Read another app's folder with `storage.store(app="other-app")`, or fetch a single file without a lock only when it changed using `storage.read_only("other-app").fetch(...)`:

```python
from pathlib import Path
from pdt.utils import storage

other = storage.store(app="other-app")
storage.read_only("other-app").fetch("state/warehouse.duckdb", Path(".cache/warehouse.duckdb"))
```

- **Web apps (Google Cloud):** Each listed app's folder is mounted read-only at `/pdt/storage/<app>/` (and `PDT_STORAGE_READ_DIR` holds the parent directory locally and in the cloud). New files appear within about a minute.

Run `pdt deploy APP` after you change `storage.read`.
