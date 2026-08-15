-- MATCHED != PAYABLE, asserted as data.
--
-- A SUBTLETY THIS TEST GOT WRONG FIRST TIME, worth keeping: `match_method` describes how a listen
-- was EVALUATED, not what the matcher concluded. SCORED_FALLBACK_UNIQUE covers 319,001 listens, of
-- which the matcher ACCEPTED 35,007 and REFUSED 283,994 as BELOW_THRESHOLD. Only the accepted ones
-- are what MATCH_RISK_POLICY holds; the refused ones are not matched at all and are held for the
-- ordinary reason that there is nothing to attribute. The first version of this assertion demanded
-- MATCH_RISK_POLICY for every SCORED_FALLBACK_UNIQUE row and failed on 283,994 listens -- the test
-- was wrong, not the model.
--
-- The four properties:
--   1. an ACCEPTED fallback-unique match is technically matched but never payout-eligible, and is
--      held under MATCH_RISK_POLICY;
--   2. no unmatched listen is ever payout-eligible;
--   3. every non-eligible listen carries exactly one hold reason;
--   4. an eligible listen carries none.
select listen_hash, match_status, match_method, payout_eligible, hold_reason
from {{ ref('int_payout_eligibility') }}
where (match_status = 'MATCHED' and match_method = 'SCORED_FALLBACK_UNIQUE'
       and (payout_eligible or hold_reason != 'MATCH_RISK_POLICY'))
   or (match_status != 'MATCHED' and payout_eligible)
   or (not payout_eligible and hold_reason is null)
   or (payout_eligible and hold_reason is not null)
