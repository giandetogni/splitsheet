{{ config(materialized = 'table') }}

-- THE RESTATEMENT RECONCILIATION. Two identities that must hold exactly, in published cents.
--
--   1. SUM(delta) = new portfolio total - prior portfolio total
--   2. every listen in the prior publication's corpus is still accounted for in the new one
--
-- The first is the arithmetic claim: if the deltas do not add up to the difference between the two
-- portfolio totals, the delta mart is describing a restatement that did not happen.
--
-- The second is the completeness claim, and it is the one an auditor actually asks. A restatement
-- that quietly dropped listens would improve every rate in the report while destroying money.
--
-- No amount here is revenue: the rate card and ownership are MODELED, so these are illustrative
-- modeled amounts over real listening events.

{% set prior_run = var('prior_attribution_run_id') %}
{% set new_run = var('attribution_run_id') %}

with prior_total as (
    select sum(holder_payout) as portfolio_paid, count(*) as holder_rows,
           count(distinct recording_mbid) as recordings,
           count(distinct rights_holder_id) as holders
    from {{ ref('fct_royalty_attribution') }} where attribution_run_id = '{{ prior_run }}'
),

new_total as (
    select sum(holder_payout) as portfolio_paid, count(*) as holder_rows,
           count(distinct recording_mbid) as recordings,
           count(distinct rights_holder_id) as holders
    from {{ ref('fct_royalty_attribution') }} where attribution_run_id = '{{ new_run }}'
),

delta_total as (
    select sum(delta) as summed_delta,
           sum(if(change_type = 'HOLDER_ADDED', delta, numeric '0')) as delta_from_added_holders,
           sum(if(change_type = 'HOLDER_REMOVED', delta, numeric '0')) as delta_from_removed_holders,
           sum(if(change_type = 'PAYOUT_INCREASED', delta, numeric '0')) as delta_from_increases,
           sum(if(change_type = 'PAYOUT_DECREASED', delta, numeric '0')) as delta_from_decreases,
           countif(change_type = 'HOLDER_ADDED') as holders_added,
           countif(change_type = 'HOLDER_REMOVED') as holders_removed,
           countif(change_type = 'PAYOUT_INCREASED') as payouts_increased,
           countif(change_type = 'PAYOUT_DECREASED') as payouts_decreased,
           countif(change_type = 'UNCHANGED') as unchanged_rows
    from {{ ref('fct_restatements') }}
    where restatement_run_id = '{{ var("restatement_run_id") }}'
),

-- The listen-level side: nothing may disappear, and every change of state is classified.
listens as (
    select
        count(*) as listens_total,
        countif(v.match_status = 'MATCHED') as prior_matched,
        countif(r.match_status = 'MATCHED') as new_matched,
        countif(v.match_status != 'MATCHED' and r.match_status = 'MATCHED') as newly_matched,
        countif(v.match_status != 'MATCHED' and r.match_status != 'MATCHED') as still_unmatched,
        countif(v.match_status = 'MATCHED' and r.match_status != 'MATCHED') as matches_lost,
        countif(r.failure_reason = 'AMBIGUOUS_TIE'
                and ifnull(v.failure_reason, '') != 'AMBIGUOUS_TIE') as newly_ambiguous,
        countif(r.recording_cohort) as cohort_listens
    from {{ source('silver', 'silver_listen_matches_restated') }} r
    join {{ source('silver', 'silver_listen_matches') }} v using (listen_hash)
    where r.listened_at >= timestamp '2026-06-01 00:00:00+00'
      and r.listened_at <  timestamp '2026-07-01 00:00:00+00'
      and v.listened_at >= timestamp '2026-06-01 00:00:00+00'
      and v.listened_at <  timestamp '2026-07-01 00:00:00+00'
),

-- Financial dispositions before and after, so the waterfall shift is visible per category.
dispositions as (
    select
        countif(attribution_status = 'ATTRIBUTABLE') as attributable_now,
        countif(attribution_status = 'MATCH_RISK_POLICY') as risk_held_now,
        countif(attribution_status = 'RATE_CARD_GAP') as rate_gap_now,
        countif(attribution_status = 'DEFECTIVE_OWNERSHIP') as defective_now,
        countif(attribution_status = 'UNMATCHED') as unmatched_now,
        count(*) as listens_classified
    from {{ ref('int_financial_disposition') }}
)

select
    '{{ var("prior_publication_id") }}' as prior_publication_id,
    '{{ var("new_publication_id") }}' as new_publication_id,
    '{{ var("restatement_run_id") }}' as restatement_run_id,
    '{{ var("trigger_reason") }}' as trigger_reason,

    p.portfolio_paid as prior_portfolio_paid,
    n.portfolio_paid as restated_portfolio_paid,
    (n.portfolio_paid - p.portfolio_paid) as portfolio_difference,
    d.summed_delta,
    -- THE IDENTITY. Exact, in NUMERIC, not approximate.
    (d.summed_delta = n.portfolio_paid - p.portfolio_paid) as delta_reconciles_exactly,

    p.holder_rows as prior_holder_rows,
    n.holder_rows as restated_holder_rows,
    p.recordings as prior_recordings_paid,
    n.recordings as restated_recordings_paid,

    d.delta_from_added_holders,
    d.delta_from_removed_holders,
    d.delta_from_increases,
    d.delta_from_decreases,
    d.holders_added,
    d.holders_removed,
    d.payouts_increased,
    d.payouts_decreased,
    d.unchanged_rows,

    l.listens_total,
    (l.listens_total = 38199641) as no_listen_disappeared,
    l.prior_matched,
    l.new_matched,
    l.newly_matched,
    l.still_unmatched,
    l.matches_lost,
    l.newly_ambiguous,
    l.cohort_listens,

    disp.attributable_now,
    disp.risk_held_now,
    disp.rate_gap_now,
    disp.defective_now,
    disp.unmatched_now,
    disp.listens_classified,
    (disp.listens_classified = 38199641) as dispositions_reconcile,

    current_timestamp() as measured_at
from prior_total p
cross join new_total n
cross join delta_total d
cross join listens l
cross join dispositions disp
