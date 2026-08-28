{%- macro autoidm_drop_netsuite() -%}
    {%- set drop_query -%}
        DROP SCHEMA IF EXISTS TAP_NETSUITE CASCADE
    {%- endset -%}
    {% do run_query(drop_query) %}
{%- endmacro -%}
