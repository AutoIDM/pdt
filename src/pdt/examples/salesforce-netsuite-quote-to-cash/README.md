# salesforce-netsuite-quote-to-cash

This app turns a won Salesforce deal into NetSuite paperwork. When an Opportunity reaches the `Closed Won` stage, the app reads its Account, its Contacts, its OpportunityLineItems and the Products behind them, then upserts the customer and contacts, creates a sales order, creates an invoice against that sales order, and writes the three new NetSuite internal ids back onto the Salesforce Opportunity. Salesforce is the system of record. The NetSuite `externalId` field holds the Salesforce record id, so the two systems stay joined without a mapping table.

It launches like any other pdt app. `run.py` is the entry point pdt calls, but the folder is a complete Meltano project in the same shape as AutoIDM's customer repositories: `meltano.yml`, `transform/` for dbt, `autoidm-transform/` for the Python business logic, and `alembic/` for the state database.

The customer and contact half of this app is the same as `salesforce-netsuite-customer-sync`. If you only want accounts and contacts in NetSuite, use that app instead.

## What gets written

**NetSuite customer**, from Salesforce Account: `companyname` from Name, `phone`, `fax`, `url` from Website, and `comments` from Description.

**NetSuite contact**, from Salesforce Contact: `firstname`, `lastname`, `salutation`, `title`, `email`, `phone`, `mobilephone`, `fax`, and `company`, which holds the NetSuite internal id of the customer the contact belongs to.

**NetSuite sales order**, one per Closed Won Opportunity: `entity` holds the customer internal id, `trandate` comes from CloseDate, `memo` from Name, and `item` holds every line.

**NetSuite invoice**, one per sales order that already exists in NetSuite: the same `entity`, `trandate` and `memo`, plus `createdfrom`, which points at the sales order.

**Salesforce Opportunity**, three custom fields: the NetSuite customer id, sales order id and invoice id. Name them in `config.yml`. The writeback fills each field as soon as its id is known, so an opportunity that is not Closed Won still gets the customer id of its account, and the other two fields stay empty until the order and the invoice exist.

Two rules decide what the sync leaves alone. A NULL in a desired column means the field is unmanaged, and `transform/macros/autoidm_matcher.sql` reports no difference for it. To clear a field that already holds a value, the transform writes the string `_blank_`, and it writes that sentinel only when NetSuite actually holds a value to clear.

## What the app skips, and why

A contact whose Salesforce Account has no NetSuite customer yet gets a NULL `company`, so the contact syncs without a parent and the transform records a send-once notification. The next run links the contact, because the customer exists by then.

An opportunity whose Account has no NetSuite customer yet produces no sales order at all, because a NetSuite sales order needs an `entity`. The transform prints the reason and records a send-once notification. The next run creates the order.

An opportunity with one line whose Product resolves to no NetSuite inventory item produces no sales order at all. Holding the whole order back is deliberate: a partial order would look complete and would bill the customer for less than they bought. The notification names the product so a person can create the item.

A Closed Won opportunity with no line items produces no sales order, because NetSuite rejects an order with no lines. The transform prints the reason and sends nothing, the same way it skips an account with no company name.

An opportunity gets an invoice only once its sales order exists in NetSuite, so the invoice always lands one run after the order. That skip is silent, because it is the normal first-run state and the two failures above already notify.

## Matching a Salesforce Product to a NetSuite inventory item

`lookup_inventory_item` in `autoidm-transform/autoidm_transform/salesforce2netsuite/desired_netsuite.py` matches `ProductCode` against the NetSuite item's `itemid`. A stock Salesforce org leaves `ProductCode` null on every Product, so when the product code is empty the lookup falls back to matching `Name` against the item's `displayname`. Two NetSuite items matching one product raise, because a silent pick would put the wrong item on an invoice.

## Two modelling decisions worth knowing before you edit anything

**Sales order lines are one JSON column.** `python_desired_netsuite_salesorder` holds one row per opportunity, and its `item` column holds a JSON array of line objects, each with the resolved NetSuite inventory item id, quantity, rate and description. The matcher compares that column as one value, so any change to any line makes the order differ and the whole order is resent. That is the behaviour we want. It does mean the comparison is text equality: the `item` column arriving from NetSuite has to hold the same JSON, with the same keys and the same line order, or every run will see a difference and resend the order. See the gaps below.

