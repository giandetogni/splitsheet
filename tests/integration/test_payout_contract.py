"""INTEGRATION tests for the published financial statement. Real BigQuery, real cost.

The dbt tests already assert the invariants inside the warehouse. These add the three things SQL
alone cannot:

  * PARITY -- the published allocation is recomputed in Python, from the published inputs, and must
    agree cent for cent. Two implementations of a money rule that are never compared is how a
    rounding difference reaches a statement.
  * TYPES -- no monetary column is FLOAT, checked against INFORMATION_SCHEMA rather than assumed
    from the model SQL.
  * IMMUTABILITY -- two publications coexist, the non-current one is still queryable, and the
    current pointer selects rather than deletes.

Nothing here writes. Nothing regenerates rights or touches the frozen Phase 4B result.
"""

from __future__ import annotations

import pathlib
import sys
from collections import defaultdict
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 60 * 1024**3

REPO = pathlib.Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "src"))
from payout.policy import attribution_run_id, load_payout_policy
from payout.rounding import allocate

POLICY = load_payout_policy()
PUBLISHED_RUN = attribution_run_id(POLICY, "PUBLISHED")
REHEARSAL_RUN = attribution_run_id(POLICY, "REHEARSAL")

LISTENS = 38_199_641
FACT = f"{PROJECT}.{DATASET}.fct_royalty_attribution"
DISPOSITION = f"{PROJECT}.{DATASET}.int_financial_disposition"

MONEY_COLUMNS = {"rate_per_stream", "gross_royalty", "gross_royalty_unrounded",
                 "holder_share_pct", "holder_payout", "holder_payout_unrounded",
                 "remainder_fraction"}


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def one(client, sql: str) -> dict:
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES)
    return next(iter(dict(r) for r in client.query(sql, job_config=cfg).result()))


def rows(client, sql: str) -> list[dict]:
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES)
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


# --- one disposition per listen, reconciling exactly --------------------------------------

