{{ config(
    materialized = 'table',
    partition_by = {'field': 'listen_date', 'data_type': 'date'},
    cluster_by = ['recording_mbid']
) }}

-- Streams per recording per day, for payout-ELIGIBLE listens only.
--
-- This is the grain royalty attribution actually works at, and it is the grain the temporal
-- ownership join runs against: 8.9M recording-days instead of 31.6M listens, for the same
-- answer. Risk-held and unmatched listens are excluded here BUT they are not lost -- they remain
-- in int_payout_eligibility with their hold_reason, which is where the "why isn't this paying"
-- question is answered.
--
-- No money is computed anywhere in this model or downstream of it in Phase 5A.
select
    matched_recording_mbid as recording_mbid,
    listen_date,
    count(*) as streams,
    -- Carried so the temporal join can be segmented without re-reading the eligibility table.
    min(match_method) as min_match_method,
    max(match_method) as max_match_method
from {{ ref('int_payout_eligibility') }}
where payout_eligible
group by recording_mbid, listen_date
