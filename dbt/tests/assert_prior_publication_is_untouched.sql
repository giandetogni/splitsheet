-- The prior publication must still hold exactly the rows and the total it was frozen with.
--
-- Values come from config/frozen_versions.yml via vars, so this test compares the warehouse against
-- the manifest rather than against itself.
select
    count(*) as row_count,
    sum(holder_payout) as portfolio_paid,
    min(published_at) as first_published_at,
    count(distinct publication_status) as statuses
from {{ ref('fct_royalty_attribution') }}
where attribution_run_id = '{{ var("prior_attribution_run_id") }}'
having count(*) != {{ var("prior_row_count") }}
    or sum(holder_payout) != numeric '{{ var("prior_portfolio_paid") }}'
