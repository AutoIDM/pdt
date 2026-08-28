with source as (

    select * from {{ source('tap_salesforce', 'Account')}}

), stage as (

SELECT
source."Id" as id,
source."Name" as name,
source."Phone" as phone,
source."Fax" as fax,
source."Website" as website,
source."Description" as description
FROM source
WHERE source."IsDeleted" = false


), final as (

    select * from stage

)

select * from final
