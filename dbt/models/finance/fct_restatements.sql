{{ config(
    materialized = 'incremental',
    incremental_strategy = 'merge',
    unique_key = ['period', 'recording_mbid', 'rights_holder_id', 'prior_publication_id',
                  'new_publication_id', 'restatement_run_id'],
    on_schema_change = 'fail',
    cluster_by = ['restatement_run_id', 'change_type']
) }}

{% if flags.FULL_REFRESH %}
  {{ exceptions.raise_compiler_error(
       "REFUSED: --full-refresh on fct_restatements would erase the audit trail linking pub:v1 to "
       ~ "its restatement. Restatement rows are append-only, keyed by restatement_run_id.") }}
{% endif %}

-- THE RESTATEMENT, AS A DELTA. Never as an overwrite.
--
-- A restatement is not "the new number". It is the statement that a previously published figure has
-- changed, by how much, for which holder, and why -- with both publications still standing. So this
-- model is a FULL OUTER JOIN of two immutable publications, and every row carries prior, restated
-- and delta together with the versions on each side.
--
--     delta = restated_payout - prior_payout
--
-- NUMERIC throughout. A restatement that lost a cent to floating point would be worse than no
-- restatement at all, because it would look like a real difference.
--
-- FOUR CHANGE TYPES, and each one is a different real event:
--
--   HOLDER_ADDED      the holder was not paid in the prior publication and is paid now. This is what
--                     a newly matched recording looks like from the money's point of view.
--   HOLDER_REMOVED    paid before, not paid now. Its delta is negative and its restated payout is 0.
--   PAYOUT_INCREASED  same holder, more money -- e.g. more attributable streams for the recording.
--   PAYOUT_DECREASED  same holder, less money.
--   UNCHANGED         kept ONLY for the reconciliation to be checkable end to end, and only for
--                     holders that appear in both publications. Without it, SUM(delta) could not be
--                     tied back to the two portfolio totals inside one query.
--
-- The prior publication is read at its own attribution_run_id and is not modified by anything here.

{% set prior_run = var('prior_attribution_run_id') %}
{% set new_run = var('attribution_run_id') %}

with prior as (
    select
        period, recording_mbid, rights_holder_id, split_version_id, rate_card_id,
        holder_payout, gross_royalty, attributable_streams, holder_share_pct,
        payout_policy_version
    from {{ ref('fct_royalty_attribution') }}
    where attribution_run_id = '{{ prior_run }}'
),

restated as (
    select
        period, recording_mbid, rights_holder_id, split_version_id, rate_card_id,
        holder_payout, gross_royalty, attributable_streams, holder_share_pct,
        payout_policy_version
    from {{ ref('fct_royalty_attribution') }}
    where attribution_run_id = '{{ new_run }}'
),

-- Aggregated to the DELTA grain before comparing. A recording can be split across two rate windows
-- in one publication and one in the other, so comparing at the finer grain would report spurious
-- adds and removes for what is actually a change in how the period was sliced.
prior_agg as (
    select period, recording_mbid, rights_holder_id,
           sum(holder_payout) as prior_payout,
           sum(attributable_streams) as prior_streams,
           min(split_version_id) as prior_split_version_id,
           min(payout_policy_version) as prior_payout_policy_version,
           count(*) as prior_rows
    from prior group by period, recording_mbid, rights_holder_id
),

restated_agg as (
    select period, recording_mbid, rights_holder_id,
           sum(holder_payout) as restated_payout,
           sum(attributable_streams) as restated_streams,
           min(split_version_id) as new_split_version_id,
           min(payout_policy_version) as new_payout_policy_version,
           count(*) as restated_rows
    from restated group by period, recording_mbid, rights_holder_id
),

joined as (
    select
        coalesce(p.period, r.period) as period,
        coalesce(p.recording_mbid, r.recording_mbid) as recording_mbid,
        coalesce(p.rights_holder_id, r.rights_holder_id) as rights_holder_id,
        ifnull(p.prior_payout, numeric '0') as prior_payout,
        ifnull(r.restated_payout, numeric '0') as restated_payout,
        ifnull(p.prior_streams, 0) as prior_streams,
        ifnull(r.restated_streams, 0) as restated_streams,
        p.prior_split_version_id,
        r.new_split_version_id,
        coalesce(p.prior_payout_policy_version, r.new_payout_policy_version)
            as payout_policy_version,
        (p.rights_holder_id is null) as is_new_holder,
        (r.rights_holder_id is null) as is_removed_holder
    from prior_agg p
    full outer join restated_agg r
      using (period, recording_mbid, rights_holder_id)
)

select
    period,
    recording_mbid,
    rights_holder_id,
    '{{ var("prior_publication_id") }}' as prior_publication_id,
    '{{ var("new_publication_id") }}' as new_publication_id,
    '{{ var("restatement_run_id") }}' as restatement_run_id,

    prior_payout,
    restated_payout,
    -- The definition, written once: restated minus prior, in NUMERIC.
    (restated_payout - prior_payout) as delta,

    prior_streams,
    restated_streams,
    (restated_streams - prior_streams) as delta_streams,

    case
        when is_new_holder then 'HOLDER_ADDED'
        when is_removed_holder then 'HOLDER_REMOVED'
        when restated_payout > prior_payout then 'PAYOUT_INCREASED'
        when restated_payout < prior_payout then 'PAYOUT_DECREASED'
        else 'UNCHANGED'
    end as change_type,

    '{{ var("trigger_reason") }}' as trigger_reason,
    '{{ var("prior_normalization_version") }}' as prior_normalization_version,
    '{{ var("new_normalization_version") }}' as new_normalization_version,
    '{{ var("prior_scoring_version") }}' as prior_scoring_version,
    '{{ var("new_scoring_version") }}' as new_scoring_version,
    prior_split_version_id as prior_split_version,
    new_split_version_id as new_split_version,
    payout_policy_version,
    current_timestamp() as run_timestamp

from joined

{% if is_incremental() %}
-- Same guard as the publication itself: a restatement run that has already been recorded is not
-- recorded twice, so re-running is a no-op rather than a duplicate audit trail.
where (select count(*) from {{ this }}
       where restatement_run_id = '{{ var("restatement_run_id") }}') = 0
{% endif %}