@pytest.mark.integration_readonly
def test_exactly_one_financial_disposition_per_listen(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_listens,
               COUNTIF(attribution_status IS NULL) AS null_status,
               COUNT(DISTINCT attribution_status) AS states,
               COUNT(DISTINCT payout_policy_version) AS policy_versions
        FROM `{DISPOSITION}`
    """)
    assert r["n"] == r["distinct_listens"] == LISTENS
    assert r["null_status"] == 0, "a NULL disposition means an unmapped policy state reached data"
    assert r["policy_versions"] == 1


@pytest.mark.integration_readonly
def test_the_waterfall_reconciles_to_every_listen(bq):
    got = {r["attribution_status"]: int(r["listens"]) for r in rows(bq, f"""
        SELECT attribution_status, COUNT(*) AS listens FROM `{DISPOSITION}`
        GROUP BY attribution_status
    """)}
    assert sum(got.values()) == LISTENS
    assert set(got) <= set(POLICY.attribution_states)
    # The measured shape of the corpus, asserted so a silent shift is caught.
    assert got["ATTRIBUTABLE"] == 28_386_887
    assert got["UNMATCHED"] == 6_601_112
    assert got["RATE_CARD_GAP"] == 3_164_839
    assert got["MATCH_RISK_POLICY"] == 35_007
    assert got["DEFECTIVE_OWNERSHIP"] == 11_796
    assert "UNKNOWN" not in got


@pytest.mark.integration_readonly
def test_the_five_metrics_are_not_conflated(bq):
    """Match coverage, payout eligibility, ownership validity, rate availability and financial
    attribution are FIVE DIFFERENT NUMBERS, each measured on its own denominator.

    THEY ARE NOT NESTED, and the first version of this test wrongly assumed they were. Ownership and
    rate resolution are properties of a recording-DAY, so they are resolved for plenty of listens
    that are held for an earlier reason: 9,968 risk-held listens have a resolved rate and 11,158 have
    resolved ownership, because they share a recording-day with an attributable listen. Only
    `payout_eligible` requires every gate at once.

    Asserting a false nesting would have hidden exactly the leak that produced those numbers.
    """
    r = one(bq, f"""
        SELECT COUNTIF(match_status = 'MATCHED') AS matched,
               COUNTIF(match_payout_eligible) AS match_gate_passed,
               COUNTIF(ownership_status = 'OWNERSHIP_RESOLVED') AS ownership_resolved,
               COUNTIF(rate_status = 'RATE_RESOLVED') AS rate_resolved,
               COUNTIF(payout_eligible) AS payable,
               COUNTIF(rate_status = 'RATE_RESOLVED'
                       AND attribution_status = 'ATTRIBUTABLE') AS rate_and_attributable,
               COUNTIF(rate_status = 'RATE_RESOLVED'
                       AND attribution_status = 'MATCH_RISK_POLICY') AS rate_but_risk_held,
               COUNTIF(rate_status = 'RATE_RESOLVED'
                       AND attribution_status = 'DEFECTIVE_OWNERSHIP') AS rate_but_defective,
               COUNTIF(ownership_status = 'OWNERSHIP_RESOLVED'
                       AND attribution_status = 'MATCH_RISK_POLICY') AS own_but_risk_held,
               COUNTIF(ownership_status = 'OWNERSHIP_RESOLVED'
                       AND attribution_status = 'RATE_CARD_GAP') AS own_but_gap
        FROM `{DISPOSITION}`
    """)
    assert r["matched"] == 31_598_529
    assert r["match_gate_passed"] == 31_563_522
    assert r["ownership_resolved"] == 31_562_884
    assert r["rate_resolved"] == 28_407_435
    assert r["payable"] == 28_386_887

    # The match gate is the only strictly nested one: it can only remove matched listens.
    assert r["matched"] - r["match_gate_passed"] == POLICY.expected_held_listens

    # Each later metric decomposes EXACTLY into the terminal states it spans, which is what proves
    # the metrics are being kept apart rather than quietly conflated.
    assert (r["rate_and_attributable"] + r["rate_but_risk_held"] + r["rate_but_defective"]
            == r["rate_resolved"])
    assert (r["rate_and_attributable"] + r["own_but_risk_held"] + r["own_but_gap"]
            == r["ownership_resolved"])
    assert r["rate_and_attributable"] == r["payable"] == 28_386_887
    assert r["rate_but_risk_held"] == 9_968


# --- held listens never produce money ------------------------------------------------------

@pytest.mark.integration_readonly
def test_match_risk_listens_stay_matched_and_carry_no_money(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS held,
               COUNTIF(match_status = 'MATCHED') AS still_matched,
               COUNTIF(payout_eligible) AS payable,
               COUNTIF(rate_per_stream IS NOT NULL) AS with_a_rate,
               COUNTIF(split_version_id IS NOT NULL) AS with_a_split,
               COUNTIF(resolved_split_version_id IS NOT NULL) AS with_a_diagnosed_split
        FROM `{DISPOSITION}` WHERE attribution_status = 'MATCH_RISK_POLICY'
    """)
    assert r["held"] == POLICY.expected_held_listens == 35_007
    assert r["still_matched"] == r["held"], "a held listen must not be downgraded to unmatched"
    assert r["payable"] == 0
    assert r["with_a_rate"] == 0
    assert r["with_a_split"] == 0
    # The diagnosed value is retained under its own name: 9,968 of these share a recording-day with
    # an attributable listen, which is how the leak that this asserts against arose in the first
    # place.
    assert r["with_a_diagnosed_split"] > 0


