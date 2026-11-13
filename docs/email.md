# Sending email

Email is one of the places pdt sends alerts, next to Slack and Microsoft Teams. An app sends a report by email when it emits an alert of its own.

## Set it up

```
pdt alerts setup
```

Choose Email. pdt asks who gets the email, how to send it (Microsoft 365, an SMTP server, Amazon SES, or Resend), and which address it comes from. For Microsoft 365 you sign in one time as an administrator and pdt does the rest. For the other methods pdt asks for the values of the method. It writes the values to the `.env` file of the project and the entry to `pdt.yml`, then offers a test message.

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

### microsoft-365

pdt sends through Microsoft Graph as an app registration that signs in with a certificate. `from` is the mailbox that sends. It must be a mailbox of your Microsoft 365 organization, for example a shared mailbox; pdt does not create it.

#### The guided setup

In `pdt alerts setup`, choose "Microsoft 365: sign in once, and pdt does the setup" and type the sender address. pdt uses the Azure sign-in of this computer, or opens the Microsoft sign-in in your browser when there is none. Sign in as an administrator. The provider of the project does not matter. pdt then makes sure of three items in your organization, and prints one line for each:

- the app registration `pdt-email`, for this organization only;
- a certificate on that registration. Its private key is the file `.secrets/pdt-email.pfx` in the project. git ignores that folder, and the file never goes into an image. The certificate is valid for two years;
- the Microsoft Graph application permission `Mail.Send`, granted for the organization.

pdt writes `PDT_EMAIL_TENANT_ID`, `PDT_EMAIL_CLIENT_ID`, and `PDT_EMAIL_PRIVATE_KEY_PATH` to `.env`. A second run finds each item and changes nothing.

Microsoft can take a short time to accept a new permission. The test message of the setup waits for a maximum of two minutes and tries again during that time.

When your account cannot create the registration or grant the permission, pdt names the role that is missing and prints a link. An administrator opens the link and approves the permission. Then run `pdt alerts setup` again; it continues with what is already there.

#### What the permission allows

The `Mail.Send` application permission lets the registration `pdt-email` send as each mailbox of the organization, not only as the sender address. Keep `.secrets/pdt-email.pfx` and the `.env` file as safe as a password.

To limit the registration to one mailbox, an Exchange administrator runs these commands in Exchange Online PowerShell:

```powershell
Connect-ExchangeOnline
# AppId and ObjectId come from Entra > Enterprise applications > pdt-email (not App registrations).
New-ServicePrincipal -AppId <application-id> -ObjectId <enterprise-app-object-id> -DisplayName "pdt-email"
New-ManagementScope -Name "pdt-alerts" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'pdt-alerts@example.com'"
New-ManagementRoleAssignment -App <enterprise-app-object-id> -Role "Application Mail.Send" -CustomResourceScope "pdt-alerts"
Test-ServicePrincipalAuthorization -Identity <enterprise-app-object-id> -Resource pdt-alerts@example.com
```

The test result must show `InScope` = `True`. Then remove the `Mail.Send` permission of `pdt-email` in Entra (App registrations > pdt-email > API permissions), because the Exchange scope does not limit a permission that Entra granted ([Microsoft: role based access control for applications](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)). Exchange keeps app permissions in a cache for 30 minutes to 2 hours, so a send can fail during that time. After this change, do not let `pdt alerts setup` replace the email entry: the guided setup grants `Mail.Send` in Entra again.

Not confirmed: that `PrimarySmtpAddress` is a valid filter property for this scope. `Test-ServicePrincipalAuthorization` shows whether the scope works.

#### The values by hand

- `PDT_EMAIL_TENANT_ID`
- `PDT_EMAIL_CLIENT_ID`
- `PDT_EMAIL_PRIVATE_KEY_PATH`: the path of a `.pfx` file with no password that holds the certificate and its private key. A relative path starts at the project folder. `pdt deploy` uploads the file's content as `PDT_EMAIL_PRIVATE_KEY_B64`; you can also set that variable yourself, to the base64 of the file.

The app registration needs the Microsoft Graph application permission `Mail.Send` with admin consent, and the certificate under Certificates & secrets. Write the entry with `method: microsoft-365` in `pdt.yml`.

Microsoft Graph takes one body, so an email with an HTML body goes out as HTML only.

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

## Send an alert from an app

```python
from pdt.utils import alert

alert.emit("compliance:changed", title="Compliance changed",
           text="3 devices changed.", html="<p>3 devices changed.</p>",
           url="https://example.com/report", devices=3)
```

`alert.emit` sends one event with the name you choose. Every entry under `alerts:` whose `events` match that name gets it. Add the name to the `events` of the email entry to get it by email.

- `title` is the subject of the email. Slack, Teams, and the console show it after the name of the app, as `<app>: <title>`.
- `text` is the plain body of the email, and the text of the Slack or Teams message.
- `html` is the HTML body of the email. Slack and Teams do not show it.
- `url` is the "Open" button in Slack and Teams.
- Every other keyword, such as `devices=3`, is a labelled field in Slack and Teams.

All four are optional. An event with no `text` and no `html` gets an email body that pdt builds from the title, the fields, and the url.

The app does not wait for the email. pdt waits for the delivery when the app ends. A delivery that fails prints a warning and the alert line in the run's output, and does not change the result of the run.

When no entry lists the event name, the run's output shows one line, `[alert] my-app: Compliance changed` for the app `my-app`, and nothing is sent.

## send_email

`pdt.utils.send_email.send_email(from_addr, to_addrs, subject, body, html="")` still works, with a `DeprecationWarning`. It emits the event `email:send`. An email entry with `email:send` in its `events` sends it to `to_addrs`, from the `from` of the entry. Use `alert.emit` in new code.
