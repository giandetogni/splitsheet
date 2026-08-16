{{ config(
    materialized = 'table',
    partition_by = {'field': 'listen_date', 'data_type': 'date'},
    cluster_by = ['attribution_status', 'recording_mbid']
) }}

-- EXACTLY ONE FINANCIAL DECISION PER LISTEN. 38,199,641 rows, no more and no fewer.
--
-- MATCHED != PAYABLE. This model is where a technical conclusion becomes a financial one, and the
-- two are kept visibly distinct:
--
--   match_status            what the FROZEN matcher concluded. Untouched here.
--   match_payout_eligible   the Phase 5A gate: matched AND not held for match risk.
--   payout_eligible         the FULL gate: everything above AND ownership AND rate.
--
-- Carrying both eligibility columns is deliberate. `payout_eligible` narrowed between Phase 5A and
-- 5B as ownership and rate gates were added, and silently reusing one name for two meanings is how
-- a reader concludes that 31.5M listens are payable when 3.1M of them have no rate.
--
-- THE GATE ORDER IS THE POLICY (config/payout_policy.yml, version passed in as a var):
--
--   1. not matched                        -> UNMATCHED
--   2. matched by a held method           -> MATCH_RISK_POLICY   (still MATCHED; not downgraded)
--   3. ownership not resolved on the date -> DEFECTIVE_OWNERSHIP
--   4. no rate on the date                -> RATE_CARD_GAP
--      more than one rate on the date     -> RATE_CARD_AMBIGUOUS
--   5. all gates passed                   -> ATTRIBUTABLE
--
-- Ownership is checked before rate because an unknown owner cannot be paid at any rate; reporting
-- such a listen as a pricing problem would misdirect the fix.
--
-- NO UNKNOWN BUCKET. `attribution_status` is NULL only if a resolution_status appears that the
-- policy does not map, and a test refuses the publication in that case rather than defaulting.
--
-- A HELD LISTEN CARRIES NO RATE AND NO SPLIT. `int_ownership_resolution` is keyed on
-- (recording_mbid, listen_date) and 5,790 recordings appear in BOTH the payout-eligible and the
-- risk-held groups, so a plain join attaches an eligible recording-day's rate to a held listen --
-- measured at 9,968 MATCH_RISK_POLICY listens before this was fixed. The resolved values are kept
-- under `resolved_*` names for diagnosis, and the payable columns are NULL unless the listen is
-- ATTRIBUTABLE, so no held row ever carries a number that could be multiplied.

with eligibility as (
    select * from {{ ref('int_payout_eligibility') }}
),

-- Ownership and rate resolution, per recording-day. Present only for listens that reached the
-- ownership gate at all: Phase 5A resolves eligible recording-days, so an unmatched or risk-held
-- listen has no row here and its ownership_status is NOT_APPLICABLE rather than a failure.
resolution as (
    select
        recording_mbid,
        listen_date,
        resolution_status,
        covering_valid_sets,
        covering_rate_rows,
        split_version_id,
        rule_version_id,
        rate_per_stream,
        currency
    from {{ ref('int_ownership_resolution') }}
),

joined as (
    select
        e.listen_hash,
        e.listened_at,
        e.listen_date,
        e.matched_recording_mbid as recording_mbid,
        e.match_status,
        e.match_method,
        e.match_tier,
        e.payout_eligible as match_payout_eligible,
        e.hold_reason as match_hold_reason,
        r.resolution_status,
        r.covering_valid_sets,
        r.covering_rate_rows,
        r.split_version_id,
        r.rule_version_id,
        r.rate_per_stream,
        r.currency
    from eligibility e
    left join resolution r
           on r.recording_mbid = e.matched_recording_mbid
          and r.listen_date    = e.listen_date
),

