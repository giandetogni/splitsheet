-- The waterfall must reconcile EXACTLY to 38,199,641 listens across mutually exclusive categories.
--
-- Exclusivity is structural (one row per listen carries one attribution_status), so what is checked
-- here is exhaustiveness and the total -- plus that the separate metrics have not been quietly
-- conflated: matched + unmatched must equal the total, and payable must equal ATTRIBUTABLE.
with w as (
    select attribution_status, listens, total_listens, matched_listens, unmatched_listens,
           payable_listens
    from {{ ref('royalty_reconciliation') }}
),
totals as (
    select sum(listens) as summed, max(total_listens) as total,
           max(matched_listens) as matched, max(unmatched_listens) as unmatched,
           max(payable_listens) as payable,
           max(if(attribution_status = 'ATTRIBUTABLE', listens, 0)) as attributable
    from w
)
select *
from totals
where summed != 38199641
   or total != 38199641
   or matched + unmatched != 38199641
   or payable != attributable
