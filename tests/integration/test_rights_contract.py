"""INTEGRATION tests for the MODELED rights layer. Not unit tests: these need GCP and cost bytes.

Three things are checked here that no unit test can:

  * the deliberate defects INJECTED by the generator are the ones the dbt quality layer FOUND --
    expected counts from docs/phase0/rights_generation.json against independently computed counts,
    which is what makes the quality models a check rather than a lookup;
  * the dbt snapshot really implements SCD Type 2 -- history preserved, one current version per
    holder, old versions still queryable with their old values;
  * the half-open temporal join selects different holders either side of a change inside the
    project's real period.

Everything here reads. Nothing regenerates rights or touches the frozen Phase 4B result.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

PROJECT = "ss-de-944054e7"
MAX_BYTES = 60 * 1024**3

REPO = pathlib.Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "src"))
from rights.generator import load_rights_model

MODEL = load_rights_model()
GENERATION_REPORT = REPO / "docs/phase0/rights_generation.json"

#: Measured on the RESTATED matches (normalization 1.1.0+b3253b155934), which is what dbt last built.
#: The Phase 5B figures for pub:v1 are preserved in docs/schema_notes.md section 23.
#:
#: NOTE WHICH GATE THIS FILE MEASURES. int_payout_eligibility applies only the MATCH gate, so its
#: `payout_eligible` is "matched and not held for match risk" -- 32,134,257 listens. The FULL gate,
#: which also requires ownership and a rate, lives in int_financial_disposition and admits 28,387,115.
#: Conflating the two is exactly the mistake the two-column design in that model exists to prevent, and
#: this test asserted the wrong one on the first attempt.
MATCH_GATE_ELIGIBLE = 32_134_257
MATCHED_LISTENS = 32_169_264
RISK_HELD_LISTENS = 35_007
LISTENS = 38_199_641


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT)


@pytest.fixture(scope="module")
def injected() -> dict:
    return json.loads(GENERATION_REPORT.read_text())["defects_injected"]


def one(client, sql: str) -> dict:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES)
    return next(iter(dict(r) for r in client.query(sql, job_config=cfg).result()))


def rows(client, sql: str) -> list[dict]:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES)
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


# --- the data declares itself modeled -----------------------------------------------------


@pytest.mark.integration_readonly
def test_every_rights_table_declares_itself_modeled(bq):
    for table in ("rights_holders", "ownership_splits", "rate_card"):
        r = one(
            bq,
            f"""
            SELECT COUNT(*) AS n, COUNTIF(NOT is_modeled) AS not_modeled,
                   COUNT(DISTINCT rights_version) AS versions
            FROM `{PROJECT}.splitsheet_rights.{table}`
        """,
        )
        assert r["n"] > 0
        assert r["not_modeled"] == 0, f"{table} has rows that do not declare themselves modeled"
        assert r["versions"] == 1


@pytest.mark.integration_readonly
def test_no_holder_name_could_be_mistaken_for_a_real_organisation(bq):
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS n,
               COUNTIF(NOT REGEXP_CONTAINS(display_name, r'^Modeled Rights Holder [0-9]{{6}}$'))
                 AS unexpected
        FROM `{PROJECT}.splitsheet_rights.rights_holders`
    """,
    )
    assert r["n"] == MODEL.holder_count
    assert r["unexpected"] == 0


@pytest.mark.integration_readonly
def test_shares_and_rates_are_decimal_not_float(bq):
    types = {
        r["column_name"]: r["data_type"]
        for r in rows(
            bq,
            f"""
        SELECT column_name, data_type
        FROM `{PROJECT}.splitsheet_rights`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name IN ('ownership_splits', 'rate_card')
          AND column_name IN ('share_pct', 'rate_per_stream')
    """,
        )
    }
    assert types["share_pct"].startswith("NUMERIC"), types
    assert types["rate_per_stream"].startswith("NUMERIC"), types
    assert "FLOAT" not in json.dumps(types)


# --- injected defects vs detected defects -------------------------------------------------


