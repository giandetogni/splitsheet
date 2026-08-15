{{ config(
    materialized = 'table',
    partition_by = {'field': 'listen_date', 'data_type': 'date'},
    cluster_by = ['hold_reason', 'match_method']
) }}

-- PAYOUT ELIGIBILITY. This is the layer that exists because a technical match is not an
-- authorisation to pay.
--
--   MATCHED != PAYABLE
--
-- Nothing here changes the matcher. silver_listen_matches is frozen (scoring_version
-- 1.0.0+cb21f9704ff0) and is read exactly as published; this model applies a BUSINESS policy on
-- top of it and can be changed by editing the policy alone, without re-running matching, without
-- choosing a new threshold, and without touching the consumed validation partition.
--
-- THE POLICY, and its measured basis:
--
--   SCORED_FALLBACK_UNIQUE  -> payout_eligible = false, hold_reason = MATCH_RISK_POLICY
--
--     Measured on the validation partition: 3.2138% disagreement with the mapper reference
--     label, against 0.0055% for STRUCTURAL_EXACT_UNIQUE and 0.1101% for SCORED_EXACT_MULTIPLE.
--     Materially worse, and no approved business risk policy exists that would justify paying
--     on it. 35,007 listens are held on that basis.
--
--     This policy is NOT presented as validated by the validation partition. The partition
--     measured the matcher; the decision to hold rather than pay is a risk judgement made
--     outside it, and it is reversible.
--
--   UNRESOLVED -> payout_eligible = false, hold_reason = NOT_MATCHED_<failure_reason>
--
--     An unmatched listen has no recording to attribute to. Its failure reason is carried into
--     the hold reason so that "why is this stream not paying" has one answer, not two lookups.
--
-- The mapper reference label is NOT used anywhere in this model. It is a correlated reference
-- label, not ground truth, and it lives in a dataset this pipeline does not join to.

with matches as (
    select * from {{ ref('stg_silver__listen_matches') }}
)

select
    listen_hash,
    listened_at,
    listen_date,
    matched_recording_mbid,
    match_status,
    match_method,
    match_tier,
    match_score,
    failure_reason,
    blocking_key_information_class,
    scoring_version,
    match_run_id,

    -- Eligibility is the AND of "we know what it is" and "we trust how we know it".
    (match_status = 'MATCHED' and match_method != 'SCORED_FALLBACK_UNIQUE')
        as payout_eligible,

    case
        when match_status != 'MATCHED'
            then concat('NOT_MATCHED_', ifnull(failure_reason, 'UNKNOWN_REASON'))
        when match_method = 'SCORED_FALLBACK_UNIQUE'
            then 'MATCH_RISK_POLICY'
        else null
    end as hold_reason,

    -- Named so a reader can tell which policy version produced the hold, the same way every
    -- other decision in this project carries its version.
    'payout-policy-1.0.0' as payout_policy_version

from matches