**The invoice is a plain record, not a `!transform` call.** A Singer target writes records; it cannot make a NetSuite transform request. So `python_desired_netsuite_invoice` carries `createdfrom` pointing at the sales order's NetSuite internal id, which links the invoice to its order while staying an ordinary record write.

**Every reference column holds a bare internal id.** `entity`, `company` and `createdfrom` all hold the NetSuite internal id as a plain value, never the reference map `{"id": "<internal id>"}`. That keeps one rule for the sink, which wraps each of them on write, and it lets a reference column be compared against the staged current state, which reads the same bare id back out of NetSuite. Confirm the wrapped shape each field expects in the NetSuite REST API Browser against your account's version before the first production run.

`netsuite_invoice_match.sql` passes `createdfrom` to `autoidm_matcher` as an ignored column, so the matcher never compares it. NetSuite fixes `createdFrom` when the invoice is created and does not let a later update change it, so comparing it would only produce a difference nobody can act on. The column still reaches `netsuite_invoice_target_create`, because `autoidm_update` selects every desired column whatever the matcher ignores.

## The four Meltano jobs

- `extract` runs the Alembic migration, drops the two source schemas, then loads `tap-salesforce` and `tap-netsuite` into Postgres through `target-postgres`.
- `transform` runs `dbt:pre_python` to build the ten staging models, then `autoidm-transform` to build the desired state, then `dbt:post_python` to build the match and target tables.
- `artifacts` exports the staging, match, and target tables to CSV through `target-csv-artifacts`, so a person can review the pending changes before a load.
- `load` reads the eight `netsuite_*_target_*` tables with `tap-postgres` and writes them to `target-netsuite`, sends the notification tables through `target-apprise`, marks the send-once notifications as sent, then reads `salesforce_opportunity_target_update` with `tap-postgres-writeback` and writes it to `target-salesforce`.

`run.py` runs `meltano install`, then `meltano run --force extract transform load`. It does not run `artifacts`; run that one by hand when you want the CSV review files.

The writeback runs last in the `load` job, but the ids it carries come from the staging tables that `transform` built, and those hold what the previous `extract` read. So a sales order created in this run reaches Salesforce on the next run, not this one. At a five minute schedule that is a five minute lag, and the plan is the same either way: nothing is ever written twice, because the matcher sees the id already in place.

## Running it locally

1. Start Postgres. The customer repositories use a container on port 5432 with user and password `postgres`.
2. Create the three custom fields on the Salesforce Opportunity object, as Text(18), and put their API names in `config.yml`. A stock org has none of them.
3. Copy the names in `env.template` into a `.env` file at this folder or at your project root, and fill them in.
4. Run `pdt run salesforce-netsuite-quote-to-cash`, or work inside this folder with Meltano directly: `meltano --environment dev install`, then `meltano --environment dev run --force extract transform load`.
5. To see what the sync would change without writing anything back, run `meltano --environment dev run --force extract transform artifacts` and read the CSV files under `artifacts/`.

Set `meltano_environment` in `config.yml` to choose which environment `run.py` uses. `dev` points at a Salesforce sandbox and a NetSuite sandbox account, `prod` points at both production systems, and `ci` builds a per-merge-request database.

`run.py` copies the three field names out of `config.yml` into the child environment as `NETSUITE_CUSTOMER_ID_FIELD`, `NETSUITE_SALES_ORDER_ID_FIELD` and `NETSUITE_INVOICE_ID_FIELD`. `transform/models/salesforce/stg_salesforce_opportunity.sql` reads them with `env_var` to pick the right source columns, and falls back to the three default names when you run Meltano by hand.

`meltano install` runs at the start of every job because pdt's Dockerfile has no build hook for it. On AWS Fargate, Azure Container Apps, or Cloud Run, every cold start pays that install cost before any data moves.

## Names this app uses that do not exist yet

**`tap-netsuite` has no `suiteql` stream.** Its streams today are `timesheet`, `timebill`, `employee`, `location`, `file_cabinet`, and `transaction`. The five `stg_netsuite_*` models here assume one generic SuiteQL stream that lands `tap_netsuite.customer`, `tap_netsuite.contact`, `tap_netsuite.inventoryitem`, `tap_netsuite.salesorder` and `tap_netsuite.invoice`, each selected by table name in `meltano.yml` and each with lower case column names, because SuiteQL returns lower case. Every one of those five models carries a comment saying so. This app assumes the stream needs no extra plugin setting beyond the selection list; if it turns out to need a table list, add that setting to `meltano.yml`.

