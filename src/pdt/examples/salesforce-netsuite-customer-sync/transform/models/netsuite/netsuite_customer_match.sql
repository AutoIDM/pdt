{{ autoidm_matcher(ref("dbt_desired_netsuite_customer"), ref("stg_netsuite_customer"), "externalid", ["_autoidm__action"], []) }}