@pytest.mark.integration_readonly
def test_rate_card_gap_is_never_imputed(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS gap_listens,
               COUNTIF(rate_per_stream IS NOT NULL) AS imputed,
               COUNTIF(payout_eligible) AS payable,
               COUNT(DISTINCT listen_date) AS distinct_days,
               MIN(listen_date) AS first_day, MAX(listen_date) AS last_day
        FROM `{DISPOSITION}` WHERE attribution_status = 'RATE_CARD_GAP'
    """)
    assert r["gap_listens"] == 3_164_839
    assert r["imputed"] == 0, "a rate appeared for a day the rate card does not cover"
    assert r["payable"] == 0
    assert r["distinct_days"] == 3
    assert str(r["first_day"]) == "2026-06-10"
    assert str(r["last_day"]) == "2026-06-12"


@pytest.mark.integration_readonly
def test_defective_ownership_never_pays(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS listens, COUNTIF(payout_eligible) AS payable,
               COUNTIF(split_version_id IS NOT NULL) AS with_a_split,
               COUNT(DISTINCT ownership_status) AS causes
        FROM `{DISPOSITION}` WHERE attribution_status = 'DEFECTIVE_OWNERSHIP'
    """)
    assert r["listens"] == 11_796
    assert r["payable"] == 0
    assert r["with_a_split"] == 0
    assert r["causes"] >= 1


@pytest.mark.integration_readonly
def test_only_attributable_recordings_reach_the_fact(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS fact_rows_without_an_attributable_listen
        FROM (
          SELECT DISTINCT f.recording_mbid, f.split_version_id
          FROM `{FACT}` f
          WHERE f.attribution_run_id = '{PUBLISHED_RUN}'
            AND NOT EXISTS (SELECT 1 FROM `{DISPOSITION}` d
                            WHERE d.recording_mbid = f.recording_mbid
                              AND d.split_version_id = f.split_version_id
                              AND d.attribution_status = 'ATTRIBUTABLE'))
    """)
    assert r["fact_rows_without_an_attributable_listen"] == 0


# --- money types and amounts ---------------------------------------------------------------

@pytest.mark.integration_readonly
def test_no_monetary_column_is_float(bq):
    types = {r["column_name"]: r["data_type"] for r in rows(bq, f"""
        SELECT column_name, data_type FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'fct_royalty_attribution'
    """)}
    for col in MONEY_COLUMNS:
        assert col in types, col
        assert types[col].startswith("NUMERIC"), (col, types[col])
    assert not any(t.startswith("FLOAT") for c, t in types.items() if c in MONEY_COLUMNS)


@pytest.mark.integration_readonly
def test_no_payout_is_negative_and_gross_reconciles(bq):
    r = one(bq, f"""
        SELECT COUNTIF(holder_payout < NUMERIC '0') AS negative_payouts,
               COUNTIF(gross_royalty < NUMERIC '0') AS negative_gross,
               COUNTIF(gross_royalty != ROUND(attributable_streams * rate_per_stream, 2))
                 AS gross_mismatch,
               COUNTIF(holder_share_pct <= NUMERIC '0'
                       OR holder_share_pct > NUMERIC '100') AS bad_share
        FROM `{FACT}` WHERE attribution_run_id = '{PUBLISHED_RUN}'
    """)
    assert r["negative_payouts"] == 0
    assert r["negative_gross"] == 0
    assert r["gross_mismatch"] == 0
    assert r["bad_share"] == 0


@pytest.mark.integration_readonly
def test_every_group_closes_exactly_in_cents(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS group_count, COUNTIF(summed != gross) AS groups_that_do_not_close
        FROM (
          SELECT MAX(gross_royalty) AS gross, SUM(holder_payout) AS summed
          FROM `{FACT}` WHERE attribution_run_id = '{PUBLISHED_RUN}'
          GROUP BY period, recording_mbid, split_version_id, rate_card_id)
    """)
    assert r["group_count"] > 3_000_000
    assert r["groups_that_do_not_close"] == 0


# --- parity: recompute the published allocation in Python ---------------------------------

