-- The physical financial grain must be unique. A duplicate here means an amount is counted twice.
select period, recording_mbid, rights_holder_id, split_version_id, rate_card_id,
       rule_version_id, payout_policy_version, attribution_run_id, count(*) as n
from {{ ref('fct_royalty_attribution') }}
group by period, recording_mbid, rights_holder_id, split_version_id, rate_card_id,
         rule_version_id, payout_policy_version, attribution_run_id
having count(*) > 1
