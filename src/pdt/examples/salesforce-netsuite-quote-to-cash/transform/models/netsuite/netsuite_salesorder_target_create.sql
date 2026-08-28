{{ autoidm_update(ref("dbt_desired_netsuite_salesorder"), ref("stg_netsuite_salesorder"), ref("netsuite_salesorder_match"), joinkey="externalid", action="CREATE", group_columns=[]) }}
