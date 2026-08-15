-- BOUNDARY TEST, run against real ownership rows inside 2026-06.
--
-- Three probe dates per interval, with the expected answer declared BEFORE the join:
--
--     listen_date = valid_from        -> MUST be covered      (inclusive lower bound)
--     listen_date = valid_to - 1 day  -> MUST be covered      (last covered day)
--     listen_date = valid_to          -> MUST NOT be covered  (exclusive upper bound)
--
-- The probes are then resolved through the SAME half-open predicate the pipeline uses, and the
-- actual answer is compared with the expected one. Restating the predicate inside the assertion
-- would prove nothing: `valid_from >= valid_from` is true whatever the semantics are. What makes
-- this a test is that the expectation is fixed independently and the join has to agree with it.
--
-- Any row returned is a violation.

with intervals as (
    -- Real, valid, mid-period intervals: the second half of an ownership change on 2026-06-15,
    -- so the boundary being probed is one the corpus actually crosses.
    select recording_mbid, split_version_id, valid_from, valid_to
    from {{ ref('int_ownership_validity') }}
    where is_valid_set
      and valid_from = date '2026-06-15'
    limit 300
),

probes as (
    select recording_mbid, split_version_id, valid_from, valid_to,
           valid_from as probe_date, 'AT_VALID_FROM' as position, true as expected_covered
    from intervals
    union all
    select recording_mbid, split_version_id, valid_from, valid_to,
           date_sub(valid_to, interval 1 day), 'DAY_BEFORE_VALID_TO', true
    from intervals
    where valid_to < date '9999-12-31'   -- the sentinel has no meaningful "day before"
    union all
    select recording_mbid, split_version_id, valid_from, valid_to,
           valid_to, 'AT_VALID_TO', false
    from intervals
    where valid_to < date '9999-12-31'
),

-- Resolved through the pipeline's own predicate, not through a restatement of it.
resolved as (
    select
        p.recording_mbid,
        p.split_version_id,
        p.position,
        p.probe_date,
        p.valid_from,
        p.valid_to,
        p.expected_covered,
        exists (
            select 1
            from {{ ref('int_ownership_validity') }} v
            where v.recording_mbid   = p.recording_mbid
              and v.split_version_id = p.split_version_id
              and p.probe_date >= v.valid_from
              and p.probe_date <  v.valid_to
        ) as actually_covered
    from probes p
)

select *
from resolved
where actually_covered != expected_covered
