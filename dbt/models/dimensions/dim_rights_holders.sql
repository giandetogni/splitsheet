{{ config(materialized = 'table', cluster_by = ['holder_id']) }}

-- MODELED rights holders as a dimension, built from the SNAPSHOT rather than from the source, so
-- that history is available and the current row is explicit.
--
-- THIS is the SCD Type 2 dimension in the project. `dbt_valid_from` / `dbt_valid_to` are produced
-- by dbt's snapshot mechanism detecting a real change between two runs -- not by a generator
-- writing intervals. The ownership splits are the other thing: temporal ownership modeling with
-- validity intervals, which is deliberately NOT called SCD Type 2.
select
    dbt_scd_id            as holder_version_id,
    holder_id,
    display_name,
    holder_type,
    payee_status,
    model_scope,
    is_modeled,
    rights_version,
    generation_run_id,
    dbt_valid_from        as version_valid_from,
    dbt_valid_to          as version_valid_to,     -- NULL for the current version
    (dbt_valid_to is null) as is_current
from {{ ref('snap_rights_holders') }}
