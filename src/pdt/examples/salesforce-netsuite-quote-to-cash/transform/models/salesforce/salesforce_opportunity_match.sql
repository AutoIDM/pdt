{{ autoidm_matcher(ref("dbt_desired_salesforce_opportunity"), ref("stg_salesforce_opportunity"), "id", ["_autoidm__action"], []) }}