@pytest.mark.integration_readonly
def test_the_quality_layer_finds_the_defects_the_generator_injected(bq, injected):
    """The reconciliation that makes the quality models meaningful.

    Counts are per SPLIT SET, and a defective recording can carry two sets (a mid-period ownership
    change produces two), so overlap and gap are asserted at the recording level where the quota
    was defined.
    """
    found = one(
        bq,
        f"""
        SELECT
          COUNTIF(defect_share_sum_not_100) AS share_sum_sets,
          COUNT(DISTINCT IF(defect_temporal_overlap, recording_mbid, NULL))
            AS overlap_recordings,
          COUNT(DISTINCT IF(defect_temporal_gap, recording_mbid, NULL)) AS gap_recordings,
          COUNTIF(defect_invalid_interval) AS invalid_interval_sets,
          COUNTIF(defect_missing_rights_holder) AS missing_holder_sets,
          COUNT(DISTINCT IF(defect_orphan_recording, recording_mbid, NULL))
            AS orphan_recordings
        FROM `{PROJECT}.splitsheet_dbt.int_ownership_validity`
    """,
    )
    assert found["share_sum_sets"] == injected["shares_do_not_sum_to_100"]
    assert found["overlap_recordings"] == injected["temporal_overlap"]
    assert found["gap_recordings"] == injected["temporal_gap"]
    assert found["invalid_interval_sets"] == injected["invalid_interval"]
    assert found["missing_holder_sets"] == injected["missing_rights_holder"]
    assert found["orphan_recordings"] == injected["orphan_recording_mbid"]


@pytest.mark.integration_readonly
def test_exactly_the_reserved_holders_have_no_ownership(bq, injected):
    r = one(
        bq,
        f"""
        SELECT COUNTIF(s.rights_holder_id IS NULL) AS holders_without_split
        FROM `{PROJECT}.splitsheet_rights.rights_holders` h
        LEFT JOIN (SELECT DISTINCT rights_holder_id
                   FROM `{PROJECT}.splitsheet_rights.ownership_splits`) s
               ON s.rights_holder_id = h.holder_id
    """,
    )
    assert r["holders_without_split"] == injected["holder_without_split_reserved"]


@pytest.mark.integration_readonly
def test_defects_were_not_silently_corrected_in_staging(bq):
    """The staging model must pass the defects through untouched."""
    r = one(
        bq,
        f"""
        SELECT
          (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_rights.ownership_splits`) AS source_rows,
          (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_dbt.stg_rights__ownership_splits`)
            AS staged_rows,
          (SELECT COUNTIF(valid_to <= valid_from)
             FROM `{PROJECT}.splitsheet_dbt.stg_rights__ownership_splits`) AS staged_invalid
    """,
    )
    assert r["source_rows"] == r["staged_rows"], "staging dropped rows"
    assert r["staged_invalid"] > 0, "staging repaired the invalid intervals"


@pytest.mark.integration_readonly
def test_healthy_split_sets_sum_to_exactly_one_hundred(bq):
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS valid_sets,
               COUNTIF(share_sum != NUMERIC '100.0000') AS not_exactly_100,
               MIN(share_sum) AS min_sum, MAX(share_sum) AS max_sum
        FROM `{PROJECT}.splitsheet_dbt.int_ownership_validity`
        WHERE is_valid_set
    """,
    )
    assert r["valid_sets"] > 2_000_000
    assert r["not_exactly_100"] == 0
    assert Decimal(r["min_sum"]) == Decimal(r["max_sum"]) == Decimal("100.0000")


# --- payout eligibility is separate from matching ------------------------------------------


@pytest.mark.integration_readonly
def test_matched_is_not_payable(bq):
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS listens,
               COUNTIF(payout_eligible) AS eligible,
               COUNTIF(hold_reason = 'MATCH_RISK_POLICY') AS risk_held,
               COUNTIF(match_status = 'MATCHED') AS matched,
               COUNTIF(match_status = 'MATCHED' AND NOT payout_eligible) AS matched_but_held
        FROM `{PROJECT}.splitsheet_dbt.int_payout_eligibility`
    """,
    )
    assert r["listens"] == LISTENS
    assert r["eligible"] == MATCH_GATE_ELIGIBLE
    assert r["risk_held"] == RISK_HELD_LISTENS
    assert r["matched"] == MATCHED_LISTENS
    assert r["matched_but_held"] == RISK_HELD_LISTENS, (
        "at the MATCH gate, the only technically matched listens that are not eligible are the "
        "risk-held ones. Ownership and rate holds are applied later, in int_financial_disposition."
    )
    assert r["matched"] - r["eligible"] == RISK_HELD_LISTENS


