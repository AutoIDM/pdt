with source as (

    select * from {{ source('tap_salesforce', 'OpportunityLineItem')}}

), stage as (

SELECT
source."Id" as id,
source."OpportunityId" as opportunityid,
source."Product2Id" as product2id,
source."PricebookEntryId" as pricebookentryid,
source."Quantity" as quantity,
source."UnitPrice" as unitprice,
source."TotalPrice" as totalprice,
source."Description" as description
FROM source
WHERE source."IsDeleted" = false


), final as (

    select * from stage

)

select * from final
