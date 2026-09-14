# Design: a data store for every app

Status: built on the `storage` branch. The cloud paths were checked against SDK and CLI signatures; the local path was run end to end. No cloud account has run it yet.

## The problem

A pdt app runs, writes files, and exits. The container is gone a second later, and so are the files. Three things people need survive that:

- Output someone wants to look at later. The customer Meltano projects write CSV artifacts on every run and keep them for six months.
- State the next run needs. Meltano's incremental bookmarks, and a database that holds tables between runs. The Salesforce sync keeps those in a Postgres server today, which is one more thing to host, pay for, and secure.
- Nothing else. The store is files. pdt does not host a database.

The store must outlive the app. A user destroys and redeploys an app, and the next run finds what the last run left. Nothing pdt does removes the data unless the user asks for that on purpose.

## What exists in the Meltano world

Four patterns are in use. Each one tells us something.

| Pattern | Examples | State | Lesson |
|---|---|---|---|
| A target for one cloud | [target-s3](https://github.com/crowemi/target-s3), [target-gcs](https://github.com/Datateer/target-gcs), [target-azureblobstorage](https://github.com/talview/target-azureblobstorage) | Maintained | Each one wants an access key in its config. None uses the job's own identity. |
| One target for all clouds | [target-jsonl-blob](https://github.com/MeltanoLabs/target-jsonl-blob) | Archived January 2026 | The cloud-from-URL idea is right. Nobody maintains it. |
| Write local files, then copy them up | [target-csv](https://hub.meltano.com/loaders/target-csv/), [target-parquet](https://github.com/automattic/target-parquet), [target-duckdb](https://github.com/MeltanoLabs/target-duckdb) | Maintained | This is what the customer projects do. The copy step is the project's own. |
| Table formats on S3 | target-iceberg, target-athena | Many forks, no clear owner | Not for pdt. |

Meltano can keep incremental state in a bucket through `state_backend.uri` ([docs](https://docs.meltano.com/concepts/state_backends)). Only the Azure backend uses ambient identity. S3 and GCS want keys.

Two facts fall out of this. No maintained target covers all three clouds. Almost every target wants a key pasted into config. pdt's job is to give the app a place to write and an identity that can write there, so that no target ever needs a key. The "local files, then copy" pattern is the one to build on, because it leaves meltano.yml untouched.

## The store

A project has one store per cloud account. A store is one bucket or container with a derived name and one folder per app.

| Field | Value |
|---|---|
| name | `pdt-data-<suffix>`. The suffix is the first 10 characters of the SHA-256 of the account id. `azure_settings` already computes this for the subscription. |
| account id | AWS: the 12-digit account number that `adopt_account` saves as `platform.account`. Google Cloud: `platform.project`. Azure: `platform.subscription`. |
| scope | AWS: a bucket, at account scope. Google Cloud: a bucket in the project. Azure: storage account `pdtdata<suffix>` in its own resource group `pdt-data`. |
| app folder | `<app>/` |
| tags | `managed-by=pdt`, `pdt-lifecycle=retain` |

Azure needs its own resource group because `destroy_group` deletes resource group `pdt` after the last app is destroyed. A separate lifecycle needs a separate scope. AWS and Google Cloud get the same shape for parity, even though a bucket already sits outside the app's lifecycle there.

Inside an app folder, pdt fixes two names and nothing else:

- `runs/<timestamp>-<run id>/` holds output from one run. `<run id>` is the provider's execution id, so two runs never share a folder.
- `state/` holds what the next run needs.

Everything below those is the app's.

## What the app sees

One environment variable, `PDT_STORAGE_URL`. On a cloud it is the app folder, for example `s3://pdt-data-3f2a9c1b7e/hello-world/`. On a laptop and on the `windows` provider it is `<project>/.pdt/storage/<app>/`. The app code does not branch on provider.

One module, `pdt.utils.storage`, with these functions:

- `root()` returns the URL above.
- `fs()` returns an fsspec filesystem for the URL, with auth set up. s3fs and gcsfs need no arguments. adlfs needs the storage account name and a credential object, and this is the one place that knows that.
- `pull(remote, local)` copies a folder from the store to disk. It takes the lock (below).
- `push(local, remote)` copies a folder from disk to the store. It releases the lock.

A Meltano app uses `pull` and `push` around `meltano run`, and points Meltano's system database and its DuckDB file at the pulled folder. A manual app uses `fs()` and writes what it likes. An app that keeps nothing between runs, like hello-world, uses none of this and still has a store folder it can write to.

## Config

One key, `storage`, per app. It defaults to `true`. An app sets `storage: false` to opt out, and then gets no folder, no grant, and no plan line. Nothing goes in `pdt.yml`. The name derives from the account and the region already exists.

## Deploy

Each provider module gains `ensure_store(app)`. It is get-or-create, so a second run changes nothing. It then grants the runtime identity write access to the app folder and nothing above it:

- AWS: one statement on the app's existing role, scoped to `arn:aws:s3:::pdt-data-<suffix>/<app>/*`.
- Google Cloud: `roles/storage.objectUser` for `pdt-runner` on the bucket with an IAM condition on the `<app>/` prefix.
- Azure: `Storage Blob Data Contributor` for the job's identity on the container, with an [attribute-based access control condition](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-auth-abac-examples) on the `<app>/` blob path and list prefix.

The plan gains two lines, the same words on every provider:

```
create bucket pdt-data-3f2a9c1b7e (kept after destroy)
grant pdt-hello-world write access to pdt-data-3f2a9c1b7e/hello-world/
```

The cost estimate gains one storage line priced from the current object count, with the pricing functions each provider already has.

## Destroy

`pdt destroy <app>` removes the grant and keeps every object. The closing "still present" list names the store: `kept: bucket pdt-data-3f2a9c1b7e (14 objects under hello-world/)`. Destroy checks for a live lock first and warns, because removing the grant mid-run loses that run's state.

Deleting data is a separate command, `pdt storage destroy`. It prints what it will delete, asks to proceed, and deletes only a store that carries both tags. It is a second change, after the rest of this lands.

Destroy then deploy restores access to the same data, because the bucket name and the app folder derive from values that did not change. A renamed app gets a new empty folder. A different cloud account gets a different bucket.

## Reading the store from the CLI

```
pdt storage ls <app> [path]
pdt storage get <app> <path> [local-dir]
pdt storage query <app> "select * from 'runs/*/artifacts/*_customers.csv'"
```

`query` downloads the matching files with fsspec to a temporary folder and runs DuckDB on them there. DuckDB's own cloud extensions read Google Cloud Storage only with HMAC keys, so the download keeps parity. The user's own cloud login supplies the credentials.

## Two runs at once

Different apps cannot conflict. Each has its own folder and a grant that stops at that folder.

The same app can run twice at once: a run longer than the schedule gap, a manual run during a scheduled one, a retry over a slow run. The schedulers do not agree on what happens. Azure Functions timers run one at a time. Lambda can be capped at one. Fargate, Cloud Run Jobs, and Container Apps jobs start a new execution each time. So the scheduler cannot be the fix.

Four conflicts are possible:

1. Lost update on `state/`. Run A pulls, run B pulls, A pushes, B pushes over A. With Meltano state inside, the bookmark also goes backward and the next run re-extracts records A loaded.
2. A half-written run folder. Push copies many objects. Each object write is atomic on S3, Google Cloud Storage, and Azure Blob. The folder is not.
3. Two runs in one folder. Solved by the run id in the folder name.
4. Destroy during a run. Solved by the lock check in destroy.

All three stores support "create this object only if it does not exist": `If-None-Match: *` on S3 and Azure Blob, `ifGenerationMatch=0` on Google Cloud Storage. That gives one lock that lives in the store itself and works the same everywhere.

The fix, all inside `pull` and `push`:

- `pull` creates `state/lock` with the conditional write. The lock holds the run id and start time. If the lock exists and is younger than the limit, `pull` exits with "another run of <app> started at <time> still holds the state". If it is older, the earlier run crashed and this run takes it over.
- `pull` records the version tag of each state object. `push` writes each object only if its tag is unchanged, then deletes the lock. A changed tag means a second run got past the lock. `push` fails loudly instead of erasing work.
- `push` writes `_done` as the last object in a run folder. Readers, including `pdt storage query`, ignore folders without it.

The app writes none of this. It calls `pull` before the work and `push` after. An app that writes only to `runs/` never takes the lock.

## Replacing Postgres with DuckDB in the Salesforce sync

The run becomes:

1. `pull("state/", ".pdt-state/")`.
2. `meltano run extract transform artifacts load` with target-duckdb, dbt-duckdb, and tap-duckdb in place of the Postgres plugins. Meltano's database and the DuckDB file both live in `.pdt-state/`.
3. `push(".pdt-state/", "state/")` and `push("artifacts/", "runs/<ts>-<id>/artifacts/")`.

Three things to verify before promising this: alembic on DuckDB through duckdb-engine, whether tap-duckdb covers the streams and incremental modes the sync uses, and the lock behavior on a 5-minute schedule when a run takes longer than 5 minutes.

## Rejected options

| Option | Why not |
|---|---|
| One bucket per app | The name follows the app. Renaming or redeploying an app orphans its data. |
| A bucket name in `pdt.yml` | One more key the user must know how to fill. Shared resources get stable defaults. |
| Reuse the Azure Functions storage account | It dies with resource group `pdt`. |
| A thin `read_text` / `write_text` API | It hid fsspec behind a smaller copy of fsspec. Give the app the real filesystem. |
| Opt-in `storage: true` | The simplest behavior is the default. A user opts out, never in. |
| Apps call boto3 or the Azure SDK directly | Breaks parity. The user must learn each SDK and where its credentials come from. |
| Concurrency limits on the scheduler | Not available on every runtime, so behavior would differ by provider. |

## Order of work

1. `pdt.utils.storage` with `root`, `fs`, `pull`, `push`, and the lock. Tests run against a local folder. No cloud.
2. `ensure_store` and the grant on each provider, the plan lines, the cost line, and the destroy behavior. All three providers in one change.
3. `pdt storage ls`, `get`, and `query`.
4. `pdt storage destroy`.
5. The Salesforce sync on DuckDB, as the proof.
