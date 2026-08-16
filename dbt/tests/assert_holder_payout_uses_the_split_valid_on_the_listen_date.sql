-- The share applied must be the share valid on the listen's own date, not the latest share.
--
-- Every fact row's split_version_id must (a) exist as a valid ownership set, and (b) cover every
-- date on which the streams it prices actually occurred. A recording whose ownership changed
-- mid-June has two split sets and must appear as two fact rows with disjoint date ranges; if the
-- join had used the latest split instead, the pre-change streams would be priced under the
-- post-change owners and this test would return them.
with priced as (
    select f.recording_mbid, f.split_version_id, f.rights_holder_id, f.holder_share_pct,
           b.first_stream_date, b.last_stream_date
    from {{ ref('fct_royalty_attribution') }} f
    join {{ ref('int_attributable_streams') }} b
      using (period, recording_mbid, split_version_id, rate_card_id)
    where f.attribution_run_id = '{{ var("attribution_run_id") }}'
)
select p.*, v.valid_from, v.valid_to, o.share_pct as ownership_share_pct
from priced p
left join {{ ref('int_ownership_validity') }} v
       on v.recording_mbid = p.recording_mbid and v.split_version_id = p.split_version_id
left join {{ ref('stg_rights__ownership_splits') }} o
       on o.recording_mbid = p.recording_mbid and o.split_version_id = p.split_version_id
      and o.rights_holder_id = p.rights_holder_id
where v.split_version_id is null              -- priced against an ownership set that does not exist
   or not v.is_valid_set                      -- priced against a defective set
   or p.first_stream_date <  v.valid_from     -- streams before the set took effect
   or p.last_stream_date  >= v.valid_to       -- streams after it ended (half-open upper bound)
   or o.share_pct is null                     -- holder is not in that set
   or o.share_pct != p.holder_share_pct       -- share does not match the set
