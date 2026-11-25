# Alerts

pdt can send lifecycle notifications for deploys, destroys, secret updates, pauses, and job runs to Slack or Microsoft Teams—either for the whole project or overridden per app.

## Set up alerts interactively

Run the setup wizard from your project folder:

```
pdt alerts setup
```

`pdt alerts setup` asks which alert destinations you want (Slack or Microsoft Teams) and which events each destination should receive, writes the `alerts:` list into `pdt.yml`, stores the secret webhook URLs in `.env` (never in YAML), and offers to send a test message.

To verify your configured alerts at any time:

```
pdt alerts test
```

When you deploy an app with `pdt deploy`, pdt automatically includes the alert webhook env vars in the app's cloud secrets—you do not need to list webhook variables under `env:`.

## Configure alerts in `pdt.yml`

Put `alerts:` at the top level of the project's `pdt.yml` to apply to every app, or in an app's own `pdt.yml` to replace the project list for that app. Set `alerts: false` to disable all alert events.

```yaml
# pdt.yml
alerts:
  - type: slack
    webhook: B0123ABC
    name: "#it-alerts"
    events: [all]
    exclude-events: [job:run-started, job:run-completed]
  - type: teams
    webhook: 0123456789abcdef
    name: IT alerts
    events: ["app:*", job:run-failed]
  - type: console
    events: [unhandled]
```

Keep the matching webhook URLs in `.env` (or your CI variables):

```
SLACK_WEBHOOK_B0123ABC=https://hooks.slack.com/services/T0123ABC/B0123ABC/replace-this-secret
TEAMS_WEBHOOK_0123456789ABCDEF=https://example.api.powerplatform.com/workflows/0123456789abcdef/replace-this-secret
```

For the full list of alert keys, wildcard rules (`all`, `job:*`, `app:*`, `unhandled`), and every event name, see [Settings: `alerts:` entries](settings.md#alerts-entries).
