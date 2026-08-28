{%- macro autoidm_update(desired_state_relation, source_state_relation, match_relation, joinkey, action, group_columns=[]) -%}
{{
  config(
    materialized = "table",
  )
}}

{%- set desired_columns = adapter.get_columns_in_relation(desired_state_relation) -%}


--Could use dbt utils select star except here instead
{%- set include_cols = [] %}
{%- set cols = adapter.get_columns_in_relation(desired_state_relation) -%}
{%- set except = group_columns | map("lower") | list %}
{%- for col in cols -%}

    {%- if col.column|lower not in except -%}
        {% do include_cols.append(col.column) %}

    {%- endif %}
{%- endfor %}

select 

{% for column in include_cols %} 
desired.{{column}} {% if not loop.last %},{% endif %}

{% endfor %}

{%- for group_column in group_columns -%} 
, 
array(
select unnest({{desired_state_relation.identifier}}_{{group_column}}) 
except
select unnest({{source_state_relation.identifier}}_{{group_column}})

)_autoidm_{{group_column}}_add,

array(
select unnest({{source_state_relation.identifier}}_{{group_column}})
except
select unnest({{desired_state_relation.identifier}}_{{group_column}}) 
)_autoidm_{{group_column}}_remove {% if not loop.last %},{% endif %}
{% endfor %}

from {{desired_state_relation}} desired
join {{match_relation}} match on match.{{desired_state_relation.identifier}}_{{joinkey}}=desired.{{joinkey}}
where 
  action = '{{action}}'
  and match.delta != '{}'
{% endmacro %}