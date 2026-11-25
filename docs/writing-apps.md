# Writing your own app

An app is a folder inside your project holding its own `pdt.yml` and the code to run. pdt supports Python apps, PowerShell apps, and web apps (Python or custom `Dockerfile`).

## Python apps

A Python app has a `run.py` with a `main()` function. It declares its dependencies in a PEP 723 script header, including `pdt-cli[apps]`:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.15"]
# ///
from pathlib import Path

from pdt.config import check_env, load_env, merged_app
from pdt.utils.log import log


def main() -> int:
    app = merged_app(Path(__file__).resolve().parent.name)
    load_env(app["dir"])
    ...
    return 0
```

`pdt new` pins the currently installed `pdt-cli` version automatically. The pinned version is what the app runs with on your computer and when deployed; run `pdt upgrade` to raise every app's pin to the latest release, then run `pdt deploy APP` so the deployed app uses it too.

pdt parses `run.py` and other `.py` files in the app folder automatically to discover env vars: `os.environ["NAME"]` is treated as required, and `os.environ.get("NAME")` or `os.getenv("NAME")` as optional. Names starting with `PDT_` are skipped by the scan, so list any `PDT_` env vars your app needs under `env:` in `pdt.yml`.

## PowerShell apps

A folder of `.ps1` files with no `run.py` is a PowerShell job: pdt scans the scripts to find required modules and `$env:NAME` reads, then runs the entry scripts in name order through `pwsh`. Before `pdt run` starts the scripts on your computer, pdt installs any missing or mismatched module versions into its own folder (`~/.local/share/pdt/powershell-modules`, or `%LOCALAPPDATA%\pdt\powershell-modules` on Windows), so local runs match deployed runs without altering other PowerShell sessions.

- **Config:** Read the app's `config:` section with `$env:PDT_CONFIG_JSON | ConvertFrom-Json`.
- **Output files:** Write files you want to keep (such as CSV or Excel reports) to `$env:PDT_OUTPUT_DIR`; pdt uploads them to `runs/<time>-<id>/` in the app's data store.
- **Saved state:** When a script reads `$env:PDT_STATE_DIR`, pdt pulls `state/` under a lock before the first script and pushes it back when every script succeeds. After a failure, saved state stays as the last good run left it. If a crashed run leaves a lock behind, release it with `pdt storage APP unlock`.
- **Entry scripts:** If `run.ps1` exists, it is the only entry script. Otherwise, set `run_scripts` in `pdt.yml` to pick entry scripts and their order (do not use `run.ps1` and `run_scripts` together), or let pdt run every `.ps1` that no other script loads in alphabetical order. Set `continue_on_error: true` to run remaining entry scripts after a failure (the job still exits with the first failing script's code).

To pin module versions explicitly or customize execution, run:

```
pdt new my-report --from-scripts
```

This writes `requirements.psd1` (which replaces module auto-detection; `pdt validate` warns if a script needs a module omitted from it) and `run.py` (which replaces the built-in PowerShell runner).

## Web apps

A web app is an HTTP server that pdt deploys behind a platform sign-in gate. Set `app_profile: web` in the app's `pdt.yml` (do not set `schedule`). Web apps deploy to Google Cloud Run behind Identity-Aware Proxy (IAP).

```yaml
app_profile: web
web:
  port: 8050          # default 8080
  health_path: /      # default /
  auth:
    provider: google
    allowed_domains: [example.com]
    allowed_groups: [sales@example.com]
storage:
  read: [warehouse]
```

A web app is either a `run.py` or a folder with a `Dockerfile` and `pdt.yml`. It must listen on `0.0.0.0` at the port given in `PDT_PORT` and respond `2xx` or `3xx` on `health_path` within 90 seconds of startup.

Visitors sign in at IAP before any request reaches your server, so the app needs no login code. Read the signed-in visitor's email with `pdt.utils.auth.current_user(headers)` (from `X-Goog-Authenticated-User-Email`; returns `""` during local `pdt run`).

`pdt run APP` serves the app on `http://127.0.0.1:<port>/` and opens it in your browser (`Ctrl+C` stops it). `pdt run APP --deployed` opens the deployed URL.

## Built-in Python helpers (`pdt.utils`)

Apps that depend on `pdt-cli[apps]` can import these public helpers directly:

### Logging (`pdt.utils.log`)

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
die(1, "missing input", path="data.csv")  # logs error and exits with code 1
```

Levels are `debug`, `info`, `warning`, and `error`. Output is human-readable text locally and one JSON object per line when `LOG_FORMAT=json` (the default on Cloud Run).

### Storage (`pdt.utils.storage`)

- `storage.store()` — Open this app's persistent store (`open()`, `fs()`, `run_folder()`, `pull()`, `push()`, `abort()`).
- `storage.sync(push_state_on_error=False)` — Context manager yielding `run.state` and `run.output` with locked state sync.
- `storage.store(app="other-app")` / `storage.read_only("other-app").fetch(remote_path, local_path)` — Read another app's files declared in `storage.read`. See [Keeping files between runs](storage.md).

### Sending email (`pdt.utils.send_email`)

```python
from pdt.utils.send_email import send_email

send_email(
    from_addr=app["config"]["email_from"],
    to_addrs="team@example.com",  # string or list of strings
    subject="Daily report",
    body="Plain-text summary.",
    html="<p>Optional HTML version.</p>",
)
```

Uses whichever transport env vars are configured (SMTP, Microsoft Graph, SES, or Resend), or prints to stdout when none are set. See [Sending email](email.md).

### Web app visitor identity (`pdt.utils.auth`)

```python
from pdt.utils.auth import current_user

email = current_user(self.headers)  # returns email behind IAP, or "" locally
```

### Microsoft Graph app-only auth (`pdt.utils.entra`)

```python
from pdt.utils.entra import graph_pages, graph_token

token = graph_token(exit_code=2)
for rows in graph_pages(token, "https://graph.microsoft.com/v1.0/users", exit_code=2):
    ...
```

Reads `PDT_AZURE_TENANT_ID`, `PDT_AZURE_CLIENT_ID`, and either `PDT_AZURE_CLIENT_SECRET` or a PFX certificate (`PDT_AZURE_PRIVATE_KEY_B64` / `PDT_AZURE_PRIVATE_KEY_PATH` + optional `PDT_AZURE_PRIVATE_KEY_PASSWORD`). See [Settings](settings.md#microsoft-graph-sign-in-env-vars).

### JSON HTTP requests (`pdt.utils.web`)

```python
from pdt.utils.web import http_json

data = http_json("https://api.example.com/items", exit_code=2, headers={"Authorization": f"Bearer {token}"})
```

Performs an HTTP request (60-second timeout) and returns decoded JSON, calling `die(exit_code, ...)` on network, HTTP, or JSON errors.
