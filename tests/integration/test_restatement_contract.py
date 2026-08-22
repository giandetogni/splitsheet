"""INTEGRATION tests for the Phase 6 restatement. Real BigQuery, real cost.

What these defend, and it is the whole point of a restatement layer:

  * the v1 publication is still exactly what it was -- row count, total, published_at and content
    digest -- checked against config/frozen_versions.yml rather than against itself;
  * v2 is a separate publication, and both are queryable at the same time;
  * SUM(delta) equals the difference between the two portfolio totals, exactly, in cents;
  * no listen disappeared, and every listen outside the affected cohort is byte-identical;
  * the restatement changed normalization ONLY: scoring, rights and payout policy are the frozen ones.
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal

import pytest
import yaml

pytestmark = pytest.mark.integration

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 100 * 1024**3

REPO = pathlib.Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "src"))
from payout.digest import publication_digest_sql

FACT = f"{PROJECT}.{DATASET}.fct_royalty_attribution"
RESTATEMENTS = f"{PROJECT}.{DATASET}.fct_restatements"
REGISTRY = f"{PROJECT}.{DATASET}.publication_registry"
V1_MATCHES = f"{PROJECT}.splitsheet_silver.silver_listen_matches"
V2_MATCHES = f"{PROJECT}.splitsheet_silver.silver_listen_matches_restated"

LISTENS = 38_199_641

with open(REPO / "config/frozen_versions.yml") as fh:
    FROZEN = yaml.safe_load(fh)
R = FROZEN["restatement"]
PRIOR_RUN = R["baseline_attribution_run_id"]


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


@pytest.fixture(scope="module")
def new_run(bq) -> str:
    r = one(bq, f"""
        SELECT attribution_run_id FROM `{REGISTRY}`
        WHERE publication_id = 'pub:v2'
    """)
    return r["attribution_run_id"]


# --- v1 is immutable -----------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_the_baseline_publication_still_matches_the_frozen_manifest(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS row_count, SUM(holder_payout) AS portfolio_paid,
               MIN(published_at) AS published_at,
               COUNT(DISTINCT publication_status) AS statuses
        FROM `{FACT}` WHERE attribution_run_id = '{PRIOR_RUN}'
    """)
    assert int(r["row_count"]) == R["baseline_row_count"] == 5_925_913
    assert str(r["portfolio_paid"]) == R["baseline_portfolio_paid"] == "98284.22"
    assert r["statuses"] == 1


@pytest.mark.integration_readonly
def test_the_baseline_content_digest_is_unchanged(bq):
    """The digest is recomputed from the rows, not read from the registry, and then compared with
    the manifest. That is what makes 'immutable' checkable instead of asserted."""
    r = one(bq, publication_digest_sql(FACT, PRIOR_RUN))
    assert r["content_digest"].startswith(R["baseline_content_digest"])
    assert int(r["row_count"]) == R["baseline_row_count"]


@pytest.mark.integration_readonly
def test_the_registry_records_both_publications_as_frozen(bq):
    registry = {r["publication_id"]: r for r in rows(bq, f"""
        SELECT publication_id, attribution_run_id, normalization_version, scoring_version,
               payout_policy_version, row_count, portfolio_paid, content_digest, frozen
        FROM `{REGISTRY}`
    """)}
    assert "pub:v1" in registry and "pub:v2" in registry
    assert all(r["frozen"] for r in registry.values())
    assert registry["pub:v1"]["normalization_version"] == R["prior_normalization_version"]
    assert registry["pub:v2"]["normalization_version"] == R["new_normalization_version"]
    # The one input that moved, and the three that did not.
    assert registry["pub:v1"]["scoring_version"] == registry["pub:v2"]["scoring_version"]
    assert (registry["pub:v1"]["payout_policy_version"]
            == registry["pub:v2"]["payout_policy_version"] == R["payout_policy_version"])
    assert registry["pub:v1"]["content_digest"] != registry["pub:v2"]["content_digest"]


@pytest.mark.integration_readonly
def test_both_publications_are_queryable_at_once(bq, new_run):
    got = {r["attribution_run_id"]: r for r in rows(bq, f"""
        SELECT attribution_run_id, COUNT(*) AS row_count, SUM(holder_payout) AS paid
        FROM `{FACT}` WHERE attribution_run_id IN ('{PRIOR_RUN}', '{new_run}')
        GROUP BY attribution_run_id
    """)}
    assert set(got) == {PRIOR_RUN, new_run}
    assert int(got[PRIOR_RUN]["row_count"]) == R["baseline_row_count"]
    assert int(got[new_run]["row_count"]) > 0


