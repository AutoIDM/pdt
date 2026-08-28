{{ autoidm_matcher(ref("dbt_desired_netsuite_invoice"), ref("stg_netsuite_invoice"), "externalid", ["_autoidm__action", "createdfrom"], []) }}
