# Sending email

Email is one of the places pdt sends alerts, next to Slack and Microsoft Teams. An app sends a report by email when it emits an alert of its own.

## Set it up

```
pdt alerts setup
```

Choose Email. pdt asks who gets the email, how to send it (Azure Communication Services, Microsoft 365, or an SMTP server), and which address it comes from. For Azure Communication Services and Microsoft 365 you sign in one time and pdt does the rest. For the other methods pdt asks for the values of the method. It writes the values to the `.env` file of the project and the entry to `pdt.yml`, then offers a test message.

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

- `method` is how pdt sends the email: `azure-communication-services`, `microsoft-365`, or `smtp`.
- `from` is the address the email comes from.
- `to` is the list of addresses that get it.
- `events` names the events that send an email. `all` is every event, and a name may hold a `*`, such as `job:*`. `exclude-events` takes events out again.

The entry sits at the top level of the project's `pdt.yml`, so it works for every app. An app that sets its own `alerts:` uses its own list.

The values of the method are env vars. Put them in the `.env` file of the project. `pdt deploy` uploads them with each app's other secrets, so no app lists them under `env:`.

## The methods

### azure-communication-services

pdt sends through Azure Communication Services Email. It needs no mailbox and no administrator consent.

In `pdt alerts setup`, choose "Azure Communication Services: no mailbox needed; pdt creates the Azure email service". pdt signs in to Azure with the same email sign-in as Microsoft 365, apart from the sign-in that `pdt deploy` uses. When the account has more than one subscription, pdt asks which one holds the email service. The provider of the project does not matter.

