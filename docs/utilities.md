# Utilities for your apps

Back to the [README](../README.md).

`pdt.utils` provides built-in support for common functionality:

- Send emails
- Logging

## Send Email

`pdt.utils.send_email` sends plain-text mail.

Supports:

- SMTP + password
- SMTP + OAuth for Microsoft or Google
- Microsoft Graph delegated Mail.Send
- SES (Amazon Simple Email Service)
- Resend

### Configuration

#### SMTP

Required environment variables:

- PDT_SMTP_HOST
- PDT_SMTP_USER

When using password auth:

- PDT_SMTP_PASSWORD

When using OAuth for Google or Microsoft:

- PDT_SMTP_OAUTH_CLIENT_ID

Optional:

- PDT_SMTP_AUTH = oauth2
- PDT_SMTP_PORT: Port 587 is the default and uses STARTTLS; port 465 uses implicit TLS.

Google also requires:

- PDT_SMTP_OAUTH_CLIENT_SECRET

Microsoft optionally accepts:

- PDT_SMTP_OAUTH_TENANT_ID: Defaults to `common`.

PDT infers OAuth when the host is `smtp.gmail.com`, `smtp.office365.com`, or `smtp-mail.outlook.com` and no password is set. If a provider revokes authorization, run or deploy the app from a terminal again.

##### Google OAuth

Create a Desktop app OAuth client in the [Google Auth Platform](https://console.cloud.google.com/auth/clients). Set its client ID and client secret in the variables above. The OAuth consent screen must include `https://mail.google.com/` because Google requires that scope for SMTP OAuth.

##### Microsoft OAuth

Create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the delegated Office 365 Exchange Online permission `https://outlook.office.com/SMTP.Send` and allow public client flows.

Microsoft 365 must also have Authenticated SMTP enabled for the sending mailbox. Use `PDT_SMTP_OAUTH_TENANT_ID` for a tenant-specific registration, or leave it empty to use `common`.

#### Microsoft Graph

Required environment variables:

- PDT_GRAPH_MAIL_USER
- PDT_GRAPH_MAIL_CLIENT_ID

Optional:

- PDT_GRAPH_MAIL_TENANT_ID: Defaults to `common`.

PDT sends as `PDT_GRAPH_MAIL_USER`, so `email_from` in config.yml must match it. Graph sends the HTML part alone; the other transports send both parts. If a provider revokes authorization, run or deploy the app from a terminal again.

Create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the Microsoft Graph delegated permission `Mail.Send` and allow public client flows. Do not add a client secret.

#### SES

Required environment variables:

- PDT_SES_REGION

Optional:

- PDT_SES_ACCESS_KEY_ID
- PDT_SES_SECRET_ACCESS_KEY

Leave the two SES key variables empty to use the standard AWS credential chain (for example, `AWS_PROFILE`, environment credentials, or a workload role).

#### Resend

Required environment variables:

- PDT_RESEND_API_KEY

## Logging

`pdt.utils.log` writes one log line per event:

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
```

Levels are `debug`, `info`, `warning`, and `error`. Locally a line is human-readable text on stdout; with `LOG_FORMAT=json` it is one JSON object per line, which is also the default on Cloud Run.
