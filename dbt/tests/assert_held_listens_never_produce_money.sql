-- Nothing held at any gate may reach an amount, and a held listen may not carry an imputed rate.
--
-- MATCH_RISK_POLICY: 35,007 technically matched listens. They stay MATCHED -- that is the matcher's
-- conclusion and this policy does not overrule it -- but they must never be priced.
-- RATE_CARD_GAP: no rate may appear. Not the previous rate, not an average, not zero.
-- DEFECTIVE_OWNERSHIP: a rate may legitimately exist for the day (a rate is a property of the date,
-- not of the ownership) but no payout may exist.
select 'match_risk_still_matched' as violation, count(*) as n
from {{ ref('int_financial_disposition') }}
where attribution_status = 'MATCH_RISK_POLICY' and match_status != 'MATCHED'
having count(*) > 0

union all
select 'match_risk_priced', count(*)
from {{ ref('int_financial_disposition') }}
where attribution_status = 'MATCH_RISK_POLICY'
  and (rate_per_stream is not null or payout_eligible)
having count(*) > 0

union all
select 'rate_gap_has_an_imputed_rate', count(*)
from {{ ref('int_financial_disposition') }}
where attribution_status = 'RATE_CARD_GAP' and rate_per_stream is not null
having count(*) > 0

union all
select 'defective_ownership_payable', count(*)
from {{ ref('int_financial_disposition') }}
where attribution_status = 'DEFECTIVE_OWNERSHIP' and payout_eligible
having count(*) > 0

-- The fact table may only contain recordings that had an ATTRIBUTABLE listen. A held recording
-- reaching the fact is the failure this whole layer exists to prevent.
union all
select 'held_recording_reached_the_fact', count(*)
from (
    select distinct f.recording_mbid, f.split_version_id
    from {{ ref('fct_royalty_attribution') }} f
    where not exists (
        select 1 from {{ ref('int_financial_disposition') }} d
        where d.recording_mbid   = f.recording_mbid
          and d.split_version_id = f.split_version_id
          and d.attribution_status = 'ATTRIBUTABLE')
)
having count(*) > 0
