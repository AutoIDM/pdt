# Keeping files between runs

Your app runs, writes some files, and stops. Then the computer it ran on is thrown away, and the files go with it. So pdt gives every app a folder in your cloud account that stays. Deploy the app, destroy it, deploy it again: the folder and everything in it is still there.

pdt creates one bucket per cloud account, named `pdt-data-` plus a short code, and gives each app its own folder inside it. When your app runs in the cloud, `PDT_STORAGE_URL` points at that folder. A Windows scheduled task points it at `%ProgramData%\pdt\APP\storage\`. When you run `pdt run APP` on your own computer, it points at `.pdt/storage/APP/` inside your project instead. The app code is the same in every place.

Write and read files with `pdt.utils.storage`:

```python
from pdt.utils import storage

store = storage.store()
with store.open("report.csv", "wb") as f:
    f.write(b"name,count\n")
```

`store.fs()` gives you the full [fsspec](https://filesystem-spec.readthedocs.io/) filesystem, rooted at your app's folder. Output from one run goes under `store.run_folder()`, which names a new `runs/<time>-<id>/` folder each run.

Some apps need files from the last run: a Meltano bookmark, a database. Keep those in the `state` folder that `storage.sync()` hands you, and put anything a person may want to look at afterwards in its `output` folder:

```python
from pdt.utils import storage

with storage.sync() as run:
    # do the work; keep anything the next run needs inside run.state/
    (run.output / "report.csv").write_text("name,count\n")
```

`sync` copies `state/` down from your app's folder and locks it, so a second copy of your app cannot run at the same time and mix up the files. When the block ends, it uploads `run.output` to this run's `runs/` folder, checks that nobody else changed the state files, saves them, and unlocks. When the block raises, or the run is interrupted, it still uploads `run.output`, unlocks, and leaves `state/` as it was, so the next run starts from the last good state. An app whose state is safe to keep at any point, such as a Meltano bookmark or an OAuth token that rotates on every login, passes `storage.sync(push_state_on_error=True)` so a failed run keeps what it saved. Both local folders live under `.pdt/runs/` in the app folder and stay there for inspection.

The same steps by hand are `store.pull("state/", folder)`, which returns a lease, then `store.push(folder, "state/", lease)` on success or `store.abort(lease)` on failure.

A run that dies without reaching either call leaves the lock behind. The next run takes it over right away when it can tell the holder is gone: the holder ran on the same computer and its process has ended, or it carries the same `PDT_RUN_ID`. Otherwise it waits 30 minutes, or you release the lock yourself with `pdt storage APP unlock`, which asks first.

From your own computer, `pdt storage APP ls`, `get`, and `query` read the files with your own cloud sign-in. `pdt storage APP destroy` is the only command that deletes them, and it asks first. An app that needs none of this sets `storage: false` in its `pdt.yml`.

## Reading another app's files

An app can read the folders of the apps listed under `storage: read:` in its `pdt.yml`. It cannot change them. `storage:` then takes a mapping: `enabled` (default `true`, the same as `storage: true`) and `read`.

```yaml
storage:
  enabled: true
  read: [warehouse]
```

A web app finds each folder as read-only files at `/pdt/storage/<app>/` (the folder in `PDT_STORAGE_READ_DIR`), so any container reads it with no pdt code. A file the job writes shows up within about a minute.

A Python app can also open the folder with `pdt.utils.storage`:

```python
from pathlib import Path

from pdt.utils import storage

other = storage.store(app="warehouse")
path = storage.read_only("warehouse").fetch("state/warehouse.duckdb", Path("warehouse.duckdb"))
```

`fetch` copies the file to the local path when the stored copy changed since the last fetch and returns the path, or `None` when the file is not there. A reader takes no lock, so it never blocks the job that writes the files, and it never sees a half-written file. For a job, `storage: read:` works with the `azure` provider only. Run `pdt deploy APP` after you change the list.