@pytest.mark.integration_readonly
def test_published_allocation_matches_the_python_implementation(bq):
    """The strongest check in this file: take published groups, recompute the cent allocation in
    Python from the published gross and shares, and require agreement on every holder."""
    sample = rows(bq, f"""
        SELECT recording_mbid, split_version_id, rate_card_id,
               ANY_VALUE(gross_royalty_unrounded) AS gross_unrounded,
               ANY_VALUE(gross_royalty) AS gross_published,
               ARRAY_AGG(STRUCT(rights_holder_id, holder_share_pct, holder_payout)
                         ORDER BY rights_holder_id) AS holders
        FROM `{FACT}`
        WHERE attribution_run_id = '{PUBLISHED_RUN}'
          AND MOD(ABS(FARM_FINGERPRINT(recording_mbid)), 3000) = 11
        GROUP BY recording_mbid, split_version_id, rate_card_id
        LIMIT 400
    """)
    assert len(sample) >= 50, f"sample too small to prove anything: {len(sample)}"
    multi_holder = 0
    for group in sample:
        holders = [(h["rights_holder_id"], Decimal(h["holder_share_pct"]))
                   for h in group["holders"]]
        expected = {a.rights_holder_id: a.published
                    for a in allocate(Decimal(group["gross_unrounded"]), holders)}
        published = {h["rights_holder_id"]: Decimal(h["holder_payout"])
                     for h in group["holders"]}
        assert expected == published, (group["recording_mbid"], expected, published)
        assert sum(published.values()) == Decimal(group["gross_published"])
        multi_holder += len(holders) > 1
    assert multi_holder > 0, "the sample contained no multi-holder group, so nothing was allocated"


@pytest.mark.integration_readonly
def test_the_remainder_tiebreak_is_deterministic_on_real_ties(bq):
    """Find published groups where two holders had the SAME discarded fraction and one cent to
    give, and confirm the lower rights_holder_id received it."""
    ties = rows(bq, f"""
        WITH g AS (
          SELECT recording_mbid, split_version_id, rate_card_id, remainder_cents,
                 ARRAY_AGG(STRUCT(rights_holder_id, remainder_fraction, remainder_rank,
                                  floor_cents, holder_payout)
                           ORDER BY remainder_fraction DESC, rights_holder_id) AS holders,
                 COUNT(DISTINCT remainder_fraction) AS distinct_fractions,
                 COUNT(*) AS holder_count
          FROM `{FACT}` WHERE attribution_run_id = '{PUBLISHED_RUN}'
          GROUP BY recording_mbid, split_version_id, rate_card_id, remainder_cents
        )
        SELECT * FROM g
        WHERE distinct_fractions < holder_count AND remainder_cents BETWEEN 1 AND holder_count - 1
        LIMIT 50
    """)
    if not ties:
        pytest.skip("no real remainder tie in the published data; covered by unit fixtures")
    for group in ties:
        tied_value = None
        winners, losers = [], []
        for h in group["holders"]:
            if h["remainder_rank"] <= group["remainder_cents"]:
                winners.append((h["rights_holder_id"], Decimal(h["remainder_fraction"])))
            else:
                losers.append((h["rights_holder_id"], Decimal(h["remainder_fraction"])))
        for wid, wfrac in winners:
            for lid, lfrac in losers:
                if wfrac == lfrac:
                    tied_value = wfrac
                    assert wid < lid, (
                        f"tie at fraction {wfrac} went to {wid} over {lid}: the tiebreak must "
                        f"prefer the lower rights_holder_id")
        assert tied_value is None or tied_value >= 0


# --- immutability and the current pointer -------------------------------------------------

@pytest.mark.integration_readonly
def test_two_publications_coexist_and_both_remain_queryable(bq):
    published = {r["attribution_run_id"]: r for r in rows(bq, f"""
        SELECT attribution_run_id, publication_status, COUNT(*) AS holder_rows,
               SUM(holder_payout) AS total_payout
        FROM `{FACT}` GROUP BY attribution_run_id, publication_status
    """)}
    assert PUBLISHED_RUN in published
    assert REHEARSAL_RUN in published, "the second publication is gone"
    assert published[PUBLISHED_RUN]["publication_status"] == "PUBLISHED"
    assert published[REHEARSAL_RUN]["publication_status"] == "REHEARSAL"
    # Same inputs, same amounts: the rehearsal exists to prove the mechanics, not to restate money.
    assert published[PUBLISHED_RUN]["holder_rows"] == published[REHEARSAL_RUN]["holder_rows"]
    assert (Decimal(published[PUBLISHED_RUN]["total_payout"])
            == Decimal(published[REHEARSAL_RUN]["total_payout"]))


