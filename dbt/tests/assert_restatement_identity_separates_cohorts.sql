-- THE COLLISION IS FIXED, AND THE FIX IS CHECKED IN THE WAREHOUSE, NOT ONLY IN PYTHON.
--
-- Scoped to the rows that describe a COHORT restatement, because those are the runs an identity has
-- to be able to tell apart. Three failures this catches, all regressions of the same defect:
--
--   1. two cohort runs sharing a canonical_run_id -- the identity stopped depending on the cohort;
--   2. two cohort runs sharing a cohort_sha256 -- the predicate stopped being distinguishing;
--   3. the rejected cohort disappearing -- the counter-example is what makes the proof a proof.
--
-- It also asserts the legacy id is still SHARED by both cohort runs, which sounds backwards but is
-- the point: the historical identifier is preserved exactly as it was, insufficiency included. If it
-- ever became unique, someone would have rewritten history to make the registry look tidier.

with cohort_runs as (
    select * from {{ ref('restatement_run_registry') }}
    where cohort_sha256 is not null
),

counts as (
    select
        count(*) as runs,
        count(distinct canonical_run_id) as distinct_canonical_ids,
        count(distinct cohort_sha256) as distinct_cohorts,
        count(distinct legacy_run_id) as distinct_legacy_ids,
        countif(run_type = 'PUBLISHED_RESTATEMENT') as published_runs,
        countif(run_type = 'REJECTED_BEFORE_PUBLICATION') as rejected_runs,
        countif(not canonical_inputs_available) as without_canonical_inputs
    from cohort_runs
)

select
    'restatement identity does not separate cohorts' as failure,
    runs, distinct_canonical_ids, distinct_cohorts, distinct_legacy_ids, published_runs
from counts
where distinct_canonical_ids != runs      -- one identity per cohort run
   or distinct_cohorts != runs            -- one cohort per cohort run
   or distinct_legacy_ids != 1            -- the legacy string was, and stays, shared
   or runs < 2                            -- the rejected cohort must remain as the counter-example
   or published_runs != 1
   or rejected_runs != 1
   or without_canonical_inputs != 0       -- a cohort run always has its canonical inputs
