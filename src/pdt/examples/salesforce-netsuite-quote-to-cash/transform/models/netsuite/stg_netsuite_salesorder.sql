-- tap-netsuite has no suiteql stream yet, so tap_netsuite.salesorder does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'salesorder')}}

), stage as (

SELECT
source.id,
source.externalid,
source.entity,
source.trandate::date as trandate,
source.memo,
source.item
FROM source


), final as (

    select * from stage

)

select * from final
