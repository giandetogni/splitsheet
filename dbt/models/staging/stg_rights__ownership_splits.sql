-- MODELED ownership splits. Deliberate defects PASS THROUGH untouched: no row is filtered, no
-- interval repaired, no share rescaled. Classification happens in int_ownership_validity, and
-- correction happens nowhere in Phase 5A.
--
-- share_pct stays NUMERIC. Casting it to FLOAT64 to "make the sum work" is exactly the bug this
-- project is meant to demonstrate awareness of.
select
    recording_mbid,
    rights_holder_id,
    share_pct,
    valid_from,
    valid_to,
    split_version_id,
    is_modeled,
    rights_version,
    generation_run_id
from {{ source('rights', 'ownership_splits') }}
