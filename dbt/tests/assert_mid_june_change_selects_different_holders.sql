-- The temporal join must select DIFFERENT holders before and after an ownership change that
-- happens inside the project's real period (2026-06-15).
--
-- A row returned here means the join is not temporal at all: it means the same day range resolved
-- to the same owners on both sides of a change, which is what happens when a predicate uses only
-- valid_from, or uses <= on the upper bound, or ignores intervals entirely.

with changed_recordings as (
    select recording_mbid
    from {{ ref('int_ownership_validity') }}
    where is_valid_set
    group by recording_mbid
    -- One set ending exactly where the next begins: a clean mid-period ownership change.
    having countif(valid_to = date '2026-06-15') = 1
       and countif(valid_from = date '2026-06-15') = 1
    limit 200
),

owners_before as (
    select s.recording_mbid,
           string_agg(distinct o.rights_holder_id order by o.rights_holder_id) as holders
    from changed_recordings s
    join {{ ref('int_ownership_validity') }} v
      on v.recording_mbid = s.recording_mbid and v.is_valid_set
     and date '2026-06-14' >= v.valid_from and date '2026-06-14' < v.valid_to
    join {{ ref('stg_rights__ownership_splits') }} o
      on o.split_version_id = v.split_version_id and o.recording_mbid = v.recording_mbid
    group by s.recording_mbid
),

owners_after as (
    select s.recording_mbid,
           string_agg(distinct o.rights_holder_id order by o.rights_holder_id) as holders
    from changed_recordings s
    join {{ ref('int_ownership_validity') }} v
      on v.recording_mbid = s.recording_mbid and v.is_valid_set
     and date '2026-06-15' >= v.valid_from and date '2026-06-15' < v.valid_to
    join {{ ref('stg_rights__ownership_splits') }} o
      on o.split_version_id = v.split_version_id and o.recording_mbid = v.recording_mbid
    group by s.recording_mbid
)

select b.recording_mbid, b.holders as holders_on_06_14, a.holders as holders_on_06_15
from owners_before b
join owners_after a using (recording_mbid)
where b.holders = a.holders
