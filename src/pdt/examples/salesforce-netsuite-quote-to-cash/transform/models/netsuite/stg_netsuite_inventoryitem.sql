-- tap-netsuite has no suiteql stream yet, so tap_netsuite.inventoryitem does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'inventoryitem')}}

), stage as (

SELECT
source.id,
source.itemid,
source.displayname
FROM source


), final as (

    select * from stage

)

select * from final
