-- tap-netsuite has no contact stream yet, so tap_netsuite.contact does not exist until one is written.
with source as (

    select * from {{ source('tap_netsuite', 'contact')}}

), stage as (

SELECT
source.id,
source."externalId" as externalid,
source."firstName" as firstname,
source."lastName" as lastname,
source.salutation,
source.title,
source.email,
source.phone,
source."mobilePhone" as mobilephone,
source.fax,
source.company->>'id' as company
FROM source


), final as (

    select * from stage

)

select * from final
