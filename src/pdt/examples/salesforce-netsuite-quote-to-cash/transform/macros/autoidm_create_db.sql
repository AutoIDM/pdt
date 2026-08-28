{%- macro autoidm_create_db() -%}
    {{ log("Starting autoidm_create_db macro", info=True) }}
    
    {%- set mr_iid = env_var('CI_MERGE_REQUEST_IID', '') -%}
    {%- set ci_db_name = env_var('CI_DB_NAME', '') -%}
    
    {{ log("CI_MERGE_REQUEST_IID value: '" ~ mr_iid ~ "'", info=True) }}
    {{ log("CI_DB_NAME value: '" ~ ci_db_name ~ "'", info=True) }}
    
    {%- if execute -%}
        {%- if mr_iid is none or mr_iid | trim == '' -%}
            {{ exceptions.raise_compiler_error("Environment variable 'CI_MERGE_REQUEST_IID' must be set and non-empty. Current value: '" ~ mr_iid ~ "'") }}
        {%- endif -%}
        
        {%- if ci_db_name is none or ci_db_name | trim == '' -%}
            {{ exceptions.raise_compiler_error("Environment variable 'CI_DB_NAME' must be set and non-empty. Current value: '" ~ ci_db_name ~ "'") }}
        {%- endif -%}
    {%- endif -%}

    {%- set query -%}
        CREATE DATABASE {{ ci_db_name }};
    {%- endset -%}
    {% do run_query(query) %}
{%- endmacro -%}