- **The Azure address.** Press Enter at the sender question. pdt creates, in the resource group `pdt-email`, an Email Communication Service, its Azure managed domain, and a Communication Service, each with the tag `managed-by=pdt`. The email comes from `DoNotReply@<id>.azurecomm.net`. Azure sends at most 5 messages each minute and 10 each hour from an Azure address, for the whole subscription, and does not raise that limit ([Microsoft: service limits](https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits#email)).
- **Your own domain.** Type an address of your domain. When the subscription holds a verified email domain with that name, pdt uses the Communication Service that sends from it, or links the domain to its own Communication Service. Otherwise pdt creates the domain. Azure then needs four DNS records: a TXT record that proves you own the domain, an SPF TXT record, and two DKIM CNAME records. When the domain has an Azure DNS zone in the same subscription, pdt adds the records and starts the verification. Otherwise pdt prints the records; add them at your DNS provider. Azure verifies a domain in 15 to 30 minutes. Run `pdt alerts setup` again: it starts or checks the verification, and finishes the setup when the domain is verified. When the user name of the address is not `DoNotReply`, pdt adds it to the domain. A custom domain sends at most 30 messages each minute and 100 each hour, and Microsoft can raise that limit.

pdt writes `PDT_ACS_CONNECTION_STRING` to `.env`. A second run finds each resource and changes nothing. pdt never changes a resource without the tag `managed-by=pdt`.

### microsoft-365

pdt sends through Microsoft Graph as an app registration that signs in with a certificate. `from` is the mailbox that sends. It must be a mailbox of your Microsoft 365 organization, for example a shared mailbox; pdt does not create it.

#### The guided setup

In `pdt alerts setup`, choose "Microsoft 365: sign in once, and pdt does the setup" and type the sender address. pdt keeps a Microsoft sign-in for email apart from the Azure sign-in that `pdt deploy` uses (`pdt az account show` does not change), so the email organization can be another one than the deploy account. The first time, pdt opens the Microsoft sign-in of the organization of the sender address in your browser. Sign in as an administrator. A later setup asks whether to use that account again or to sign in with another account. The provider of the project does not matter. pdt then checks that the sender address is a mailbox of the organization (a user or a shared mailbox, by its user name, mail, or an alias); when it is not, pdt changes nothing and asks for the sender address again. pdt then makes sure of three items in your organization, and prints one line for each:

- the app registration `pdt-email`, for this organization only;
- a certificate on that registration. Its private key is the file `.secrets/pdt-email.pfx` in the project. git ignores that folder, and the file never goes into an image. The certificate is valid for 10 years;
- the Microsoft Graph application permission `Mail.Send`, granted for the organization.

pdt writes `PDT_EMAIL_TENANT_ID`, `PDT_EMAIL_CLIENT_ID`, and `PDT_EMAIL_PRIVATE_KEY_PATH` to `.env`. A second run finds each item and changes nothing. pdt uses only a registration that it created, or the one that `PDT_EMAIL_CLIENT_ID` in `.env` names. When another registration named `pdt-email` exists, the setup stops, changes nothing, and says what you can do. When the sign-in is for another organization than the `PDT_EMAIL_TENANT_ID` in `.env`, the setup asks whether to replace the saved settings with those of the new organization. With yes, pdt finds or creates the registration in the new organization as in a first setup, puts the certificate `.secrets/pdt-email.pfx` on it, writes the new values to `.env`, and removes `PDT_EMAIL_PRIVATE_KEY_B64` from `.env` when it is there. With no, pdt changes nothing and the setup stops.

Microsoft can take a short time to accept a new permission. The test message of the setup waits for a maximum of two minutes and tries again during that time.

When your account cannot create the registration or grant the permission, pdt names the role that is missing and prints a link. An administrator opens the link and approves the permission. Then run `pdt alerts setup` again; it continues with what is already there.

#### What the permission allows

The `Mail.Send` application permission lets the registration `pdt-email` send as each mailbox of the organization, not only as the sender address. Keep `.secrets/pdt-email.pfx` and the `.env` file as safe as a password.

To limit the registration to one mailbox, Microsoft documents [role based access control for applications in Exchange Online](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac). An administrator with the Exchange Administrator role runs these commands in Exchange Online PowerShell, after `Connect-ExchangeOnline`:

```powershell
# The AppId and the ObjectId come from Enterprise applications > pdt-email in the Microsoft Entra admin center, not from App registrations.
New-ServicePrincipal -AppId <application-id> -ObjectId <object-id> -DisplayName "pdt-email"
New-ManagementScope -Name "pdt-alerts" -RecipientRestrictionFilter "<filter>"
New-ManagementRoleAssignment -App <object-id> -Role "Application Mail.Send" -CustomResourceScope "pdt-alerts"
Test-ServicePrincipalAuthorization -Identity <object-id> -Resource pdt-alerts@example.com
```

`<filter>` is a filter expression that matches only the sender mailbox. The Microsoft page links the list of [filterable properties](https://learn.microsoft.com/en-us/powershell/exchange/recipientfilter-properties); its own example is `"CustomAttribute1 -eq '012332'"`. In the result of the last command, `InScope` is `True` when the permission applies to that mailbox.

The Exchange assignment acts in addition to the permission that Microsoft Entra ID granted. The registration can send as each mailbox until that grant is gone, so revoke it: sign in to the Microsoft Entra admin center as at least a Cloud Application Administrator, go to Entra ID > Enterprise apps > All applications > pdt-email > Permissions, open the Admin consent tab, select `...` next to `Mail.Send`, and select Revoke permission ([Microsoft: review permissions granted to enterprise applications](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/manage-application-permissions)). Exchange keeps app permissions in a cache for 30 minutes to 2 hours, so a change can take that long.

After this change, do not let `pdt alerts setup` replace the email entry: the guided setup grants `Mail.Send` in Microsoft Entra ID again.

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

For Amazon SES, use this method. In the Amazon SES console, choose SMTP settings, then Create SMTP Credentials. AWS shows an SMTP user name and an SMTP password one time; they are `PDT_SMTP_USER` and `PDT_SMTP_PASSWORD`. The SMTP password is different from an AWS secret access key, and the credentials work in one AWS Region only. `PDT_SMTP_HOST` is the SMTP endpoint of that Region; the [list of SES endpoints](https://docs.aws.amazon.com/general/latest/gr/ses.html) has each one. The endpoint takes port 587 with STARTTLS and port 465 with implicit TLS. See [AWS: obtaining Amazon SES SMTP credentials](https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html) and [AWS: connecting to an Amazon SES SMTP endpoint](https://docs.aws.amazon.com/ses/latest/dg/smtp-connect.html).

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

`pdt.utils.send_email.send_email(from_addr, to_addrs, subject, body, html="")` still works, with a `DeprecationWarning`. It emits the event `email:send`, and it sends nothing by itself.

An email entry sends a `send_email` call only when its `events` list holds `email:send`. An entry with `events: [all]` also receives it. `pdt alerts setup` does not add that name. Add it to the entry in `pdt.yml` by hand:

```yaml
    events: [app:deploy-completed, app:deploy-failed, job:run-failed, email:send]
```

The entry then sends the email to `to_addrs`, from the `from` of the entry; `from_addr` is not used. Without `email:send` in an entry, the run's output shows the warning and one `[alert]` line with the subject, and no email goes out. Use `alert.emit` in new code.
