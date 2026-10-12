"""The `pdt gui` web app: a Django project that shows what pdt knows.

Every fact on a page came out of a pdt command run in a child process
(`pdt runs`, `pdt logs`, `pdt storage ls`, `pdt pause`, `pdt run
--deployed`), so the page and the terminal always agree, and the GUI
never talks to a cloud itself. Answers are kept in SQLite under the
project's .pdt folder and refreshed when they are older than
`sync.STALE`, or when the user asks.

`gui_server.py` starts it; `gui_cli.py` is the `pdt gui` command that
starts the server and opens the browser.
"""
