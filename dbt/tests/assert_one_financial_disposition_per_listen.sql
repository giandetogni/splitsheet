-- Exactly one financial decision per listen, and every listen has one.
--
-- Three failure modes, all of which would be invisible in an aggregate: a listen appearing twice
-- (a fan-out from the ownership or rate join), a listen with no terminal state (a policy gap
-- reaching the data as NULL), and a listen whose disposition contradicts its own gate columns.
select 'duplicate_listen' as violation, listen_hash, count(*) as n
from {{ ref('int_financial_disposition') }}
group by listen_hash having count(*) > 1

union all

select 'null_or_unknown_attribution_status', listen_hash, 1
from {{ ref('int_financial_disposition') }}
where attribution_status is null
   or attribution_status = 'UNKNOWN'
   or attribution_status not in ('ATTRIBUTABLE', 'UNMATCHED', 'MATCH_RISK_POLICY',
                                 'DEFECTIVE_OWNERSHIP', 'RATE_CARD_GAP', 'RATE_CARD_AMBIGUOUS')

union all

-- payout_eligible and attribution_status must agree in both directions, and hold_reason must be
-- present exactly when nothing is payable.
select 'disposition_contradicts_eligibility', listen_hash, 1
from {{ ref('int_financial_disposition') }}
where (payout_eligible and attribution_status != 'ATTRIBUTABLE')
   or (not payout_eligible and attribution_status = 'ATTRIBUTABLE')
   or (payout_eligible and hold_reason is not null)
   or (not payout_eligible and hold_reason is null)
