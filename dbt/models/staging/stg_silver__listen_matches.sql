-- REAL matching output, narrowed to what the payout layer needs. The partition filter is
-- required on the source table and is applied here once.
--
-- This model does NOT decide eligibility. It carries the technical match forward unchanged.
select
    listen_hash,
    listened_at,
    date(listened_at) as listen_date,
    matched_recording_mbid,
    match_status,
    match_method,
    match_tier,
    match_score,
    failure_reason,
    blocking_key_information_class,
    scoring_version,
    match_run_id
from {{ source('silver', 'silver_listen_matches') }}
where listened_at >= timestamp '2026-06-01 00:00:00+00'
  and listened_at <  timestamp '2026-07-01 00:00:00+00'