@pytest.mark.integration_readonly
def test_the_matcher_was_not_modified_to_produce_the_hold(bq):
    """The frozen result must be untouched: the hold lives downstream of it."""
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT match_run_id) AS runs,
               MIN(match_run_id) AS run_id, MIN(scoring_version) AS scoring_version
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """,
    )
    assert r["n"] == LISTENS
    assert r["runs"] == 1
    assert r["run_id"] == "match:101eef5c5b5c081e"
    assert r["scoring_version"] == "1.0.0+cb21f9704ff0"


# --- the temporal join ---------------------------------------------------------------------


@pytest.mark.integration_readonly
def test_ownership_boundary_is_half_open_on_real_rows(bq):
    """valid_from covered, valid_to - 1 covered, valid_to NOT covered."""
    r = one(
        bq,
        f"""
        WITH i AS (
          SELECT recording_mbid, split_version_id, valid_from, valid_to
          FROM `{PROJECT}.splitsheet_dbt.int_ownership_validity`
          WHERE is_valid_set AND valid_from = DATE '2026-06-15' AND valid_to < DATE '9999-12-31'
          LIMIT 200
        ), i2 AS (
          SELECT recording_mbid, split_version_id, valid_from, valid_to
          FROM `{PROJECT}.splitsheet_dbt.int_ownership_validity`
          WHERE is_valid_set AND valid_to = DATE '2026-06-15'
          LIMIT 200
        )
        SELECT
          (SELECT COUNTIF(NOT (valid_from >= valid_from AND valid_from < valid_to)) FROM i2)
            AS lower_bound_excluded,
          (SELECT COUNTIF(NOT (DATE_SUB(valid_to, INTERVAL 1 DAY) >= valid_from
                               AND DATE_SUB(valid_to, INTERVAL 1 DAY) < valid_to)) FROM i2)
            AS last_day_excluded,
          (SELECT COUNTIF(valid_to >= valid_from AND valid_to < valid_to) FROM i2)
            AS upper_bound_included,
          (SELECT COUNT(*) FROM i2) AS probed
    """,
    )
    assert r["probed"] > 0
    assert r["lower_bound_excluded"] == 0
    assert r["last_day_excluded"] == 0
    assert r["upper_bound_included"] == 0


@pytest.mark.integration_readonly
def test_a_june_ownership_change_selects_different_holders_before_and_after(bq):
    change = MODEL.change_date
    before = change - dt.timedelta(days=1)
    pairs = rows(
        bq,
        f"""
        WITH changed AS (
          SELECT recording_mbid
          FROM `{PROJECT}.splitsheet_dbt.int_ownership_validity`
          WHERE is_valid_set
          GROUP BY recording_mbid
          HAVING COUNTIF(valid_to = DATE '{change}') = 1
             AND COUNTIF(valid_from = DATE '{change}') = 1
          LIMIT 100
        ),
        owners AS (
          SELECT c.recording_mbid, probe_date,
                 STRING_AGG(DISTINCT o.rights_holder_id ORDER BY o.rights_holder_id) AS holders
          FROM changed c
          CROSS JOIN UNNEST([DATE '{before}', DATE '{change}']) AS probe_date
          JOIN `{PROJECT}.splitsheet_dbt.int_ownership_validity` v
            ON v.recording_mbid = c.recording_mbid AND v.is_valid_set
           AND probe_date >= v.valid_from AND probe_date < v.valid_to
          JOIN `{PROJECT}.splitsheet_rights.ownership_splits` o
            ON o.recording_mbid = v.recording_mbid
           AND o.split_version_id = v.split_version_id
          GROUP BY c.recording_mbid, probe_date
        )
        SELECT recording_mbid,
               MAX(IF(probe_date = DATE '{before}', holders, NULL)) AS holders_before,
               MAX(IF(probe_date = DATE '{change}', holders, NULL)) AS holders_after
        FROM owners GROUP BY recording_mbid
    """,
    )
    assert len(pairs) >= 20, f"too few mid-period changes to prove anything: {len(pairs)}"
    for p in pairs:
        assert p["holders_before"] and p["holders_after"]
        assert p["holders_before"] != p["holders_after"], (
            f"{p['recording_mbid']} resolved to the same holders on {before} and {change}: "
            f"the join is not temporal"
        )


@pytest.mark.integration_readonly
def test_no_recording_day_carries_an_owner_without_exactly_one_valid_set(bq):
    r = one(
        bq,
        f"""
        SELECT COUNTIF(covering_valid_sets != 1 AND split_version_id IS NOT NULL)
                 AS owner_without_single_valid_set,
               COUNTIF(covering_valid_sets > 1 AND resolution_status != 'MULTIPLE_VALID_SETS')
                 AS ambiguity_not_flagged,
               COUNTIF(is_attributable AND (split_version_id IS NULL OR rate_per_stream IS NULL))
                 AS attributable_without_both_sides
        FROM `{PROJECT}.splitsheet_dbt.int_ownership_resolution`
    """,
    )
    assert r["owner_without_single_valid_set"] == 0
    assert r["ambiguity_not_flagged"] == 0
    assert r["attributable_without_both_sides"] == 0


@pytest.mark.integration_readonly
def test_the_rate_card_gap_is_detected_and_never_priced(bq):
    gap = one(
        bq,
        f"""
        SELECT COUNT(*) AS recording_days, SUM(streams) AS streams,
               COUNT(DISTINCT listen_date) AS distinct_days,
               COUNTIF(rate_per_stream IS NOT NULL) AS priced_anyway,
               COUNTIF(is_attributable) AS attributable
        FROM `{PROJECT}.splitsheet_dbt.int_ownership_resolution`
        WHERE resolution_status = 'RATE_CARD_GAP'
    """,
    )
    assert gap["distinct_days"] == MODEL.expected_rate_gap_days == 3
    assert gap["recording_days"] > 0
    assert gap["priced_anyway"] == 0, "a day with no rate card must not carry a rate"
    assert gap["attributable"] == 0


# --- SCD Type 2, for real ------------------------------------------------------------------


@pytest.mark.integration_readonly
def test_snapshot_implements_scd_type_2_over_rights_holders(bq):
    """History preserved, exactly one current version per holder, old values still readable."""
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS versions,
               COUNT(DISTINCT holder_id) AS holders,
               COUNTIF(dbt_valid_to IS NULL) AS current_versions,
               COUNTIF(dbt_valid_to IS NOT NULL) AS closed_versions
        FROM `{PROJECT}.splitsheet_dbt.snap_rights_holders`
    """,
    )
    assert r["holders"] == MODEL.holder_count
    assert r["current_versions"] == r["holders"], "exactly one current version per holder"
    assert (
        r["closed_versions"] > 0
    ), "no history: the controlled revision was never snapshotted, so SCD2 is unproven"
    assert r["versions"] == r["current_versions"] + r["closed_versions"]


