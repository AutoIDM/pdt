You are rebasing one merge request branch in a GitLab CI job. No human is present. Read AGENTS.md for the repo's rules before you change anything.

Steps:

1. `git fetch origin`, then `git checkout -B <branch> origin/<branch>`.
2. `git rebase origin/<target>`.
3. When the rebase stops on a conflict, resolve each conflicted file so the branch's intent applies on top of the new base. Keep both sides where they are independent. Then `git add` the files and `git rebase --continue`. Repeat until the rebase finishes.
4. When the rebase completes, run `uv run --group dev pytest -q` and `uv run --group dev ruff check`. If they fail because of your conflict resolution, fix the resolution. If they fail for reasons unrelated to the rebase, still push and say so in your summary.
5. `git push --force-with-lease origin <branch>`.

Rules:

- Never amend, squash, reset, or add commits. The branch must end with the same commits it started with, rebased.
- Never push without `--force-with-lease`.
- Do not edit files that have no conflict markers unless a resolution needs it to compile or pass tests.
- If a conflict cannot be resolved with confidence, run `git rebase --abort`, do not push, and say which file and why. The pipeline reports it and a person takes over.

End with a short summary: whether you pushed, which files had conflicts (say "no conflicts" if none), and the test results.
