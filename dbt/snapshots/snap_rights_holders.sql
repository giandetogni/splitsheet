{% snapshot snap_rights_holders %}
{{
    config(
      target_schema = 'splitsheet_dbt',
      unique_key    = 'holder_id',
      strategy      = 'check',
      check_cols    = ['display_name', 'holder_type', 'payee_status', 'model_scope'],
      invalidate_hard_deletes = true
    )
}}

-- SCD TYPE 2, FOR REAL. dbt snapshot compares the current source state against what it stored
-- last time and, when a checked column differs, closes the old row and inserts a new one.
--
-- `strategy = check` rather than `timestamp`: the generated source has no reliable
-- last-modified column, and inventing one would make the snapshot detect wall-clock noise
-- instead of content changes.
--
-- The controlled proof: run the snapshot, re-land rights_holders with N holders' payee_status
-- flipped (src/rights/build_rights.py --revise-payee-status N), run the snapshot again. The old
-- version stays queryable with a closed dbt_valid_to, the new version opens, exactly one row per
-- holder is current, and nothing is overwritten.
--
-- Ownership splits are NOT snapshotted. Their intervals come from the generator, so calling that
-- SCD Type 2 would be a naming error, not a modelling choice.
select
    holder_id,
    display_name,
    holder_type,
    payee_status,
    model_scope,
    is_modeled,
    rights_version,
    generation_run_id
from {{ source('rights', 'rights_holders') }}

{% endsnapshot %}
