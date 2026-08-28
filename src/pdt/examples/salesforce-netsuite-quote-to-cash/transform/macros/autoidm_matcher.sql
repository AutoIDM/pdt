{%- macro autoidm_matcher(desired_state_relation, current_state_relation, join_column, additional_ignored_columns, ignore_case_columns) -%}
{{
  config(
    materialized = "table",
  )
}}

{%- set desired_columns = adapter.get_columns_in_relation(desired_state_relation) -%}
{%- set current_columns = adapter.get_columns_in_relation(current_state_relation) -%}
{%- set ignored_columns = ["action"] -%}



{%- for column in additional_ignored_columns -%} --TODO: Should fail if ignored column doesn't exist
{{ ignored_columns.append(column) or '' }}
{% endfor %}

{{ log("Ignored Columns: " ~ ignored_columns, True) }}

{%- if join_column is none  -%}
  {{ exceptions.raise_compiler_error("Need to provide a join_column, none was provided") }}     	
{% endif %}

--Psuedo code
--Want a column for each column in desired state relation
--Check that every column in desired_state_relation exists in current_state relation
--  Throw Exception Otherwise
--Add desired.column, target.column, target_delta
--Use the || operator to create a full delta map of all changes


{%- for col in adapter.get_missing_columns(desired_state_relation, current_state_relation) if not (col.name in ignored_columns)  -%}
  {{ exceptions.raise_compiler_error("Current_state_relation of:" ~ current_state_relation ~ " doesn't have the column: "~ col ~ " that the desired_state_relation:" ~ desired_state_relation ~ " has. They need to exist to call this macro") }}
{%- endfor -%}

--first
with first as (

select 
{% for col in desired_columns %}
  {% if col.name not in ignored_columns %}
  {% set case_column = False %}
    {% if col.name in ignore_case_columns -%}
      {% set case_column = True %}
    {% endif %}
  desired.{{col.name}} as {{desired_state_relation.identifier}}_{{col.name}},
  current.{{col.name}} as {{current_state_relation.identifier}}_{{col.name}},
  case 
    {% if not case_column  %}
    when desired.{{col.name}} = current.{{col.name}} OR desired.{{col.name}} IS NULL then '{}'
    {% else %}
    when UPPER(desired.{{col.name}}) = UPPER(current.{{col.name}}) OR desired.{{col.name}} IS NULL then '{}'
    {% endif %}
    
    else jsonb_build_object(('{{col.name}}'), json_build_object('before', current.{{col.name}}, 'after', desired.{{col.name}}))
    end as {{col.name}}_delta

  {% else %}
  desired.{{col.name}} as {{desired_state_relation.identifier}}_{{col.name}}
  {% endif %} --ignored columns if
  {% if not loop.last %},{% endif %}
{% endfor %}

from {{desired_state_relation}} desired --TODO: Do we want desired/current hard coded? Maybe a shortname?
left join {{current_state_relation}} current on desired.{{join_column}} = current.{{join_column}}

), delta as (

select
*,

--Create delta map
{% for col in desired_columns if not (col.name in ignored_columns) %}
{{col.name}}_delta
{% if not loop.last %}||{% endif %}
{% endfor %}
as delta

from first

), final as (

    select *, jsonb_pretty(delta) pretty_delta from delta
)

select * from final

{% endmacro %}
