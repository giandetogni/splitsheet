{{ config(materialized = 'view') }}

-- THE CURRENT PUBLICATION, selected by pointer rather than by recency.
--
-- `publication_pointer` is a one-row table maintained outside dbt (src/payout/publish.py). Moving
-- the pointer changes which publication is in force and touches NO published row: the fact table is
-- append-only and every historical publication remains queryable by its attribution_run_id.
--
-- Deliberately NOT `where published_at = (select max(published_at) ...)`: latest is not the same as
-- current. A restatement in Phase 6 may need to publish a new version and leave the pointer on the
-- old one until the new figures are approved, and "current" has to be able to say that.
select f.*
from {{ ref('fct_royalty_attribution') }} f
join {{ source('finance', 'publication_pointer') }} p
  on  p.attribution_run_id = f.attribution_run_id
 and p.pointer_name = 'CURRENT'
