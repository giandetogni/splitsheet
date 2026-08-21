-- THE COLLISION IS FIXED, AND THE FIX IS CHECKED IN THE WAREHOUSE, NOT ONLY IN PYTHON.
--
-- Three failures this catches, all of them regressions of the same defect:
--
--   1. two runs sharing a canonical_run_id -- the identity stopped depending on the cohort;
--   2. two runs sharing a cohort_sha256 -- the cohort predicate stopped being distinguishing;
--   3. a run whose canonical id does not actually change when its cohort digest does.
--
-- It also asserts the legacy id is still AMBIGUOUS, which sounds backwards but is the point: the
-- historical identifier is preserved exactly as it was, insufficiency included. If it ever became
-- unique, someone would have rewritten history to make the registry look tidier.

with registry as (select * from {{ ref('restatement_run_registry') }}),

counts as (
    select
        count(*) as runs,
        count(distinct canonical_run_id) as distinct_canonical_ids,
        count(distinct cohort_sha256) as distinct_cohorts,
        count(distinct legacy_run_id) as distinct_legacy_ids,
        countif(cohort_status = 'PUBLISHED') as published_runs
    from registry
)

select
    'restatement identity does not separate cohorts' as failure,
    runs, distinct_canonical_ids, distinct_cohorts, distinct_legacy_ids, published_runs
from counts
where distinct_canonical_ids != runs      -- one identity per run
   or distinct_cohorts != runs            -- one cohort per run
   or distinct_legacy_ids != 1            -- the legacy string was, and stays, shared
   or runs < 2                            -- the rejected cohort must remain as the counter-example
   or published_runs != 1
