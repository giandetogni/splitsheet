-- REAL matching output, narrowed to what the payout layer needs. The partition filter is
-- required on the source table and is applied here once.
--
-- This model does NOT decide eligibility. It carries the technical match forward unchanged.
--
-- TWO POSSIBLE SOURCES, selected by the `matches_source` var:
--
--   v1 (default)  splitsheet_silver.silver_listen_matches           normalization 1.0.0+0bc0dd643e06
--   restated      splitsheet_silver.silver_listen_matches_restated  normalization 1.1.0+b3253b155934
--
-- Both carry all 38,199,641 listens, so the SAME frozen financial gates run over either one and the
-- difference in the output is attributable to the normalization change alone. Switching the var is
-- how the restated publication is produced; it does not modify the v1 publication, which is
-- append-only and keyed by its own attribution_run_id.
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
    -- The restated table identifies its run as `restatement_run_id`; the v1 table calls it
    -- `match_run_id`. Aliasing here keeps every downstream model unaware of which source it is
    -- reading, which is what lets the SAME frozen gates run over both.
    {% if var('matches_source', 'v1') == 'restated' %}
    restatement_run_id as match_run_id
    {% else %}
    match_run_id
    {% endif %}
{% if var('matches_source', 'v1') == 'restated' %}
from {{ source('silver', 'silver_listen_matches_restated') }}
{% else %}
from {{ source('silver', 'silver_listen_matches') }}
{% endif %}
where listened_at >= timestamp '2026-06-01 00:00:00+00'
  and listened_at <  timestamp '2026-07-01 00:00:00+00'
