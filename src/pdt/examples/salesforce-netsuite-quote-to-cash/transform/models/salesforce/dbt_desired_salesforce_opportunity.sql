{{ config(materialized='table') }}
select * from {{ source("autoidm", "python_desired_salesforce_opportunity") }}
