{% set customer_id_field = env_var('NETSUITE_CUSTOMER_ID_FIELD', 'NetSuite_Customer_Id__c') %}
{% set sales_order_id_field = env_var('NETSUITE_SALES_ORDER_ID_FIELD', 'NetSuite_Sales_Order_Id__c') %}
{% set invoice_id_field = env_var('NETSUITE_INVOICE_ID_FIELD', 'NetSuite_Invoice_Id__c') %}
with source as (

    select * from {{ source('tap_salesforce', 'Opportunity')}}

), stage as (

SELECT
source."Id" as id,
source."Name" as name,
source."StageName" as stagename,
source."CloseDate"::date as closedate,
source."AccountId" as accountid,
source."Amount" as amount,
source."Pricebook2Id" as pricebook2id,
source."{{ customer_id_field }}" as netsuite_customer_id,
source."{{ sales_order_id_field }}" as netsuite_sales_order_id,
source."{{ invoice_id_field }}" as netsuite_invoice_id
FROM source
WHERE source."IsDeleted" = false


), final as (

    select * from stage

)

select * from final