@pytest.mark.integration_readonly
def test_the_current_pointer_selects_one_publication_without_deleting_the_other(bq):
    pointer = one(bq, f"""
        SELECT attribution_run_id, payout_policy_version
        FROM `{PROJECT}.{DATASET}.publication_pointer` WHERE pointer_name = 'CURRENT'
    """)
    assert pointer["attribution_run_id"] == PUBLISHED_RUN
    assert pointer["payout_policy_version"] == POLICY.version

    current = one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT attribution_run_id) AS runs,
               MIN(publication_status) AS status
        FROM `{PROJECT}.{DATASET}.fct_royalty_attribution_current`
    """)
    assert current["runs"] == 1
    assert current["status"] == "PUBLISHED"
    assert current["n"] == 5_925_913


@pytest.mark.integration_readonly
def test_the_publication_carries_the_policy_and_run_that_produced_it(bq):
    r = one(bq, f"""
        SELECT COUNT(DISTINCT payout_policy_version) AS policies,
               MIN(payout_policy_version) AS policy,
               COUNT(DISTINCT rule_version_id) AS rule_versions,
               COUNT(DISTINCT period) AS periods
        FROM `{FACT}` WHERE attribution_run_id = '{PUBLISHED_RUN}'
    """)
    assert r["policies"] == 1
    assert r["policy"] == POLICY.version
    assert r["rule_versions"] == 1
    assert r["periods"] == 1


@pytest.mark.integration_readonly
def test_the_frozen_matcher_is_still_untouched(bq):
    """Phase 5B must not have moved anything upstream of itself."""
    r = one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT match_run_id) AS runs, MIN(match_run_id) AS run_id,
               MIN(scoring_version) AS scoring_version
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """)
    assert r["n"] == LISTENS
    assert r["runs"] == 1
    assert r["run_id"] == POLICY.match_run_id == "match:101eef5c5b5c081e"
    assert r["scoring_version"] == POLICY.scoring_version == "1.0.0+cb21f9704ff0"


# --- the split applied is the one valid on the listen date --------------------------------

@pytest.mark.integration_readonly
def test_a_mid_june_ownership_change_splits_the_money_between_two_holder_sets(bq):
    """A recording whose ownership changed on 2026-06-15 must appear as two fact groups with
    disjoint holder sets, not one group under the latest owners."""
    changed = rows(bq, f"""
        WITH two_sets AS (
          SELECT recording_mbid
          FROM `{FACT}` WHERE attribution_run_id = '{PUBLISHED_RUN}'
          GROUP BY recording_mbid
          HAVING COUNT(DISTINCT split_version_id) = 2
          LIMIT 25
        )
        SELECT f.recording_mbid, f.split_version_id,
               STRING_AGG(DISTINCT f.rights_holder_id ORDER BY f.rights_holder_id) AS holders,
               SUM(f.holder_payout) AS paid
        FROM `{FACT}` f JOIN two_sets USING (recording_mbid)
        WHERE f.attribution_run_id = '{PUBLISHED_RUN}'
        GROUP BY f.recording_mbid, f.split_version_id
    """)
    assert changed, "no recording in the publication has two ownership sets"
    by_recording = defaultdict(list)
    for row in changed:
        by_recording[row["recording_mbid"]].append(row)
    proved = 0
    for recording, groups in by_recording.items():
        if len(groups) != 2:
            continue
        assert groups[0]["holders"] != groups[1]["holders"], (
            f"{recording} paid the same holders under two split sets: the temporal join collapsed")
        proved += 1
    assert proved > 0
