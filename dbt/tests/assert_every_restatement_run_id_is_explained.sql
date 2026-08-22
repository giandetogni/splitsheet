-- EVERY IDENTIFIER IN THE DELTA MART RESOLVES TO EXACTLY ONE EXPLANATION. No exceptions by name.
--
-- The earlier version of this test allowed `restate:pending` explicitly, which is not an invariant
-- but a note: "everything must be explained, except the thing that is not explained" cannot fail on
-- the next unexplained id. So the placeholder was given a real registry entry classifying it as a
-- LEGACY_REHEARSAL with no canonical inputs, and the exception was deleted from here.
--
-- Resolution goes through `mart_run_id`, which is NULL for a run that never wrote a row. That
-- matters: the legacy identifier is SHARED by two cohort runs, so joining on legacy_run_id would
-- resolve the published id to two rows and make "exactly one" unsatisfiable without rewriting
-- history. Only one of those runs actually wrote the rows, and only that one claims the mart id.
--
-- Three ways to fail, and each is a different real problem:
--   0 entries   an id nobody can account for -- provenance lost;
--   >1 entries  an id two runs both claim -- the collision, returned;
--   delta drift the registry's recorded delta disagrees with what the mart actually holds.

with registry as (select * from {{ ref('restatement_run_registry') }}),

mart as (
    select restatement_run_id, count(*) as rows_in_mart, sum(delta) as summed_delta
    from {{ ref('fct_restatements') }}
    group by restatement_run_id
),

resolution as (
    select
        m.restatement_run_id,
        m.rows_in_mart,
        m.summed_delta,
        count(r.registry_key) as registry_entries,
        min(r.recorded_delta) as recorded_delta,
        min(r.rows_in_delta_mart) as recorded_rows
    from mart m
    left join registry r on r.mart_run_id = m.restatement_run_id
    group by 1, 2, 3
)

select
    case
        when registry_entries = 0
            then 'no registry entry explains this run id: ' || restatement_run_id
        when registry_entries > 1
            then 'more than one registry entry claims this run id: ' || restatement_run_id
        when recorded_delta != summed_delta
            then 'the registry records a delta the mart does not hold: ' || restatement_run_id
        else 'the registry records a row count the mart does not hold: ' || restatement_run_id
    end as failure,
    restatement_run_id, registry_entries, rows_in_mart, recorded_rows,
    summed_delta, recorded_delta
from resolution
where registry_entries != 1
   or recorded_delta is null
   or recorded_delta != summed_delta
   or recorded_rows != rows_in_mart
