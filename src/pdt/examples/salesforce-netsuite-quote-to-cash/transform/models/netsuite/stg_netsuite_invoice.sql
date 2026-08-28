-- tap-netsuite has no suiteql stream yet, so tap_netsuite.invoice does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'invoice')}}

), stage as (

SELECT
source.id,
source.externalid,
source.entity,
source.trandate::date as trandate,
source.memo,
source.createdfrom
FROM source


), final as (

    select * from stage

)

select * from final
