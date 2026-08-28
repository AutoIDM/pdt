{{ config(materialized='table') }}
with source as (

    select * from {{ source('autoidm_state', 'send_once_notifications')}}

), stage as (

SELECT 
source.title,
source.body
FROM source
WHERE source.sent is FALSE

), final as (

    select * from stage

)

select * from final