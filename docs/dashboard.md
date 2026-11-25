# The dashboard

`pdt gui` starts a local web server on `localhost` and opens your project's dashboard in your browser. It has no login and never talks to the cloud directly: every number comes from running `pdt` commands (`pdt runs`, `pdt logs`, `pdt storage ls`), and every button runs one (`pdt pause`, `pdt unpause`, `pdt run --deployed`). Hover any button to see the exact command it runs.

## What the dashboard shows

- **Project overview:** The home page shows `pdt health` as a table with a run-history grid underneath (by hour, day, week, or month).
- **App history and logs:** Click an app to see its runs, and click a run to read its log and browse the files it wrote to storage.
- **CSV SQL workbench:** Clicking any CSV file kept by a run opens an in-browser SQL workbench powered by DuckDB. You can sort, filter, and page the table, or write SQL that queries and joins every CSV in that run—with the matching `pdt storage APP query` command shown underneath. On first launch, `pdt gui` downloads DuckDB-WASM once into pdt's data folder (`~/.local/share/pdt` or `%LOCALAPPDATA%\pdt`). A Download link next to each file still saves it directly.
- **Performance stats:** The Stats link shows how long each page load and underlying `pdt` command took.

## Caching and background refresh

The dashboard caches fetched run history, logs, and file lists in `.pdt/gui.sqlite3` inside your project folder, so runs remain visible even after the cloud provider's log retention expires. Data refreshes automatically when older than five minutes or when you click **Refresh**.

A background worker inside the server checks every two minutes: it refreshes each app's run list, then backfills logs and file listings for runs that lack them (newest first). Pages render cached data immediately and only wait when you open a run the worker has not fetched yet.

## Command options

```
pdt gui
pdt gui --background
pdt gui --stop
pdt gui --port 8765
pdt gui --no-browser
```

- `--background` leaves the server running after the command returns; `--stop` ends a background server.
- `--port N` picks a custom localhost port; `--no-browser` starts without opening a browser tab.
