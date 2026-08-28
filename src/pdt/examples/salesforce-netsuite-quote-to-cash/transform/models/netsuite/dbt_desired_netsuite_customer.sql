{{ config(materialized='table') }}
select * from {{ source("autoidm", "python_desired_netsuite_customer") }}
