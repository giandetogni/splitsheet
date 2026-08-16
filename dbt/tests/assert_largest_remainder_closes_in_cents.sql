-- THE CLOSURE INVARIANT, asserted in cents and exactly.
--
--     SUM(holder_payout) = gross_royalty     for every group in the financial grain
--
-- Not "within a cent", not "within a tolerance". If this fails, a published statement does not
-- equal the sum of its parts, which is the failure that makes a royalty report unauditable.
--
-- Also asserted: the number of remainder cents distributed is between 0 and the holder count, and
-- exactly that many holders received one. More than one cent to a holder, or a negative remainder,
-- would mean the allocation logic is wrong even if the total happened to close.
with grouped as (
    select
        period, recording_mbid, split_version_id, rate_card_id,
        max(gross_royalty) as gross_royalty,
        sum(holder_payout) as summed_holder_payout,
        max(remainder_cents) as remainder_cents,
        max(holder_count) as holder_count,
        countif(remainder_rank <= remainder_cents) as holders_given_a_cent,
        min(holder_payout) as min_holder_payout
    from {{ ref('fct_royalty_attribution') }}
    where attribution_run_id = '{{ var("attribution_run_id") }}'
    group by period, recording_mbid, split_version_id, rate_card_id
)
select *
from grouped
where summed_holder_payout != gross_royalty
   or remainder_cents < 0
   or remainder_cents > holder_count
   or holders_given_a_cent != remainder_cents
   or min_holder_payout < numeric '0'
