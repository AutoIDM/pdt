{%- macro autoidm_drop_salesforce() -%}
    {%- set drop_query -%}
        DROP SCHEMA IF EXISTS TAP_SALESFORCE CASCADE
    {%- endset -%}
    {% do run_query(drop_query) %}
{%- endmacro -%}
