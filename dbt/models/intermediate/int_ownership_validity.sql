{{ config(materialized = 'table', cluster_by = ['recording_mbid']) }}

-- CLASSIFICATION OF EVERY OWNERSHIP SPLIT SET. One row per (recording_mbid, split_version_id).
--
-- Nothing is corrected here and nothing is dropped. Each split set is labelled with what is
-- wrong with it, and downstream models decide what to do with that label. The deliberate defects
-- in the MODELED source are detected here INDEPENDENTLY: the source carries no defect column, so
-- these are real checks rather than lookups.
--
-- WHAT COUNTS AS A DEFECT, and the business consequence of each:
--
--   SHARE_SUM_NOT_100      allocation would be over or under 100% -> ownership_allocation_at_risk
--   INVALID_INTERVAL       valid_to <= valid_from, so the set covers no day at all, silently
--   MISSING_RIGHTS_HOLDER  a share points at a holder that does not exist -> unpayable
--   ORPHAN_RECORDING       ownership for a recording outside the matched catalogue
--   TEMPORAL_OVERLAP       two sets claim the same day -> wrong_rights_holder_risk
--   TEMPORAL_GAP           a day inside the coverage span has no owner -> attribution at risk
--
-- Overlap and gap are properties of a RECORDING, not of one set, so they are computed across
-- sets and then attached to every set of that recording: a set that overlaps another is not
-- individually innocent.

with splits as (
    select * from {{ ref('stg_rights__ownership_splits') }}
),

holders as (
    select holder_id from {{ ref('stg_rights__holders') }}
),

-- The catalogue an ownership row is allowed to reference: recordings the frozen matcher actually
-- matched. Anything else is an orphan.
catalogue as (
    select distinct matched_recording_mbid as recording_mbid
    from {{ ref('stg_silver__listen_matches') }}
    where match_status = 'MATCHED'
),

-- Grain of a split SET: one recording, one effective date.
sets as (
    select
        recording_mbid,
        split_version_id,
        min(valid_from) as valid_from,
        min(valid_to)   as valid_to,
        count(*)        as holder_rows,
        count(distinct rights_holder_id) as distinct_holders,
        sum(share_pct)  as share_sum,          -- NUMERIC throughout
        min(share_pct)  as min_share,
        max(share_pct)  as max_share,
        countif(h.holder_id is null) as rows_with_unknown_holder,
        max(rights_version)    as rights_version,
        max(generation_run_id) as generation_run_id
    from splits s
    left join holders h on h.holder_id = s.rights_holder_id
    group by recording_mbid, split_version_id
),

-- A set must have ONE interval. If a split_version_id ever carried two different intervals the
-- grain assumption would be broken, so it is measured rather than assumed.
interval_consistency as (
    select recording_mbid, split_version_id,
           count(distinct format('%t|%t', valid_from, valid_to)) as distinct_intervals
    from splits
    group by recording_mbid, split_version_id
),

-- Overlap: does any OTHER set of the same recording intersect this one? Half-open intersection
-- is `a.from < b.to and b.from < a.to`.
overlaps as (
    select a.recording_mbid, a.split_version_id, count(*) as overlapping_sets
    from sets a
    join sets b
      on  a.recording_mbid   = b.recording_mbid
      and a.split_version_id != b.split_version_id
      and a.valid_from < b.valid_to
      and b.valid_from < a.valid_to
      -- An invalid interval intersects nothing meaningful; it is its own defect class.
      and a.valid_from < a.valid_to
      and b.valid_from < b.valid_to
    group by a.recording_mbid, a.split_version_id
),

-- Gap: within a recording's own coverage span, is there a day between the end of one set and the
-- start of the next? Ordered by valid_from, so the previous set's end is the reference point.
ordered as (
    select
        recording_mbid, split_version_id, valid_from, valid_to,
        lag(valid_to) over (partition by recording_mbid order by valid_from, valid_to)
            as previous_valid_to
    from sets
    where valid_from < valid_to
),

gaps as (
    select recording_mbid,
           countif(previous_valid_to is not null and valid_from > previous_valid_to) as gap_count,
           min(if(previous_valid_to is not null and valid_from > previous_valid_to,
                  previous_valid_to, null)) as first_gap_start,
           min(if(previous_valid_to is not null and valid_from > previous_valid_to,
                  valid_from, null)) as first_gap_end
    from ordered
    group by recording_mbid
)

select
    s.recording_mbid,
    s.split_version_id,
    s.valid_from,
    s.valid_to,
    s.holder_rows,
    s.distinct_holders,
    s.share_sum,
    s.min_share,
    s.max_share,
    s.rights_version,
    s.generation_run_id,

    -- Individual defect flags, each independently checkable.
    (s.share_sum != numeric '100.0000')                 as defect_share_sum_not_100,
    (s.valid_to <= s.valid_from)                        as defect_invalid_interval,
    (s.rows_with_unknown_holder > 0)                    as defect_missing_rights_holder,
    (c.recording_mbid is null)                          as defect_orphan_recording,
    (ifnull(o.overlapping_sets, 0) > 0)                 as defect_temporal_overlap,
    (ifnull(g.gap_count, 0) > 0)                        as defect_temporal_gap,
    (s.min_share < numeric '0' or s.max_share > numeric '100') as defect_share_out_of_range,
    (i.distinct_intervals > 1)                          as defect_split_set_has_two_intervals,

    ifnull(o.overlapping_sets, 0) as overlapping_sets,
    ifnull(g.gap_count, 0)        as gap_count,
    g.first_gap_start,
    g.first_gap_end,

    -- A set is usable for attribution only if NOTHING is wrong with it. Deliberately strict:
    -- partial usability is how a wrong holder gets paid a plausible-looking amount.
    (
        s.share_sum = numeric '100.0000'
        and s.valid_to > s.valid_from
        and s.rows_with_unknown_holder = 0
        and c.recording_mbid is not null
        and ifnull(o.overlapping_sets, 0) = 0
        and ifnull(g.gap_count, 0) = 0
        and s.min_share >= numeric '0'
        and s.max_share <= numeric '100'
        and i.distinct_intervals = 1
    ) as is_valid_set

from sets s
left join catalogue c using (recording_mbid)
left join overlaps o using (recording_mbid, split_version_id)
left join gaps g using (recording_mbid)
left join interval_consistency i using (recording_mbid, split_version_id)
