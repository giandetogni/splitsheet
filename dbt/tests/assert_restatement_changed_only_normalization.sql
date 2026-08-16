-- A restatement is allowed to change ONE input. Every restatement row must record the SAME payout
-- policy and the SAME scoring version on both sides, and a DIFFERENT normalization version -- or the
-- delta cannot be attributed to the trigger.
select *
from {{ ref('fct_restatements') }}
where restatement_run_id = '{{ var("restatement_run_id") }}'
  and (prior_scoring_version != new_scoring_version
    or prior_normalization_version = new_normalization_version
    or payout_policy_version != '{{ var("payout_policy_version_expected") }}'
    or trigger_reason is null)
