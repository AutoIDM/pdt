{{ autoidm_update(ref("dbt_desired_netsuite_customer"), ref("stg_netsuite_customer"), ref("netsuite_customer_match"), joinkey="externalid", action="UPDATE", group_columns=[]) }}
