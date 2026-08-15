-- MODELED rate card. ILLUSTRATIVE MODELED RATES, not observed industry rates.
--
-- The deliberate 2026-06-10..12 gap is preserved: no row is interpolated to close it, because a
-- pipeline that invents a rate for an uncovered day is inventing money.
select
    rate_card_id,
    model_scope,
    valid_from,
    valid_to,
    rate_per_stream,
    currency,
    rule_version_id,
    is_modeled,
    rights_version
from {{ source('rights', 'rate_card') }}
