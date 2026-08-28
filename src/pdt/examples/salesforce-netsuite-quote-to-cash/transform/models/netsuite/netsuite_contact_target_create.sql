{{ autoidm_update(ref("dbt_desired_netsuite_contact"), ref("stg_netsuite_contact"), ref("netsuite_contact_match"), joinkey="externalid", action="CREATE", group_columns=[]) }}