classified as (
    select
        *,
        -- The precise ownership cause is preserved even though several causes share one terminal
        -- state: "nobody gets paid" is the same outcome, but "no ownership record" and "two valid
        -- owners" need different fixes.
        case
            when resolution_status is null then 'NOT_APPLICABLE'
            when resolution_status in ('RESOLVED', 'RATE_CARD_GAP', 'RATE_CARD_OVERLAP')
                then 'OWNERSHIP_RESOLVED'
            when resolution_status = 'DEFECTIVE_OWNERSHIP' then 'OWNERSHIP_DEFECTIVE'
            when resolution_status = 'COVERED_ONLY_BY_INVALID_SET'
                then 'OWNERSHIP_COVERED_ONLY_BY_INVALID_SET'
            when resolution_status = 'NO_OWNERSHIP_RECORD' then 'OWNERSHIP_MISSING'
            when resolution_status = 'OWNERSHIP_GAP_FOR_DATE' then 'OWNERSHIP_GAP_FOR_DATE'
            when resolution_status = 'MULTIPLE_VALID_SETS' then 'OWNERSHIP_AMBIGUOUS'
            else null
        end as ownership_status,

        case
            when covering_rate_rows is null then 'NOT_APPLICABLE'
            when covering_rate_rows = 1 then 'RATE_RESOLVED'
            when covering_rate_rows = 0 then 'RATE_GAP'
            else 'RATE_AMBIGUOUS'
        end as rate_status
    from joined
),

-- The gates are evaluated here; the columns that may only exist for an attributed listen are
-- nulled in the final SELECT, which needs `attribution_status` to already exist.
priced as (
    select
    listen_hash,
    listened_at,
    listen_date,
    recording_mbid,
    match_status,
    match_method,
    match_tier,
    match_payout_eligible,
    match_hold_reason,
    ownership_status,
    rate_status,
    split_version_id,
    rule_version_id,
    rate_per_stream,
    currency,

    case
        when match_status != 'MATCHED' then 'UNMATCHED'
        when match_method in ('SCORED_FALLBACK_UNIQUE') then 'MATCH_RISK_POLICY'
        when ownership_status not in ('OWNERSHIP_RESOLVED') then 'DEFECTIVE_OWNERSHIP'
        when rate_status = 'RATE_GAP' then 'RATE_CARD_GAP'
        when rate_status = 'RATE_AMBIGUOUS' then 'RATE_CARD_AMBIGUOUS'
        when rate_status = 'RATE_RESOLVED' then 'ATTRIBUTABLE'
        else null
    end as attribution_status,

    -- Payable means every gate passed. Nothing else is payable, including a listen whose only
    -- problem is a missing rate on one of three days.
    (
        match_status = 'MATCHED'
        and match_method not in ('SCORED_FALLBACK_UNIQUE')
        and ownership_status = 'OWNERSHIP_RESOLVED'
        and rate_status = 'RATE_RESOLVED'
    ) as payout_eligible,

    -- The reason nothing is being paid, NULL exactly when it is. Carried from the match gate when
    -- that is what stopped it, otherwise named for the gate that did.
    case
        when match_status != 'MATCHED' then match_hold_reason
        when match_method in ('SCORED_FALLBACK_UNIQUE') then 'MATCH_RISK_POLICY'
        when ownership_status not in ('OWNERSHIP_RESOLVED') then 'DEFECTIVE_OWNERSHIP'
        when rate_status = 'RATE_GAP' then 'RATE_CARD_GAP'
        when rate_status = 'RATE_AMBIGUOUS' then 'RATE_CARD_AMBIGUOUS'
        else null
    end as hold_reason,

    '{{ var("payout_policy_version") }}' as payout_policy_version,
    '{{ var("attribution_run_id") }}' as attribution_run_id

from classified
)

select
    listen_hash,
    listened_at,
    listen_date,
    recording_mbid,
    match_status,
    match_method,
    match_tier,
    match_payout_eligible,
    match_hold_reason,
    ownership_status,
    rate_status,
    attribution_status,
    payout_eligible,
    hold_reason,
    if(attribution_status = 'ATTRIBUTABLE', split_version_id, null) as split_version_id,
    if(attribution_status = 'ATTRIBUTABLE', rule_version_id, null) as rule_version_id,
    if(attribution_status = 'ATTRIBUTABLE', rate_per_stream, null) as rate_per_stream,
    if(attribution_status = 'ATTRIBUTABLE', currency, null) as currency,
    split_version_id as resolved_split_version_id,
    payout_policy_version,
    attribution_run_id
from priced
