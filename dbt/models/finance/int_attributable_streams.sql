{{ config(materialized = 'table', cluster_by = ['recording_mbid']) }}

-- The money base: attributable streams per recording, per ownership split set, per rate window.
--
-- ONLY ATTRIBUTABLE LISTENS ENTER. Everything held at any gate is absent by construction rather
-- than filtered later, so nothing that failed a gate can reach an amount.
--
-- GRAIN: recording_mbid + split_version_id + rate_card_id (within the period).
--
-- WHY rate_card_id IS IN THE GRAIN, and this is a measured adjustment to the project's stated
-- grain rather than a preference: the MODELED rate card changes value inside the period
-- (0.003500000 until 2026-06-10, 0.003700000 from 2026-06-13). One row per recording per split
-- set would have to carry two different rates, and `gross_royalty = attributable_streams *
-- rate_per_stream` would stop being exactly true. Splitting by rate window keeps that identity
-- checkable on every single row.
--
-- The rate is joined on the SAME half-open predicate as everything else in this project:
-- listen_date >= valid_from AND listen_date < valid_to.

with attributable as (
    select recording_mbid, listen_date, split_version_id, rule_version_id
    from {{ ref('int_financial_disposition') }}
    where attribution_status = 'ATTRIBUTABLE'
),

rates as (
    select rate_card_id, valid_from, valid_to, rate_per_stream, currency, rule_version_id
    from {{ ref('stg_rights__rate_card') }}
),

joined as (
    select
        a.recording_mbid,
        a.split_version_id,
        r.rate_card_id,
        r.rule_version_id,
        r.rate_per_stream,
        r.currency,
        a.listen_date
    from attributable a
    join rates r
      on  a.listen_date >= r.valid_from
      and a.listen_date <  r.valid_to
)

select
    date '2026-06-01' as period,
    recording_mbid,
    split_version_id,
    rate_card_id,
    rule_version_id,
    rate_per_stream,
    currency,
    count(*) as attributable_streams,
    min(listen_date) as first_stream_date,
    max(listen_date) as last_stream_date,

    -- gross_royalty = attributable_streams * rate_per_stream, exactly, in NUMERIC. Never FLOAT:
    -- this value is the denominator of every holder amount below it.
    count(*) * rate_per_stream as gross_royalty_unrounded,
    round(count(*) * rate_per_stream, 2) as gross_royalty

from joined
group by period, recording_mbid, split_version_id, rate_card_id, rule_version_id,
         rate_per_stream, currency