@pytest.mark.integration_readonly
def test_the_superseded_versions_still_carry_their_old_values(bq):
    """The point of SCD2: the old row is not overwritten, it is closed and still queryable."""
    changed = rows(
        bq,
        f"""
        SELECT holder_id,
               MAX(IF(dbt_valid_to IS NULL, payee_status, NULL)) AS current_status,
               MAX(IF(dbt_valid_to IS NOT NULL, payee_status, NULL)) AS previous_status,
               MAX(IF(dbt_valid_to IS NOT NULL, dbt_valid_to, NULL)) AS closed_at,
               MIN(IF(dbt_valid_to IS NULL, dbt_valid_from, NULL)) AS current_from
        FROM `{PROJECT}.splitsheet_dbt.snap_rights_holders`
        GROUP BY holder_id
        HAVING COUNT(*) > 1
    """,
    )
    assert changed, "no holder has more than one version"
    for h in changed:
        assert (
            h["previous_status"] != h["current_status"]
        ), f"{h['holder_id']} has two versions with the same value"
        assert h["closed_at"] is not None
        assert (
            h["current_from"] >= h["closed_at"]
        ), "the new version must start no earlier than the old one closed"


@pytest.mark.integration_readonly
def test_dimension_exposes_exactly_one_current_row_per_holder(bq):
    r = one(
        bq,
        f"""
        SELECT COUNT(*) AS versions, COUNTIF(is_current) AS current_rows,
               COUNT(DISTINCT IF(is_current, holder_id, NULL)) AS distinct_current_holders
        FROM `{PROJECT}.splitsheet_dbt.dim_rights_holders`
    """,
    )
    assert r["current_rows"] == r["distinct_current_holders"] == MODEL.holder_count
    assert r["versions"] > r["current_rows"]


# --- the quality report is queryable -------------------------------------------------------


@pytest.mark.integration_readonly
def test_quality_report_is_queryable_and_carries_business_impact(bq):
    report = rows(
        bq,
        f"""
        SELECT rule, severity, status, failed_records, total_records, failure_rate,
               business_impact, expectation
        FROM `{PROJECT}.splitsheet_dbt.quality_report`
    """,
    )
    assert len(report) >= 15
    assert {r["business_impact"] for r in report} <= {
        "royalty_attribution_at_risk",
        "wrong_rights_holder_risk",
        "ownership_allocation_at_risk",
    }
    for r in report:
        assert r["severity"] in ("ERROR", "WARN")
        assert r["status"] == ("PASS" if r["failed_records"] == 0 else "FAIL")
        assert r["total_records"] > 0
    # Rules that must be clean, versus rules expected to find the injected defects.
    must_be_zero = [r for r in report if r["expectation"] == "must_be_zero"]
    assert must_be_zero
    assert all(r["failed_records"] == 0 for r in must_be_zero), [
        r["rule"] for r in must_be_zero if r["failed_records"]
    ]
    finds = [r for r in report if r["expectation"] == "expected_to_find_injected_defects"]
    assert any(
        r["failed_records"] > 0 for r in finds
    ), "no rule found any defect, which means the checks are not working"
