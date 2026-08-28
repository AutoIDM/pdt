{{ autoidm_update(ref("dbt_desired_netsuite_invoice"), ref("stg_netsuite_invoice"), ref("netsuite_invoice_match"), joinkey="externalid", action="UPDATE", group_columns=[]) }}
