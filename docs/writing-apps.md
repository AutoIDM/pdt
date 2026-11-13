# Writing your own app

An app is a folder with a `run.py` that has a `main()` function. It declares its own dependencies in a script header, and pdt is one of them:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.14"]
# ///
from pathlib import Path

from pdt.config import check_env, load_env, merged_app
from pdt.utils.log import log


def main() -> int:
    app = merged_app(Path(__file__).resolve().parent.name)
    load_env(app["dir"])
    ...
```

The pinned version is the pdt version the app runs with, on your computer and in a deployed job. Upgrading pdt on your computer does not change it. Keep the pin at the newest pdt release, and run `pdt deploy APP` after you raise it, so the deployed job uses it too.

## PowerShell apps

A folder of `.ps1` files with no `run.py` is a PowerShell app: pdt reads the scripts to find the modules to install, then runs the entry scripts in name order through pwsh. Before `pdt run` starts the scripts on your computer, pdt installs each of those modules that is missing or at another version into its own folder (`~/.local/share/pdt/powershell-modules`, or `%LOCALAPPDATA%\pdt\powershell-modules` on Windows), so a local run loads the same versions a deploy installs. Other PowerShell sessions do not see that folder, and deleting it removes the modules.

A script reads the app's `config:` section from `$env:PDT_CONFIG_JSON | ConvertFrom-Json`, and pdt keeps every file a script writes to `$env:PDT_OUTPUT_DIR` in the app's data store. For files a later run needs, such as the last result to compare against, use `$env:PDT_STATE_DIR`: when a script reads it, pdt fills it with the app's saved state before the first script and saves it again when every script succeeds. After a failure the saved state stays as the last good run left it. While a run holds the state, another run of the same app stops with an error; `pdt storage <app> unlock` frees state that a dead run still holds.

A `run.ps1` file is the only entry script. Otherwise, use `run_scripts` in the app's `pdt.yml` to choose entries and set their order. Do not use `run.ps1` and `run_scripts` together. By default, pdt stops after the first failed script. Set `continue_on_error: true` to run every entry script. The run still fails with the first script's exit code.

To change either step, run `pdt new <app> --from-scripts`. It writes `requirements.psd1`, which lists the modules with pinned versions, and `run.py`, which runs the scripts. Once they exist, pdt installs exactly the modules in `requirements.psd1` (`pdt validate` warns about a module the scripts need that it leaves out), and `pdt run` and deploy run your `run.py`.

```
pdt new my-report --from-scripts
```

## Web apps

A job runs and exits. A web app runs all the time and answers HTTP requests: a dashboard, a lookup form, an internal tool. Set `app_profile: web` in the app's own `pdt.yml` to make one. [App profiles](profiles.md) lists what every command means for a web app and which resources each provider creates for it. The web profile deploys on Google Cloud (a Cloud Run service) and Windows (two services); `pdt validate` says so for AWS and Azure.

```yaml
app_profile: web
web:
  port: 8050                    # the app listens here (default 8080)
  health_path: /                # must answer 200 (default /)
  auth:
    provider: google
    allowed_domains: [example.com]
    allowed_groups: [sales@example.com]
storage:
  enabled: true
  read: [warehouse]             # other apps whose storage this app may read
```

A web app has no `schedule`; `pdt validate` rejects one. A web app is either a `run.py` that serves HTTP on the port in `PDT_PORT`, or any container: a folder with a `Dockerfile` and a `pdt.yml` and no `run.py`, such as nginx serving static files. It needs no sign-in code. The platform puts its own gate in front of it, and no request reaches the app before the visitor signed in: Identity-Aware Proxy on Cloud Run, oauth2-proxy on a Windows PC. A web app is never open to everyone: `pdt validate` requires `web.auth.provider` and at least one of `web.auth.allowed_domains` and `web.auth.allowed_groups`.

`web.auth.provider` names where visitors sign in. `google` works on Google Cloud and Windows; `microsoft` is accepted but has no gate yet. On Google Cloud nothing more is needed: Identity-Aware Proxy uses a Google-managed OAuth client. On Windows, create an OAuth client in the Google Cloud console with the redirect URI `pdt deploy` names, and put its id and secret in `.env` as `PDT_AUTH_CLIENT_ID` and `PDT_AUTH_CLIENT_SECRET`; `pdt validate` and `pdt deploy` require them like any other env var. Windows also needs `PDT_AUTH_COOKIE_SECRET` (any random text), and with `web.auth.allowed_groups` a service account with domain-wide delegation; see [Web apps on Windows](platforms.md#web-apps-on-windows).

`web.auth.allowed_domains` lists the domains whose accounts may enter, and `web.auth.allowed_groups` the groups whose members may. On Google Cloud both are IAM bindings on the service (a Google Workspace domain, a Google group); on Windows the proxy checks both.

Each gate forwards the visitor's email in a header: `X-Goog-Authenticated-User-Email` (IAP, as `accounts.google.com:<email>`), `X-Forwarded-Email` (Windows). An app may ignore it. A Python app that wants it calls `pdt.utils.auth.current_user(headers)`, which reads every gate's header and returns the email, or `""` with no gate.

```python
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdt.utils.auth import current_user


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        user = current_user(self.headers)   # the email the gate forwarded, or ""
        ...


ThreadingHTTPServer(("0.0.0.0", int(os.environ["PDT_PORT"])), Handler).serve_forever()
```

`pdt run APP` serves the app on `http://127.0.0.1:PORT/` on your own computer with no gate, so `current_user` returns `""` there; Ctrl+C stops it. A container app runs through Docker on your computer, with each `storage.read` app's local storage folder mounted read-only under `/pdt/storage/<app>/`. A deployed web app has no runs to start, so `pdt run APP --deployed` refuses it; `pdt deploy APP` restarts it with the deployed code.

A web app often shows what a job produced. `storage.read` names the apps whose storage it may read, and `pdt deploy` grants the read rights. On Google Cloud, each named app's folder of the bucket is mounted read-only at `/pdt/storage/<app>/` (the folder in `PDT_STORAGE_READ_DIR`), so any container reads it as plain files; a file the job writes shows up within about a minute, and the reader takes no lock, so it never blocks the job. A Python app can also read the store itself: `pdt.utils.storage.read_only("warehouse")` is a read-only view of that app's folder, and its `fetch("state/warehouse.duckdb", Path("warehouse.duckdb"))` copies the file to that path when the stored copy changed since the last fetch.

## Logging

`pdt.utils.log` writes one log line per event:

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
```

Levels are `debug`, `info`, `warning`, and `error`. Locally a line is human-readable text on stdout; with `LOG_FORMAT=json` it is one JSON object per line, which is also the default on Cloud Run.
