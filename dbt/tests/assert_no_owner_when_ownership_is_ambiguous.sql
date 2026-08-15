-- Nothing may carry an owner unless exactly one valid ownership set covers the day.
--
-- This is the ownership equivalent of the matcher's tie rule. A row here means a conflict was
-- resolved arbitrarily somewhere upstream -- by ANY_VALUE, by a MIN over competing rows, or by
-- ROW_NUMBER() = 1 -- which is how the wrong rights holder gets paid a plausible amount.
select recording_mbid, listen_date, covering_valid_sets, split_version_id, resolution_status
from {{ ref('int_ownership_resolution') }}
where (covering_valid_sets != 1 and split_version_id is not null)
   or (covering_valid_sets > 1 and resolution_status != 'MULTIPLE_VALID_SETS')
   or (is_attributable and (split_version_id is null or rate_per_stream is null))
