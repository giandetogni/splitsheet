-- SUM(delta) must equal the difference between the two portfolio totals, exactly, in cents.
--
-- And no listen may disappear: a restatement that dropped rows would improve every rate in the
-- report while destroying money, which is the failure mode this test exists for.
select *
from {{ ref('restatement_reconciliation') }}
where not delta_reconciles_exactly
   or not no_listen_disappeared
   or not dispositions_reconcile
   or summed_delta != portfolio_difference
   or listens_total != 38199641
