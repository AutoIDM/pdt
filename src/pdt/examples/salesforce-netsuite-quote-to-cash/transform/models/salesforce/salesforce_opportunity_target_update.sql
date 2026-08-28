{{ autoidm_update(ref("dbt_desired_salesforce_opportunity"), ref("stg_salesforce_opportunity"), ref("salesforce_opportunity_match"), joinkey="id", action="UPDATE", group_columns=[]) }}
