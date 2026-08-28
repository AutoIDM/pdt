-- tap-netsuite has no customer stream yet, so tap_netsuite.customer does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'customer')}}

), stage as (

SELECT
source.id,
source."externalId" as externalid,
source."companyName" as companyname,
source.phone,
source.fax,
source.url,
source.comments
FROM source


), final as (

    select * from stage

)

select * from final
