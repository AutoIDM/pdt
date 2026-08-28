with source as (

    select * from {{ source('tap_salesforce', 'Product2')}}

), stage as (

SELECT
source."Id" as id,
source."Name" as name,
source."ProductCode" as productcode,
source."IsActive" as isactive
FROM source
WHERE source."IsDeleted" = false


), final as (

    select * from stage

)

select * from final