**`stg_netsuite_salesorder.item` is the least certain column in this app.** SuiteQL returns transaction lines as rows in `transactionline`, not as an array on the order. For the sales order matcher to work, the suiteql stream has to assemble those rows into an `item` JSON array in the same shape the transform writes: one object per line with `item`, `quantity`, `rate` and `description`. Until it does, every Closed Won opportunity will look changed on every run and the order will be resent. Whoever writes the stream should settle this first.

**`target-netsuite` has no generic record sink.** `get_sink_class` in `target_netsuite/target.py` returns a sink only for a stream name containing `timesheet` or `timebill`, and raises for anything else. The eight streams this app sends are `netsuite_customer_target_create` and `_update`, and the same pair for `contact`, `salesorder` and `invoice`. A sink for those has to map the stream name to a NetSuite record type, has to map the lower case column names this project produces back to the camel case field names the NetSuite REST API expects, for example `companyname` to `companyName` and `createdfrom` to `createdFrom`, and has to pass the `item` column through as the record's `item` sublist rather than as a scalar field. The dispatch contract itself is already met: every record carries `_autoidm__action` set to `CREATE` or `UPDATE`.

**The three Salesforce custom fields do not exist in a stock org.** `NetSuite_Customer_Id__c`, `NetSuite_Sales_Order_Id__c` and `NetSuite_Invoice_Id__c` are the default names in `config.yml`, not fields Salesforce ships. Create them, or create your own and change the names.

**`PricebookEntry` is loaded and unused.** `tap-salesforce` selects it because a line item names a `PricebookEntryId` and an opportunity names a `Pricebook2Id`, so an org that prices from the pricebook rather than from the line's `UnitPrice` has the data waiting. No model reads it today.

**There are no `plugins/*.lock` files.** `meltano lock` needs network access to the Meltano Hub, so the lock files are absent here. The first `meltano install` resolves the Hub plugins and writes the locks. Run `meltano lock --all` once and commit the result if you want the plugin versions pinned.

## The Salesforce writeback

`target-salesforce` is the `dan-ladd` variant from the Meltano Hub, the only variant there. It is not an AutoIDM plugin, and this app is the first place we use it, so treat the first production writeback as a test.

Three things about it decide how `meltano.yml` is written, and all three come from reading its source:

- It picks the Salesforce object from the stream name: `sf_object = getattr(self.sf_client.bulk, self.stream_name)` in `target_salesforce/sinks.py`. Our stream is called `autoidm-salesforce_opportunity_target_update`, so the stream map renames it to `Opportunity` with `__alias__`.
- Its `action` setting is plugin level, not per record. `sinks.py` reads `self.config.get("action")` and never looks at a field on the record. It shares a name with the `action` column that `autoidm_update.sql` puts on every target row, but the two never meet, because the stream map drops that column before the record reaches the target. `meltano.yml` sets `action: update` explicitly, so the writeback can never insert an Opportunity.
- It validates each field against the object's real field list with a case sensitive lookup, `object_fields.get(field_name)`. Every column in this project is lower case, so the stream map renames the four columns it keeps to their exact Salesforce API names and drops everything else with `__else__: __NULL__`.

That last point is the one wart in this app. The stream map keys in `meltano.yml` hold the API names as literal text, because a Singer stream map key cannot read a config value. So if you change a field name in `config.yml`, change the matching key in the `target-salesforce` `stream_maps` block too. `config.yml` says so at the keys themselves.

## Two details worth knowing before you edit the dbt models

Every desired row carries the action twice, as `action` and as `_autoidm__action`. `transform/macros/autoidm_update.sql` is vendored from the copier template unchanged, and it filters on a column named `action`. `target-netsuite` dispatches on `_autoidm__action`. The match models pass `_autoidm__action` to `autoidm_matcher` as an ignored column, and the macro ignores `action` on its own.

Every column in this project is lower case. The vendored macros write unquoted identifiers, which Postgres folds to lower case, so a camel case column would not survive the matcher. This is why the NetSuite sink has to restore the field name casing, and why the Salesforce stream map has to do the same job for the writeback.
