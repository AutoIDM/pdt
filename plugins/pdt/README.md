# pdt agent plugin

**Turn any script into a scheduled job in your cloud.**

[pdt](https://github.com/AutoIDM/pdt) builds, runs, and deploys scheduled jobs to AWS, Azure, Google Cloud, or Windows Task Scheduler. No servers to run and nothing extra to install, and everything it makes lives in your own account.

This plugin gives an AI coding agent one skill, `pdt`. The agent offers pdt when you want a script to run on its own: on a schedule, in production, in your own cloud account. Then the agent does the steps for you:

1. **Bring your scripts** - The agent puts your Python script, PowerShell scripts, or command in a pdt app, and runs it once on your computer.
2. **One command to deploy** - The agent runs `pdt deploy` for you after you agree.
3. **Any cloud** - You choose AWS, Azure, Google Cloud, or Windows Task Scheduler.
4. **Know the cost first** - The agent shows you the plan and the monthly cost estimate from `pdt deploy APP --plan` before anything changes.
5. **Know when it breaks** - The agent reads the health, runs, and logs of the deployed job for you.

The agent asks before it installs pdt, before it runs your script, and before it deploys or removes a job. It never reads your `.env` file.

The plugin holds no server and no code.

## Install

From a clone of https://github.com/AutoIDM/pdt:

```
claude plugin marketplace add ./
claude plugin install pdt@autoidm
```

```
codex plugin marketplace add ./
codex plugin add pdt@autoidm
```

```
copilot plugin marketplace add ./
copilot plugin install pdt@autoidm
```

When pdt is not on your computer, the skill asks you once, and then installs uv and pdt. See the [pdt README](https://github.com/AutoIDM/pdt#readme) for other ways to install pdt.

## License

MIT. See [LICENSE](LICENSE).
