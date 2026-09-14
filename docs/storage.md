# Keeping files between runs

## The problem

Your app runs, writes some files, and stops. Then the computer it ran on is thrown away. The files go with it.

That is fine for an app that only sends an email. It is not fine when you want to look at last week's report, or when the next run needs to know what the last run did.

pdt gives every app a folder in your cloud account that does not go away. Deploy the app, destroy it, deploy it again. The folder and everything in it is still there.

## What you get

Every app gets a folder. You do not have to ask for it. pdt creates a bucket in your cloud account called `pdt-data-` plus a short code, and gives each app its own folder inside it.

When your app runs, the environment variable `PDT_STORAGE_URL` points at that folder. On your own computer it points at a folder inside your project instead, so you can test without a cloud account.

pdt never deletes the bucket on its own. `pdt destroy` removes the app and leaves the files. Only `pdt storage destroy` deletes files, and it asks first.

If an app does not need this, put `storage: false` in its `config.yml`.

## Writing files

Open the folder and write to it:

```python
from pdt.utils import storage

store = storage.store()
with store.open("report.csv", "wb") as f:
    f.write(b"name,count\n")
```

`store.fs()` gives you the full [fsspec](https://filesystem-spec.readthedocs.io/) filesystem, rooted at your app's folder, when you need more than open and list.

Anything you write is there next time. Output from one run goes under `store.run_folder()`, which names a new `runs/<time>-<id>/` folder each run, so runs never mix.

## Keeping state between runs

Some apps need a file from the last run. A Meltano app needs its bookmark, so it only reads new records. A sync needs its database.

Put those files in a local folder, and copy that folder down at the start and up at the end:

```python
from pathlib import Path
from pdt.utils import storage

store = storage.store()
lease = store.pull("state/", Path(".pdt-state"))
# do the work, keep anything the next run needs inside .pdt-state/
store.push(Path(".pdt-state"), "state/", lease)
store.push(Path("artifacts"), store.run_folder() + "artifacts/")
```

That is the whole job. `pull` locks the folder so a second copy of your app cannot run at the same time and mix up the files. `push` checks that nobody else changed them, saves them, and unlocks. If a run crashes, the next one takes over the lock after a few minutes.

If `push` says another run changed the files, let your app fail. The next run starts from the last good state.

## Looking at the files

From your own computer:

```
pdt storage ls my-app
pdt storage ls my-app runs/
pdt storage get my-app runs/20260914T090000Z-abc123/report.csv
pdt storage query my-app "select count(*) from 'runs/*/report.csv'"
```

`query` runs SQL over CSV and Parquet files in the folder. It uses your own cloud sign-in, the same one `pdt deploy` uses.

## Deleting files

```
pdt storage destroy my-app
```

It shows what it will delete and asks. Nothing else in pdt deletes your data.
