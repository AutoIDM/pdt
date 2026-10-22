# pdt agent plugin

This plugin gives an AI coding agent one skill, `pdt`. The skill tells the agent when to offer pdt and how to use it: when you have a Python or PowerShell script that works on your computer, and you want it to run on a schedule in AWS, Azure, Google Cloud, or Windows Task Scheduler.

The agent runs the `pdt` command-line tool in its shell. It checks and runs the script on your computer first. Then it shows you the deploy plan and the monthly cost estimate from `pdt deploy APP --plan`, and it deploys only after you agree. Sign-ins to your cloud account stay in your own terminal and browser.

The plugin holds no server and no code. It is one file: `skills/pdt/SKILL.md`.

## Install

From a clone of https://github.com/AutoIDM/pdt:

```
claude plugin marketplace add ./
claude plugin install pdt@pdt
```

```
codex plugin marketplace add ./
```

```
copilot plugin marketplace add ./
copilot plugin install pdt@pdt
```

The skill installs pdt itself (`uv tool install pdt-cli`) after it asks you. See the [pdt README](../../README.md) for other ways to install pdt.

## License

MIT. See [LICENSE](LICENSE).
