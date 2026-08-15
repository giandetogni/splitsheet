-- MODELED rights holders, typed and renamed. No filtering: a staging model that drops rows is a
-- silent correction, and the reserved holders with no ownership are a defect the quality layer
-- has to be able to see.
select
    holder_id,
    display_name,
    holder_type,
    payee_status,
    model_scope,
    is_modeled,
    rights_version,
    generation_run_id,
    generated_at
from {{ source('rights', 'rights_holders') }}
