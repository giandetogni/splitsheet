{{ config(materialized = 'table') }}

-- THE WATERFALL. All 38,199,641 listens, in mutually exclusive terminal categories that reconcile
-- exactly to the total.
--
-- FIVE DIFFERENT METRICS ARE KEPT APART, because collapsing them is how a pipeline reports 82.72%
-- and means four incompatible things by it:
--
--   match_coverage        did the matcher identify a recording?
--   payout_eligibility    do we trust HOW it identified it?
--   ownership_validity    do we know who owned it on that date?
--   rate_availability     do we know what a stream was worth on that date?
--   financial_attribution all of the above, therefore payable
--
-- At the listen grain one listen is one stream, so `listens` and `streams` are equal by
-- construction; both are reported because the downstream figure of interest is streams, and a
-- reader should not have to infer the identity. Recording-days and holder rows are reported for the
-- attributable category only, where they exist.

with disposition as (
    select * from {{ ref('int_financial_disposition') }}
),

terminal as (
    select
        attribution_status,
        count(*) as listens,
        count(*) as streams,                       -- one listen is one stream at this grain
        count(distinct recording_mbid) as distinct_recordings,
        count(distinct if(attribution_status = 'ATTRIBUTABLE',
                          format('%t|%t', recording_mbid, listen_date), null))
            as attributable_recording_days
    from disposition
    group by attribution_status
),

-- The intermediate metrics, each measured on its own denominator rather than derived from the
-- terminal categories.
metrics as (
    select
        countif(match_status = 'MATCHED') as matched_listens,
        countif(match_status != 'MATCHED') as unmatched_listens,
        countif(match_payout_eligible) as match_gate_passed,
        countif(match_status = 'MATCHED' and not match_payout_eligible) as match_gate_held,
        countif(ownership_status = 'OWNERSHIP_RESOLVED') as ownership_resolved,
        countif(ownership_status not in ('OWNERSHIP_RESOLVED', 'NOT_APPLICABLE'))
            as ownership_failed,
        countif(rate_status = 'RATE_RESOLVED') as rate_resolved,
        countif(rate_status = 'RATE_GAP') as rate_gap,
        countif(rate_status = 'RATE_AMBIGUOUS') as rate_ambiguous,
        countif(payout_eligible) as payable_listens,
        count(*) as total_listens
    from disposition
),

money as (
    select
        count(*) as holder_rows,
        count(distinct recording_mbid) as recordings_paid,
        count(distinct rights_holder_id) as holders_paid,
        sum(attributable_streams) as fact_streams,
        sum(holder_payout) as total_holder_payout
    from {{ ref('fct_royalty_attribution') }}
    where attribution_run_id = '{{ var("attribution_run_id") }}'
),

gross as (
    select sum(gross_royalty) as total_gross_royalty,
           sum(attributable_streams) as attributable_streams
    from {{ ref('int_attributable_streams') }}
)

select
    t.attribution_status,
    t.listens,
    t.streams,
    round(100 * t.listens / m.total_listens, 6) as pct_of_all_listens,
    t.distinct_recordings,
    t.attributable_recording_days,
    m.total_listens,
    m.matched_listens,
    m.unmatched_listens,
    m.match_gate_held,
    m.ownership_resolved,
    m.ownership_failed,
    m.rate_resolved,
    m.rate_gap,
    m.rate_ambiguous,
    m.payable_listens,
    g.attributable_streams as fact_input_streams,
    g.total_gross_royalty,
    mo.holder_rows,
    mo.recordings_paid,
    mo.holders_paid,
    mo.total_holder_payout,
    '{{ var("payout_policy_version") }}' as payout_policy_version,
    '{{ var("attribution_run_id") }}' as attribution_run_id
from terminal t
cross join metrics m
cross join gross g
cross join money mo
order by t.listens desc
