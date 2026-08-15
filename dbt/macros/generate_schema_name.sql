{#
  Every model lands in the target dataset (splitsheet_dbt) unless it explicitly names another
  schema. dbt's default appends the custom schema to the target, producing
  `splitsheet_dbt_quality` and friends, which would scatter one phase's output across several
  datasets for no benefit. Terraform owns the dataset; dbt owns what is inside it.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
