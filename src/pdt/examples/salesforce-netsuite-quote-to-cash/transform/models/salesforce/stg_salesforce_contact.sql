with source as (

    select * from {{ source('tap_salesforce', 'Contact')}}

), stage as (

SELECT
source."Id" as id,
source."AccountId" as accountid,
source."FirstName" as firstname,
source."LastName" as lastname,
source."Salutation" as salutation,
source."Title" as title,
source."Email" as email,
source."Phone" as phone,
source."MobilePhone" as mobilephone,
source."Fax" as fax
FROM source
WHERE source."IsDeleted" = false


), final as (

    select * from stage

)

select * from final