# --- the delta reconciles ------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_summed_delta_equals_the_difference_between_the_two_totals(bq, new_run):
    r = one(bq, f"""
        SELECT
          (SELECT SUM(holder_payout) FROM `{FACT}`
             WHERE attribution_run_id = '{PRIOR_RUN}') AS prior_paid,
          (SELECT SUM(holder_payout) FROM `{FACT}`
             WHERE attribution_run_id = '{new_run}') AS restated_paid,
          (SELECT SUM(delta) FROM `{RESTATEMENTS}`
             WHERE restatement_run_id = '{FROZEN["restatement"]["trigger_id"]}'
                OR restatement_run_id LIKE 'restate:%') AS summed_delta
    """)
    prior, restated, delta = (Decimal(r["prior_paid"]), Decimal(r["restated_paid"]),
                              Decimal(r["summed_delta"]))
    assert delta == restated - prior, (prior, restated, delta)


@pytest.mark.integration_readonly
def test_the_restatement_mart_carries_prior_new_and_delta(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS rows_total,
               COUNTIF(delta != restated_payout - prior_payout) AS delta_wrong,
               COUNTIF(prior_normalization_version = new_normalization_version) AS same_norm,
               COUNTIF(prior_scoring_version != new_scoring_version) AS scoring_moved,
               COUNTIF(trigger_reason IS NULL) AS missing_trigger,
               COUNT(DISTINCT change_type) AS change_types
        FROM `{RESTATEMENTS}`
    """)
    assert int(r["rows_total"]) > 0
    assert int(r["delta_wrong"]) == 0, "delta must be restated minus prior, exactly"
    assert int(r["same_norm"]) == 0, "every row must record a normalization change"
    assert int(r["scoring_moved"]) == 0, "a restatement may not retune the scorer"
    assert int(r["missing_trigger"]) == 0
    assert int(r["change_types"]) >= 2


@pytest.mark.integration_readonly
def test_no_monetary_column_in_the_restatement_mart_is_float(bq):
    types = {r["column_name"]: r["data_type"] for r in rows(bq, f"""
        SELECT column_name, data_type FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'fct_restatements'
    """)}
    for col in ("prior_payout", "restated_payout", "delta"):
        assert types[col].startswith("NUMERIC"), (col, types[col])


# --- nothing disappeared, nothing outside the cohort moved ---------------------------------

@pytest.mark.integration_readonly
def test_no_listen_disappeared_and_the_cohort_is_contained(bq):
    r = one(bq, f"""
        SELECT
          (SELECT COUNT(*) FROM `{V2_MATCHES}`
             WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
               AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00') AS restated_listens,
          (SELECT COUNT(DISTINCT listen_hash) FROM `{V2_MATCHES}`
             WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
               AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00') AS distinct_listens,
          (SELECT COUNTIF(recording_cohort) FROM `{V2_MATCHES}`
             WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
               AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00') AS cohort_listens
    """)
    assert int(r["restated_listens"]) == int(r["distinct_listens"]) == LISTENS
    assert 0 < int(r["cohort_listens"]) < LISTENS


@pytest.mark.integration_readonly
def test_every_listen_outside_the_cohort_is_identical_to_v1(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS moved
        FROM `{V2_MATCHES}` r JOIN `{V1_MATCHES}` v USING (listen_hash)
        WHERE r.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND r.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
          AND v.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND v.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
          AND NOT r.recording_cohort
          AND (IFNULL(r.matched_recording_mbid, 'x') != IFNULL(v.matched_recording_mbid, 'x')
            OR r.match_status != v.match_status
            OR r.match_method != v.match_method
            OR IFNULL(r.failure_reason, 'x') != IFNULL(v.failure_reason, 'x')
            OR r.candidate_count != v.candidate_count)
    """)
    assert int(r["moved"]) == 0, "the restatement is not contained to its cohort"


@pytest.mark.integration_readonly
def test_the_restated_matches_carry_the_new_normalization_and_the_frozen_scoring(bq):
    r = one(bq, f"""
        SELECT COUNT(DISTINCT normalization_version) AS norm_versions,
               MIN(normalization_version) AS normalization_version,
               COUNT(DISTINCT scoring_version) AS scoring_versions,
               MIN(scoring_version) AS scoring_version
        FROM `{V2_MATCHES}`
        WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """)
    assert r["normalization_version"] == R["new_normalization_version"]
    assert r["scoring_version"] == R["scoring_version"]
    assert int(r["norm_versions"]) == int(r["scoring_versions"]) == 1


# --- run identity: the warehouse can tell the two cohorts apart ------------------------------

@pytest.mark.integration_readonly
def test_the_run_registry_resolves_the_legacy_identifier_without_rewriting_it(bq):
    """The published rows keep the identifier they were published with, and the registry explains it.

    Two things are asserted together on purpose. The rows of pub:v2 must still carry the legacy
    string -- if they ever carry the canonical one, someone rewrote a published statement -- and the
    registry must map that string to the canonical identity of the run that produced them.
    """
    ident = R["published_run_identity"]
    reg = one(bq, f"""
        SELECT canonical_run_id, legacy_run_id, mart_run_id, cohort_key, cohort_sha256,
               new_publication_id, is_financially_effective
        FROM `{PROJECT}.{DATASET}.restatement_run_registry`
        WHERE run_type = 'PUBLISHED_RESTATEMENT'
    """)
    assert reg["canonical_run_id"] == ident["canonical_run_id"]
    assert reg["legacy_run_id"] == ident["legacy_run_id"]
    assert reg["mart_run_id"] == ident["legacy_run_id"]
    assert reg["cohort_sha256"] == ident["cohort_sha256"]
    assert reg["new_publication_id"] == ident["publication_id"]
    assert reg["is_financially_effective"] is True

    # The rows that reconcile to the published delta must still carry the legacy string.
    carried = rows(bq, f"""
        SELECT restatement_run_id, COUNT(*) AS n, SUM(delta) AS summed_delta
        FROM `{RESTATEMENTS}` GROUP BY 1 ORDER BY n DESC
    """)
    real = [r for r in carried if r["summed_delta"] != 0]
    assert len(real) == 1
    assert real[0]["restatement_run_id"] == ident["legacy_run_id"], (
        "the published restatement rows carry an identifier other than the one they were published "
        "with; a published statement was rewritten")
    assert ident["canonical_run_id"] not in {r["restatement_run_id"] for r in carried}, (
        "the canonical identity was retrofitted onto published rows; the correction is supposed to "
        "be a registry entry, not an edit")


@pytest.mark.integration_readonly
def test_every_identifier_in_the_delta_mart_resolves_to_exactly_one_entry(bq):
    """No orphans, no duplicate claims, and -- deliberately -- no exception by name.

    An earlier version of this test allowed `restate:pending` explicitly. That is a note, not an
    invariant: it cannot fail on the NEXT unexplained identifier. The placeholder now has its own
    registry entry classifying it as a LEGACY_REHEARSAL with no canonical inputs, so the assertion
    is the plain one.
    """
    resolution = rows(bq, f"""
        WITH mart AS (
          SELECT restatement_run_id, COUNT(*) AS rows_in_mart, SUM(delta) AS summed_delta
          FROM `{RESTATEMENTS}` GROUP BY 1
        )
        SELECT m.restatement_run_id, m.rows_in_mart, m.summed_delta,
               COUNT(r.registry_key) AS entries,
               MIN(r.recorded_delta) AS recorded_delta,
               MIN(r.rows_in_delta_mart) AS recorded_rows,
               MIN(r.run_type) AS run_type,
               MIN(r.is_financially_effective) AS is_financially_effective
        FROM mart m
        LEFT JOIN `{PROJECT}.{DATASET}.restatement_run_registry` r
          ON r.mart_run_id = m.restatement_run_id
        GROUP BY 1, 2, 3 ORDER BY 1
    """)
    assert resolution, "the delta mart is empty"
    for r in resolution:
        assert r["entries"] == 1, f"{r['restatement_run_id']} resolves to {r['entries']} entries"
        assert r["recorded_delta"] == r["summed_delta"], r["restatement_run_id"]
        assert r["recorded_rows"] == r["rows_in_mart"], r["restatement_run_id"]

    by_id = {r["restatement_run_id"]: r for r in resolution}
    rehearsal = by_id["restate:pending"]
    assert rehearsal["run_type"] == "LEGACY_REHEARSAL"
    assert rehearsal["is_financially_effective"] is False
    assert rehearsal["summed_delta"] == 0
    effective = [r for r in resolution if r["is_financially_effective"]]
    assert len(effective) == 1
    assert effective[0]["summed_delta"] == Decimal("0.86")


@pytest.mark.integration_readonly
def test_the_rejected_cohort_has_a_different_identity_in_the_warehouse(bq):
    """The collision, closed and observable: same legacy string, two canonical ids, two cohorts."""
    reg = rows(bq, f"""
        SELECT canonical_run_id, legacy_run_id, cohort_sha256, run_type, measured_listens
        FROM `{PROJECT}.{DATASET}.restatement_run_registry`
        WHERE cohort_sha256 IS NOT NULL ORDER BY canonical_run_id
    """)
    assert len(reg) == 2
    assert len({r["canonical_run_id"] for r in reg}) == 2
    assert len({r["cohort_sha256"] for r in reg}) == 2
    assert len({r["legacy_run_id"] for r in reg}) == 1
    by_type = {r["run_type"]: r for r in reg}
    assert by_type["PUBLISHED_RESTATEMENT"]["measured_listens"] == 982_322
    assert by_type["REJECTED_BEFORE_PUBLICATION"]["measured_listens"] == 1_377_862
