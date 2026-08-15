{{ config(
    materialized = 'table',
    partition_by = {'field': 'listen_date', 'data_type': 'date'},
    cluster_by = ['resolution_status', 'recording_mbid']
) }}

-- THE TEMPORAL JOIN. For every payout-eligible recording-day, which ownership set was in force?
--
-- HALF-OPEN, ALWAYS:
--
--     listen_date >= valid_from AND listen_date < valid_to
--
-- valid_from is inside, valid_to is outside. An ownership change on 2026-06-15 therefore means
-- the old owners hold 2026-06-14 and the new owners hold 2026-06-15, with no day claimed twice
-- and no day left unclaimed. Tested at all three boundary positions in
-- tests/assert_ownership_boundary_is_half_open.sql.
--
-- NO CONFLICT IS RESOLVED BY ARBITRARY CHOICE, and the owner is unique BY CONSTRUCTION rather
-- than by a later filter: the `sole_valid_set` CTE keeps only groups of exactly one covering set,
-- so the MIN inside it returns that one row instead of picking from a set. There is no ANY_VALUE
-- over competing candidates and no ROW_NUMBER() = 1 anywhere in this model. When two valid sets
-- cover the same day the row is classified MULTIPLE_VALID_SETS and carries no owner at all --
-- the same discipline the matcher applies to an ambiguous tie. Silently picking one would pay a
-- plausible-looking wrong holder, which is the exact failure this project exists to avoid.
--
-- NO MONEY IS COMPUTED. Streams, share and rate are brought together and classified; they are
-- deliberately not multiplied. Payout arithmetic is a later phase and needs validated ownership
-- first, which is what this model produces.

with days as (
    select * from {{ ref('int_eligible_recording_days') }}
),

sets as (
    select * from {{ ref('int_ownership_validity') }}
),

rates as (
    select * from {{ ref('stg_rights__rate_card') }}
),

-- Every VALID set covering the day. Half-open on both sides of the comparison.
covering_valid as (
    select
        d.recording_mbid,
        d.listen_date,
        d.streams,
        s.split_version_id,
        s.valid_from,
        s.valid_to,
        s.share_sum,
        s.distinct_holders
    from days d
    join sets s
      on  s.recording_mbid = d.recording_mbid
      and s.is_valid_set
      and d.listen_date >= s.valid_from
      and d.listen_date <  s.valid_to
),

-- How many valid sets cover the day. Counting comes first, so "more than one" is a visible
-- classification instead of being silently multiplied into two output rows.
coverage_count as (
    select recording_mbid, listen_date, count(*) as covering_valid_sets
    from covering_valid
    group by recording_mbid, listen_date
),

-- UNIQUENESS BY CONSTRUCTION: only groups with exactly one covering valid set survive the
-- HAVING, so MIN over that grain is that single row rather than an arbitrary member of a set.
sole_valid_set as (
    select
        recording_mbid,
        listen_date,
        min(split_version_id) as split_version_id,
        min(valid_from) as valid_from,
        min(valid_to)   as valid_to,
        min(distinct_holders) as distinct_holders
    from covering_valid
    group by recording_mbid, listen_date
    having count(*) = 1
),

-- Context for the days that resolved to nothing: does the recording have ANY ownership at all,
-- does it have ownership that is merely defective, or is the day simply outside every interval?
-- Three different problems with three different owners, so they are not collapsed into one.
context as (
    select
        recording_mbid as ctx_recording_mbid,
        count(*) as total_sets,
        countif(is_valid_set) as valid_sets,
        countif(not is_valid_set) as defective_sets,
        countif(defect_temporal_gap) as sets_flagged_gap,
        countif(defect_temporal_overlap) as sets_flagged_overlap
    from sets
    group by recording_mbid
),

covering_any as (
    select distinct d.recording_mbid as cov_recording_mbid, d.listen_date as cov_listen_date
    from days d
    join sets s
      on  s.recording_mbid = d.recording_mbid
      and d.listen_date >= s.valid_from
      and d.listen_date <  s.valid_to
),

rate_for_day as (
    select
        d.recording_mbid,
        d.listen_date,
        count(r.rate_card_id) as covering_rate_rows,
        min(r.rate_per_stream) as rate_per_stream,
        min(r.currency) as currency,
        min(r.rule_version_id) as rule_version_id
    from days d
    left join rates r
      on  d.listen_date >= r.valid_from
      and d.listen_date <  r.valid_to
    group by d.recording_mbid, d.listen_date
)

select
    d.recording_mbid,
    d.listen_date,
    d.streams,

    ifnull(cnt.covering_valid_sets, 0) as covering_valid_sets,
    ctx.total_sets,
    ctx.valid_sets,
    ctx.defective_sets,

    -- Populated only for days that had exactly one covering valid set: the CTE they come from
    -- cannot contain a day with two.
    sole.split_version_id,
    sole.valid_from,
    sole.valid_to,
    sole.distinct_holders,

    rate.covering_rate_rows,
    -- Guarded by covering_rate_rows = 1, so the MIN in rate_for_day is that one row's value. A
    -- day covered by two rate rows is classified RATE_CARD_OVERLAP and priced by neither.
    if(rate.covering_rate_rows = 1, rate.rate_per_stream, null) as rate_per_stream,
    if(rate.covering_rate_rows = 1, rate.currency, null) as currency,
    if(rate.covering_rate_rows = 1, rate.rule_version_id, null) as rule_version_id,

    case
        when ifnull(cnt.covering_valid_sets, 0) > 1 then 'MULTIPLE_VALID_SETS'
        when ifnull(cnt.covering_valid_sets, 0) = 1 and rate.covering_rate_rows = 0
            then 'RATE_CARD_GAP'
        when ifnull(cnt.covering_valid_sets, 0) = 1 and rate.covering_rate_rows > 1
            then 'RATE_CARD_OVERLAP'
        when ifnull(cnt.covering_valid_sets, 0) = 1 then 'RESOLVED'
        when ctx.ctx_recording_mbid is null then 'NO_OWNERSHIP_RECORD'
        when ctx.valid_sets = 0 and ctx.defective_sets > 0 then 'DEFECTIVE_OWNERSHIP'
        when cov.cov_recording_mbid is not null then 'COVERED_ONLY_BY_INVALID_SET'
        else 'OWNERSHIP_GAP_FOR_DATE'
    end as resolution_status,

    -- Attributable means: exactly one valid owner set AND exactly one rate. Both, or neither --
    -- an amount cannot be computed from half of a contract.
    (ifnull(cnt.covering_valid_sets, 0) = 1 and rate.covering_rate_rows = 1) as is_attributable

from days d
left join coverage_count cnt using (recording_mbid, listen_date)
left join sole_valid_set sole using (recording_mbid, listen_date)
left join context ctx on ctx.ctx_recording_mbid = d.recording_mbid
left join covering_any cov
       on cov.cov_recording_mbid = d.recording_mbid and cov.cov_listen_date = d.listen_date
left join rate_for_day rate using (recording_mbid, listen_date)
