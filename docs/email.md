# Sending email

Email is one of the places pdt sends alerts, next to Slack and Microsoft Teams. An app sends a report by email when it emits an alert of its own.

## Set it up

```
pdt alerts setup
```

Choose Email. pdt asks who gets the email, how to send it (an SMTP server, Amazon SES, or Resend), which address it comes from, and the values of that method. It writes the values to the `.env` file of the project and the entry to `pdt.yml`, then offers a test message.

```
pdt alerts test
```

sends the test message to every Slack, Teams, and email entry again.

## The entry in pdt.yml

```yaml
alerts:
  - type: email
    method: smtp
    from: pdt-alerts@example.com
    to: [it-ops@example.com]
    events: [job:run-failed, compliance:changed]
```

- `method` is how pdt sends the email: `smtp`, `ses`, `resend`, or `microsoft-365`.
- `from` is the address the email comes from.
- `to` is the list of addresses that get it.
- `events` names the events that send an email. `all` is every event, and a name may hold a `*`, such as `job:*`. `exclude-events` takes events out again.

The entry sits at the top level of the project's `pdt.yml`, so it works for every app. An app that sets its own `alerts:` uses its own list.

The values of the method are env vars. Put them in the `.env` file of the project. `pdt deploy` uploads them with each app's other secrets, so no app lists them under `env:`.

## The methods

### smtp

- `PDT_SMTP_HOST`
- `PDT_SMTP_USER`
- `PDT_SMTP_PASSWORD`
- `PDT_SMTP_PORT` (optional): 587 is the default and uses STARTTLS; 465 uses implicit TLS.

pdt signs in with the user name and the password.

### ses

- `PDT_SES_REGION`
- `PDT_SES_ACCESS_KEY_ID` and `PDT_SES_SECRET_ACCESS_KEY` (optional): leave both empty to use the standard AWS credential chain, for example `AWS_PROFILE` or the role of the job.

This method needs the `boto3` package. An app's `run.py` has it through `pdt-cli[apps]`, so an alert that the app emits is sent. The pdt command and the events of a deployed job that pdt emits itself (`job:run-failed` and the others) run without `boto3`, so pdt prints those on the console with a warning.

### resend

- `PDT_RESEND_API_KEY`

### microsoft-365

pdt sends through Microsoft Graph as an app registration that signs in with a certificate.

- `PDT_EMAIL_TENANT_ID`
- `PDT_EMAIL_CLIENT_ID`
- `PDT_EMAIL_PRIVATE_KEY_PATH`: the path of a `.pfx` file with no password that holds the certificate and its private key. A relative path starts at the project folder. `pdt deploy` uploads the file's content as `PDT_EMAIL_PRIVATE_KEY_B64`; you can also set that variable yourself, to the base64 of the file.

The app registration needs the Microsoft Graph application permission `Mail.Send` with admin consent, and the certificate under Certificates & secrets. `from` is the mailbox that sends. `pdt alerts setup` does not list this method yet; write the entry and the values by hand.

Microsoft Graph takes one body, so an email with an HTML body goes out as HTML only.

## Send an alert from an app

```python
from pdt.utils import alert

alert.emit("compliance:changed", title="Compliance changed",
           text="3 devices changed.", html="<p>3 devices changed.</p>",
           url="https://example.com/report", devices=3)
```

`alert.emit` sends one event with the name you choose. Every entry under `alerts:` whose `events` match that name gets it. Add the name to the `events` of the email entry to get it by email.

- `title` is the subject of the email, and the title of the Slack or Teams message.
- `text` is the plain body of the email, and the text of the Slack or Teams message.
- `html` is the HTML body of the email. Slack and Teams do not show it.
- `url` is the "Open" button in Slack and Teams.
- Every other keyword, such as `devices=3`, is a labelled field in Slack and Teams.

All four are optional. An event with no `text` and no `html` gets an email body that pdt builds from the title, the fields, and the url.

The app does not wait for the email. pdt waits for the delivery when the app ends. A delivery that fails prints a warning and the alert line in the run's output, and does not change the result of the run.

When no entry lists the event name, the run's output shows one line, `[alert] Compliance changed`, and nothing is sent.

## send_email

`pdt.utils.send_email.send_email(from_addr, to_addrs, subject, body, html="")` still works, with a `DeprecationWarning`. It emits the event `email:send`. An email entry with `email:send` in its `events` sends it to `to_addrs`, from the `from` of the entry. Use `alert.emit` in new code.
