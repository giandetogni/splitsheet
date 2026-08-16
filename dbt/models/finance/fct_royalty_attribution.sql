{{ config(
    materialized = 'incremental',
    incremental_strategy = 'merge',
    unique_key = ['period', 'recording_mbid', 'rights_holder_id', 'split_version_id',
                  'rate_card_id', 'rule_version_id', 'payout_policy_version',
                  'attribution_run_id'],
    on_schema_change = 'fail',
    cluster_by = ['attribution_run_id', 'recording_mbid']
) }}

-- THE FINANCIAL FACT. One row per holder per recording per split set per rate window per run.
--
-- GRAIN, physical and asserted:
--   period + recording_mbid + rights_holder_id + split_version_id + rate_card_id
--   + rule_version_id + payout_policy_version + attribution_run_id
--
-- IMMUTABLE AND EFFECTIVELY APPEND-ONLY. A completed publication is never updated.
--
-- WHY `merge` RATHER THAN `append`: dbt-bigquery has no append strategy -- it offers merge,
-- insert_overwrite and microbatch. insert_overwrite would replace a partition, which is exactly the
-- destructive update the policy forbids. So the strategy is merge on the FULL grain, which includes
-- attribution_run_id, plus the guard at the bottom of this model:
--
--   * the guard makes the select return ZERO rows when this attribution_run_id is already
--     published, so a re-run performs no insert and no update at all -- that is the idempotency;
--   * because attribution_run_id is part of the unique key, rows from a NEW publication can never
--     match rows from an old one, so a later run cannot update an earlier publication's amounts.
--
-- Between them, a completed publication is unreachable by any subsequent run. This is enforced by
-- construction rather than by convention, and it is what leaves the previous publication queryable
-- for the Phase 6 restatement.
--
-- ONE ROUNDING POINT, LARGEST REMAINDER, DETERMINISTIC TIEBREAK.
--
-- Rounding each holder independently and accepting a residual is how a published gross stops
-- equalling the sum of its parts. Instead: the gross is rounded once, each holder's share is
-- floored to cents, and the leftover cents go one each to the largest discarded fractions, with
-- `rights_holder_id` ascending as the tiebreak. The invariant, in cents and exactly:
--
--     SUM(holder_payout) = gross_royalty     for every group in the grain
--
-- ROW_NUMBER() appears below and is NOT an arbitrary pick: it orders by a measured quantity with a
-- total tiebreak, which is the rounding policy itself. Compare with the matcher and the ownership
-- join, where ROW_NUMBER() = 1 would have chosen a winner among candidates that no measurement
-- separated -- and where it is therefore absent.
--
-- Every amount here is an ILLUSTRATIVE MODELED AMOUNT: the rate card and the ownership splits are
-- MODELED. The listens and recordings are real.

with base as (
    select * from {{ ref('int_attributable_streams') }}
),

-- The ownership split set that was in force. Joined on split_version_id, which already encodes
-- "this recording, from this date" -- so the share applied is the share valid on the listen's own
-- date, not the latest share.
holders as (
    select recording_mbid, split_version_id, rights_holder_id, share_pct
    from {{ ref('stg_rights__ownership_splits') }}
),

expanded as (
    select
        b.period,
        b.recording_mbid,
        b.split_version_id,
        b.rate_card_id,
        b.rule_version_id,
        b.rate_per_stream,
        b.currency,
        b.attributable_streams,
        b.gross_royalty_unrounded,
        b.gross_royalty,
        h.rights_holder_id,
        h.share_pct,
        -- Full internal precision. No rounding yet, on purpose.
        b.gross_royalty_unrounded * h.share_pct / numeric '100' as holder_unrounded
    from base b
    join holders h
      on  h.recording_mbid    = b.recording_mbid
      and h.split_version_id  = b.split_version_id
),

floored as (
    select
        *,
        cast(floor(holder_unrounded / numeric '0.01') as int64) as floor_cents,
        (holder_unrounded / numeric '0.01'
         - floor(holder_unrounded / numeric '0.01')) as remainder_fraction
    from expanded
),

group_totals as (
    select
        period, recording_mbid, split_version_id, rate_card_id,
        cast(round(max(gross_royalty) / numeric '0.01') as int64) as target_cents,
        sum(floor_cents) as floor_cents_sum,
        count(*) as holder_count
    from floored
    group by period, recording_mbid, split_version_id, rate_card_id
),

ranked as (
    select
        f.*,
        g.target_cents,
        g.floor_cents_sum,
        g.holder_count,
        g.target_cents - g.floor_cents_sum as remainder_cents,
        row_number() over (
            partition by f.period, f.recording_mbid, f.split_version_id, f.rate_card_id
            order by f.remainder_fraction desc, f.rights_holder_id asc
        ) as remainder_rank
    from floored f
    join group_totals g
      using (period, recording_mbid, split_version_id, rate_card_id)
)

select
    period,
    recording_mbid,
    rights_holder_id,
    split_version_id,
    rate_card_id,
    rule_version_id,
    '{{ var("payout_policy_version") }}' as payout_policy_version,
    '{{ var("attribution_run_id") }}' as attribution_run_id,

    attributable_streams,
    rate_per_stream,
    currency,
    gross_royalty,
    gross_royalty_unrounded,
    share_pct as holder_share_pct,
    holder_unrounded as holder_payout_unrounded,

    -- The published amount: floored cents plus at most one remainder cent.
    (cast(floor_cents + if(remainder_rank <= remainder_cents, 1, 0) as numeric)
     * numeric '0.01') as holder_payout,

    -- Kept so the allocation is auditable after the fact rather than only re-derivable.
    floor_cents,
    remainder_fraction,
    remainder_rank,
    remainder_cents,
    holder_count,

    '{{ var("publication_status") }}' as publication_status,
    current_timestamp() as published_at

from ranked

-- THE IMMUTABILITY GUARD. On an incremental run, if this attribution_run_id has already been
-- published, the whole select yields nothing: no update, no duplicate, no second published_at.
-- A scalar subquery, so it is evaluated once rather than per row.
{% if is_incremental() %}
where (select count(*) from {{ this }}
       where attribution_run_id = '{{ var("attribution_run_id") }}') = 0
{% endif %}
