-- gross_royalty must be exactly attributable_streams * rate_per_stream, rounded once.
--
-- This is why rate_card_id is part of the grain: with two rate windows inside the period, a single
-- row per recording could not satisfy this identity, and the discrepancy would look like a rounding
-- artefact rather than a modelling error.
select *
from {{ ref('fct_royalty_attribution') }}
where attribution_run_id = '{{ var("attribution_run_id") }}'
  and (gross_royalty != round(attributable_streams * rate_per_stream, 2)
       or gross_royalty_unrounded != attributable_streams * rate_per_stream
       or rate_per_stream <= numeric '0'
       or attributable_streams <= 0)
