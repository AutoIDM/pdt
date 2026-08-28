import hashlib
import json
import os
from functools import cached_property

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import TEXT, insert


class Transformation:

    CLIENT_EMAIL = "it@example.com"

    def __init__(self) -> None:
        self.environment = os.getenv("MELTANO_ENVIRONMENT")
        self.sqlalchemy_url = os.getenv("SQLALCHEMY_URL")
        assert self.environment
        assert self.sqlalchemy_url

    @cached_property
    def db_connection(self):
        return create_engine(self.sqlalchemy_url, pool_recycle=3600)

    @cached_property
    def salesforce_account_df(self) -> pd.DataFrame:
        return pd.read_sql("select * from autoidm.stg_salesforce_account", self.db_connection).add_prefix("salesforce_account_")

    @cached_property
    def salesforce_contact_df(self) -> pd.DataFrame:
        return pd.read_sql("select * from autoidm.stg_salesforce_contact", self.db_connection).add_prefix("salesforce_contact_")

    @cached_property
    def netsuite_customer_df(self) -> pd.DataFrame:
        return pd.read_sql("select * from autoidm.stg_netsuite_customer", self.db_connection).add_prefix("netsuite_customer_")

    @cached_property
    def netsuite_contact_df(self) -> pd.DataFrame:
        return pd.read_sql("select * from autoidm.stg_netsuite_contact", self.db_connection).add_prefix("netsuite_contact_")

    @cached_property
    def merged_account_df(self) -> pd.DataFrame:
        return pd.merge(
            self.salesforce_account_df.copy(),
            self.netsuite_customer_df.copy(),
            left_on="salesforce_account_id",
            right_on="netsuite_customer_externalid",
            how="left",
            suffixes=("_salesforce", "_netsuite"),
        )

    @cached_property
    def merged_contact_df(self) -> pd.DataFrame:
        return pd.merge(
            self.salesforce_contact_df.copy(),
            self.netsuite_contact_df.copy(),
            left_on="salesforce_contact_id",
            right_on="netsuite_contact_externalid",
            how="left",
            suffixes=("_salesforce", "_netsuite"),
        )

    @staticmethod
    def has_real_value(value) -> bool:
        if pd.isnull(value):
            return False
        if isinstance(value, str):
            return value.strip() != ""
        return True

    @classmethod
    def value_for_sync(cls, salesforce_value, netsuite_value):
        if not cls.has_real_value(salesforce_value) and cls.has_real_value(netsuite_value):
            return "_blank_"
        return salesforce_value

    def pii_safe_account(self, row: dict) -> str:
        return f"salesforce_account_id: {row['salesforce_account_id']}. netsuite_customer_id: {row['netsuite_customer_id']}"

    def pii_safe_contact(self, row: dict) -> str:
        return f"salesforce_contact_id: {row['salesforce_contact_id']}. netsuite_contact_id: {row['netsuite_contact_id']}"

    def lookup_customer(self, accountid) -> str:
        """Returns the NetSuite internal id of the customer holding this Salesforce account id"""
        netsuite_customer_df = self.netsuite_customer_df
        filtered_netsuite_customer_df = netsuite_customer_df[netsuite_customer_df["netsuite_customer_externalid"] == accountid]
        if len(filtered_netsuite_customer_df) == 1:
            return filtered_netsuite_customer_df["netsuite_customer_id"].values[0]
        if len(filtered_netsuite_customer_df) > 1:
            msg = f"Found more than one NetSuite customer for {accountid=}, externalid must be unique"
            raise RuntimeError(msg)
        return None

    def hash_notification(self, notification: dict) -> str:
        return hashlib.sha256(json.dumps(notification, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def ignore_duplicates(pd_table, conn, keys, data_iter):
        """Insert data into a table and ignore duplicates if they already exist."""
        data = [dict(zip(keys, row)) for row in data_iter]
        insert_statement = insert(pd_table.table).on_conflict_do_nothing()
        result = conn.execute(insert_statement, data)
        return result.rowcount

    def desired_customers(self, notifications: list) -> list:
        desired_rows = []
        for row in self.merged_account_df.to_dict(orient="records"):
            desired_customer = {}
            pii_safe_identifier = self.pii_safe_account(row=row)

            if not self.has_real_value(row["salesforce_account_name"]):
                print(f"Skipping account because NetSuite needs a company name. Account: {pii_safe_identifier}")
                continue

            if self.has_real_value(row["netsuite_customer_id"]):
                desired_customer["action"] = "UPDATE"
                desired_customer["id"] = row["netsuite_customer_id"]
            else:
                desired_customer["action"] = "CREATE"
                desired_customer["id"] = None
                notifications.append({
                    "title": f"New NetSuite Customer - {row['salesforce_account_name']}",
                    "body": (
                        f"A NetSuite customer will be created for a Salesforce account.<br>"
                        f"Company Name: {row['salesforce_account_name']}<br>"
                        f"Salesforce Account ID: {row['salesforce_account_id']}<br>"
                    ),
                    "_sdc_replace_target_email": self.CLIENT_EMAIL,
                })
            desired_customer["_autoidm__action"] = desired_customer["action"]
            desired_customer["externalid"] = row["salesforce_account_id"]
            desired_customer["companyname"] = row["salesforce_account_name"]
            desired_customer["phone"] = self.value_for_sync(
                salesforce_value=row["salesforce_account_phone"],
                netsuite_value=row.get("netsuite_customer_phone"),
            )
            desired_customer["fax"] = self.value_for_sync(
                salesforce_value=row["salesforce_account_fax"],
                netsuite_value=row.get("netsuite_customer_fax"),
            )
            desired_customer["url"] = self.value_for_sync(
                salesforce_value=row["salesforce_account_website"],
                netsuite_value=row.get("netsuite_customer_url"),
            )
            desired_customer["comments"] = self.value_for_sync(
                salesforce_value=row["salesforce_account_description"],
                netsuite_value=row.get("netsuite_customer_comments"),
            )
            desired_rows.append(desired_customer)
        return desired_rows

    def desired_contacts(self, send_once_notifications: list) -> list:
        desired_rows = []
        for row in self.merged_contact_df.to_dict(orient="records"):
            desired_contact = {}
            pii_safe_identifier = self.pii_safe_contact(row=row)

            if not self.has_real_value(row["salesforce_contact_lastname"]):
                print(f"Skipping contact because NetSuite needs a last name. Contact: {pii_safe_identifier}")
                continue

            company = self.lookup_customer(accountid=row["salesforce_contact_accountid"])
            if company is None:
                print(f"The NetSuite customer for this contact does not exist yet, leaving company unmanaged. Contact: {pii_safe_identifier}")
                notification = {
                    "title": "Contact Without a NetSuite Customer",
                    "body": (
                        f"A Salesforce contact points at an account that has no NetSuite customer yet.<br>"
                        f"Salesforce Contact ID: {row['salesforce_contact_id']}<br>"
                        f"Salesforce Account ID: {row['salesforce_contact_accountid']}<br>"
                        "The contact syncs without a company until the customer exists."
                    ),
                    "_sdc_replace_target_email": self.CLIENT_EMAIL,
                }
                notification.update({"hash": self.hash_notification(notification)})
                send_once_notifications.append(notification)

            if self.has_real_value(row["netsuite_contact_id"]):
                desired_contact["action"] = "UPDATE"
                desired_contact["id"] = row["netsuite_contact_id"]
            else:
                desired_contact["action"] = "CREATE"
                desired_contact["id"] = None
            desired_contact["_autoidm__action"] = desired_contact["action"]
            desired_contact["externalid"] = row["salesforce_contact_id"]
            desired_contact["firstname"] = row["salesforce_contact_firstname"]
            desired_contact["lastname"] = row["salesforce_contact_lastname"]
            desired_contact["salutation"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_salutation"],
                netsuite_value=row.get("netsuite_contact_salutation"),
            )
            desired_contact["title"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_title"],
                netsuite_value=row.get("netsuite_contact_title"),
            )
            desired_contact["email"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_email"],
                netsuite_value=row.get("netsuite_contact_email"),
            )
            desired_contact["phone"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_phone"],
                netsuite_value=row.get("netsuite_contact_phone"),
            )
            desired_contact["mobilephone"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_mobilephone"],
                netsuite_value=row.get("netsuite_contact_mobilephone"),
            )
            desired_contact["fax"] = self.value_for_sync(
                salesforce_value=row["salesforce_contact_fax"],
                netsuite_value=row.get("netsuite_contact_fax"),
            )
            desired_contact["company"] = company
            desired_rows.append(desired_contact)
        return desired_rows

    def transform(self):
        notifications = []
        send_once_notifications = []

        desired_customer_df = pd.DataFrame.from_dict(self.desired_customers(notifications=notifications))
        print("Writing autoidm.python_desired_netsuite_customer", flush=True)
        desired_customer_df.to_sql(
            "python_desired_netsuite_customer",
            self.db_connection,
            schema="autoidm",
            if_exists="replace",
            index=False,
            dtype={"id": TEXT},
        )

        desired_contact_df = pd.DataFrame.from_dict(self.desired_contacts(send_once_notifications=send_once_notifications))
        print("Writing autoidm.python_desired_netsuite_contact", flush=True)
        desired_contact_df.to_sql(
            "python_desired_netsuite_contact",
            self.db_connection,
            schema="autoidm",
            if_exists="replace",
            index=False,
            dtype={"id": TEXT, "company": TEXT},
        )

        notifications_df = pd.DataFrame.from_dict(notifications)
        print("Writing autoidm.notifications", flush=True)
        notifications_df.to_sql(
            "notifications",
            self.db_connection,
            schema="autoidm",
            if_exists="replace",
            index=False,
        )

        send_once_notifications_df = pd.DataFrame.from_dict(send_once_notifications)
        print("Writing autoidm_state.send_once_notifications", flush=True)
        send_once_notifications_df.to_sql(
            "send_once_notifications",
            self.db_connection,
            schema="autoidm_state",
            if_exists="append",
            index=False,
            method=self.ignore_duplicates,
        )

    @staticmethod
    def run():
        print("Starting autoidm-transform", flush=True)
        try:
            t = Transformation()
            t.transform()
            print("Completed autoidm-transform", flush=True)
            return 0
        except Exception as e:
            import sys
            import traceback
            print(
                f"autoidm-transform failed with {type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            raise
