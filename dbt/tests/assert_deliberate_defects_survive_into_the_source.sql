-- The MODELED source must STILL contain its deliberate defects.
--
-- If a future staging change starts filtering or repairing them, the quality layer would report a
-- clean pipeline over silently corrected data, which is worse than reporting the defects. So the
-- absence of each defect class is itself a failure. Expected counts come from
-- config/rights_model.yml and are recorded in docs/phase0/rights_generation.json.
with found as (
    select
        countif(defect_share_sum_not_100)      as share_sum,
        countif(defect_temporal_overlap)       as overlap,
        countif(defect_temporal_gap)           as gap,
        countif(defect_invalid_interval)       as invalid_interval,
        countif(defect_missing_rights_holder)  as missing_holder,
        countif(defect_orphan_recording)       as orphan_recording
    from {{ ref('int_ownership_validity') }}
)
select *
from found
where share_sum = 0 or overlap = 0 or gap = 0
   or invalid_interval = 0 or missing_holder = 0 or orphan_recording = 0
