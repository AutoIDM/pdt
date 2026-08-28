-- tap-netsuite has no suiteql stream yet, so tap_netsuite.contact does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'contact')}}

), stage as (

SELECT
source.id,
source.externalid,
source.firstname,
source.lastname,
source.salutation,
source.title,
source.email,
source.phone,
source.mobilephone,
source.fax,
source.company
FROM source


), final as (

    select * from stage

)

select * from final
