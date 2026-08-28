# salesforce-netsuite-customer-sync

This app keeps NetSuite customers and contacts in step with Salesforce accounts and contacts. Salesforce is the system of record. Every Salesforce Account becomes a NetSuite customer, and every Salesforce Contact becomes a NetSuite contact attached to that customer. The NetSuite `externalId` field holds the Salesforce record id, so the two systems stay joined without a mapping table.

It launches like any other pdt app. `run.py` is the entry point pdt calls, but the folder is a complete Meltano project in the same shape as AutoIDM's customer repositories: `meltano.yml`, `transform/` for dbt, `autoidm-transform/` for the Python business logic, and `alembic/` for the state database.

## What gets written

The sync sends these NetSuite customer fields: `companyname` from Account Name, `phone`, `fax`, `url` from Website, and `comments` from Description. It sends these NetSuite contact fields: `firstname`, `lastname`, `salutation`, `title`, `email`, `phone`, `mobilephone`, `fax`, and `company`, which holds the NetSuite internal id of the customer the contact belongs to.

Two rules decide what the sync leaves alone. A NULL in a desired column means the field is unmanaged, and `transform/macros/autoidm_matcher.sql` reports no difference for it. To clear a field that already holds a value, the transform writes the string `_blank_`, and it writes that sentinel only when NetSuite actually holds a value to clear.

A contact whose Salesforce Account has no NetSuite customer yet gets a NULL `company`, so the contact syncs without a parent and the transform records a send-once notification. The next run links the contact, because the customer exists by then.

## The four Meltano jobs

- `extract` runs the Alembic migration, drops the two source schemas, then loads `tap-salesforce` and `tap-netsuite` into Postgres through `target-postgres`.
- `transform` runs `dbt:pre_python` to build the four staging models, then `autoidm-transform` to build the desired state, then `dbt:post_python` to build the match and target tables.
- `artifacts` exports the staging, match, and target tables to CSV through `target-csv-artifacts`, so a person can review the pending changes before a load.
- `load` reads the four `netsuite_*_target_*` tables with `tap-postgres`, writes them to `target-netsuite`, sends the notification tables through `target-apprise`, then marks the send-once notifications as sent.

`run.py` runs `meltano install`, then `meltano run --force extract transform load`. It does not run `artifacts`; run that one by hand when you want the CSV review files.

## Running it locally

1. Start Postgres. The customer repositories use a container on port 5432 with user and password `postgres`.
2. Copy the names in `env.template` into a `.env` file at this folder or at your project root, and fill them in.
3. Run `pdt run salesforce-netsuite-customer-sync`, or work inside this folder with Meltano directly: `meltano --environment dev install`, then `meltano --environment dev run --force extract transform load`.
4. To see what the sync would change without writing to NetSuite, run `meltano --environment dev run --force extract transform artifacts` and read the CSV files under `artifacts/`.

Set `meltano_environment` in `config.yml` to choose which environment `run.py` uses. `dev` points at a Salesforce sandbox and a NetSuite sandbox account, `prod` points at both production systems, and `ci` builds a per-merge-request database.

`meltano install` runs at the start of every job because pdt's Dockerfile has no build hook for it. On AWS Fargate, Azure Container Apps, or Cloud Run, every cold start pays that install cost before any data moves.

## Three gaps to close before this runs against a real account

**`tap-netsuite` has no `customer` or `contact` stream.** Its streams today are `timesheet`, `timebill`, `employee`, `location`, `file_cabinet`, and `transaction`. `transform/models/netsuite/stg_netsuite_customer.sql` and `stg_netsuite_contact.sql` assume streams named `customer` and `contact` that land as `tap_netsuite.customer` and `tap_netsuite.contact`, each with an `externalId` column. Those two streams have to be written first.

**`target-netsuite` has no generic record sink.** `get_sink_class` in `target_netsuite/target.py` returns a sink only for a stream name containing `timesheet` or `timebill`, and raises for anything else. The four streams this app sends are `netsuite_customer_target_create`, `netsuite_customer_target_update`, `netsuite_contact_target_create`, and `netsuite_contact_target_update`. A sink for those has to map the stream name to a NetSuite record type, and it has to map the lower case column names this project produces back to the camel case field names the NetSuite REST API expects, for example `companyname` to `companyName`. The dispatch contract itself is already met: every record carries `_autoidm__action` set to `CREATE` or `UPDATE`.

**There are no `plugins/*.lock` files.** `meltano lock` needs network access to the Meltano Hub, so the lock files are absent here. The first `meltano install` resolves `tap-salesforce`, `tap-postgres`, and `target-postgres` from the Hub and writes the locks. Run `meltano lock --all` once and commit the result if you want the plugin versions pinned.

## Two details worth knowing before you edit the dbt models

Every desired row carries the action twice, as `action` and as `_autoidm__action`. `transform/macros/autoidm_update.sql` is vendored from the copier template unchanged, and it filters on a column named `action`. `target-netsuite` dispatches on `_autoidm__action`. The match models pass `_autoidm__action` to `autoidm_matcher` as an ignored column, and the macro ignores `action` on its own.

Every column in this project is lower case. The vendored macros write unquoted identifiers, which Postgres folds to lower case, so a camel case column would not survive the matcher. This is why the NetSuite sink has to restore the field name casing.
