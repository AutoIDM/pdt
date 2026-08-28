-- tap-netsuite has no suiteql stream yet, so tap_netsuite.customer does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'customer')}}

), stage as (

SELECT
source.id,
source.externalid,
source.companyname,
source.phone,
source.fax,
source.url,
source.comments
FROM source


), final as (

    select * from stage

)

select * from final
