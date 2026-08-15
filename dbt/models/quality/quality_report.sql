{{ config(materialized = 'table', cluster_by = ['severity', 'model']) }}

-- QUERYABLE DATA QUALITY REPORT. One row per rule per run.
--
-- Not a log of dbt test failures: those are pass/fail. This is the count, the denominator, the
-- rate and the business consequence, kept as data so "how much royalty attribution is at risk
-- this run" is a query rather than a scroll through CI output.
--
-- SEVERITY is about what the pipeline is allowed to do, not about how alarming a rule sounds:
--   ERROR  the affected rows must not be paid; the pipeline holds them
--   WARN   the affected rows are usable, but something upstream is deteriorating
--
-- BUSINESS IMPACT names who gets hurt:
--   royalty_attribution_at_risk   streams cannot be attributed -> money sits unallocated
--   wrong_rights_holder_risk      the wrong party could be paid -> the expensive failure
--   ownership_allocation_at_risk  shares do not add up -> over- or under-payment
--
-- EVERY RULE HERE RUNS AGAINST THE MODELED SOURCE AS IT IS. The deliberate defects are expected
-- to appear; the report is compared against docs/phase0/rights_generation.json, which records how
-- many of each were injected. A rule that found zero would mean the check is broken, not that the
-- data is clean.

with sets as (
    select * from {{ ref('int_ownership_validity') }}
),

splits as (
    select * from {{ ref('stg_rights__ownership_splits') }}
),

holders as (
    select * from {{ ref('stg_rights__holders') }}
),

resolution as (
    select * from {{ ref('int_ownership_resolution') }}
),

eligibility as (
    select * from {{ ref('int_payout_eligibility') }}
),

rules as (

    -- share range ---------------------------------------------------------------------------
    select 'stg_rights__ownership_splits' as model, 'share_pct_not_negative' as rule,
           'ERROR' as severity, 'ownership_allocation_at_risk' as business_impact,
           countif(share_pct < numeric '0') as failed_records,
           count(*) as total_records
    from splits

    union all
    select 'stg_rights__ownership_splits', 'share_pct_at_most_100', 'ERROR',
           'ownership_allocation_at_risk',
           countif(share_pct > numeric '100'), count(*)
    from splits

    -- share sum -----------------------------------------------------------------------------
    union all
    select 'int_ownership_validity', 'valid_set_shares_sum_to_exactly_100', 'ERROR',
           'ownership_allocation_at_risk',
           countif(share_sum != numeric '100.0000'), count(*)
    from sets

    -- temporal integrity --------------------------------------------------------------------
    union all
    select 'int_ownership_validity', 'no_temporal_overlap', 'ERROR',
           'wrong_rights_holder_risk',
           countif(defect_temporal_overlap), count(*)
    from sets

    union all
    select 'int_ownership_validity', 'no_temporal_gap', 'ERROR',
           'royalty_attribution_at_risk',
           countif(defect_temporal_gap), count(*)
    from sets

    union all
    select 'int_ownership_validity', 'interval_is_valid', 'ERROR',
           'royalty_attribution_at_risk',
           countif(defect_invalid_interval), count(*)
    from sets

    union all
    select 'int_ownership_validity', 'split_set_has_one_interval', 'ERROR',
           'ownership_allocation_at_risk',
           countif(defect_split_set_has_two_intervals), count(*)
    from sets

    -- referential integrity -----------------------------------------------------------------
    union all
    select 'int_ownership_validity', 'recording_exists_in_catalogue', 'ERROR',
           'royalty_attribution_at_risk',
           countif(defect_orphan_recording), count(*)
    from sets

    union all
    select 'int_ownership_validity', 'rights_holder_exists', 'ERROR',
           'wrong_rights_holder_risk',
           countif(defect_missing_rights_holder), count(*)
    from sets

    union all
    select 'stg_rights__holders', 'holder_has_at_least_one_split', 'WARN',
           'royalty_attribution_at_risk',
           countif(s.rights_holder_id is null), count(*)
    from holders h
    left join (select distinct rights_holder_id from splits) s
           on s.rights_holder_id = h.holder_id

    -- the temporal join ---------------------------------------------------------------------
    union all
    select 'int_ownership_resolution', 'exactly_one_valid_ownership_set_per_recording_day',
           'ERROR', 'wrong_rights_holder_risk',
           countif(covering_valid_sets > 1), count(*)
    from resolution

    union all
    select 'int_ownership_resolution', 'eligible_recording_day_resolves_to_an_owner', 'ERROR',
           'royalty_attribution_at_risk',
           countif(resolution_status != 'RESOLVED'), count(*)
    from resolution

    union all
    select 'int_ownership_resolution', 'rate_card_covers_the_day', 'ERROR',
           'royalty_attribution_at_risk',
           countif(covering_rate_rows = 0), count(*)
    from resolution

    union all
    select 'int_ownership_resolution', 'rate_card_does_not_double_cover_the_day', 'ERROR',
           'ownership_allocation_at_risk',
           countif(covering_rate_rows > 1), count(*)
    from resolution

    -- payout eligibility --------------------------------------------------------------------
    -- Not a defect: a measured, intended hold. Reported at WARN so the volume of held revenue is
    -- visible in the same place as everything else, without claiming something is broken.
    union all
    select 'int_payout_eligibility', 'match_risk_policy_holds_fallback_unique', 'WARN',
           'royalty_attribution_at_risk',
           countif(hold_reason = 'MATCH_RISK_POLICY'), count(*)
    from eligibility

    union all
    select 'int_payout_eligibility', 'eligible_listens_carry_a_recording', 'ERROR',
           'royalty_attribution_at_risk',
           countif(payout_eligible and matched_recording_mbid is null), count(*)
    from eligibility

    union all
    select 'int_payout_eligibility', 'held_listens_carry_a_hold_reason', 'ERROR',
           'royalty_attribution_at_risk',
           countif(not payout_eligible and hold_reason is null), count(*)
    from eligibility
)

select
    -- dbt's invocation id: the run this measurement belongs to, so two runs can be compared
    -- instead of overwriting each other's meaning.
    '{{ invocation_id }}' as run_id,
    current_timestamp() as measured_at,
    model,
    rule,
    severity,
    business_impact,
    failed_records,
    total_records,
    round(safe_divide(failed_records, total_records), 8) as failure_rate,
    if(failed_records = 0, 'PASS', 'FAIL') as status,
    -- The MODELED source contains deliberate defects, so FAIL on those rules is the expected
    -- outcome. This column says which failures are expected findings and which would be news.
    if(rule in ('share_pct_not_negative', 'share_pct_at_most_100',
                'split_set_has_one_interval', 'rate_card_does_not_double_cover_the_day',
                'eligible_listens_carry_a_recording', 'held_listens_carry_a_hold_reason'),
       'must_be_zero', 'expected_to_find_injected_defects') as expectation
from rules
