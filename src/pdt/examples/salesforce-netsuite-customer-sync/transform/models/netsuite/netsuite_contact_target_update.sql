{{ autoidm_update(ref("dbt_desired_netsuite_contact"), ref("stg_netsuite_contact"), ref("netsuite_contact_match"), joinkey="externalid", action="UPDATE", group_columns=[]) }}
