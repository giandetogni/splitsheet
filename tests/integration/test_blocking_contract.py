"""INTEGRATION tests for staged blocking. Not unit tests: these need GCP and cost bytes.

The read-only ones assert the invariants the blocking design depends on. The destructive
ones invoke the real builder and are excluded from CI.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT = "ss-de-944054e7"
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
MAX_BYTES = 120 * 1024**3

NORMALIZED = 38_199_641
CANDIDATES = 34_466_312
PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")

SILVER_BANNED = {"user_id", "recording_mbid", "release_mbid", "artist_credit_id",
                 "artist_credit_mbids", "mapper_recording_mbid", "label_available"}


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def one(client, sql: str) -> dict:
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES)
    return next(iter(dict(r) for r in client.query(sql, job_config=cfg).result()))


# --- normalization grain ----------------------------------------------------------------

@pytest.mark.integration_readonly
def test_normalized_has_exactly_one_row_per_listen(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_hash,
               COUNT(DISTINCT normalization_version) AS versions
        FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
        WHERE {PERIOD}
    """)
    assert r["n"] == NORMALIZED
    assert r["distinct_hash"] == NORMALIZED, "listen_hash is not unique in silver"
    assert r["versions"] == 1


@pytest.mark.integration_readonly
def test_silver_carries_no_pii_or_derived_identifier(bq):
    cols = {r["column_name"] for r in bq.query(f"""
        SELECT column_name FROM `{PROJECT}.splitsheet_silver`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'silver_listens_normalized'
    """).result()}
    assert not (cols & SILVER_BANNED), f"banned columns present: {cols & SILVER_BANNED}"


# --- staged precedence ------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_fallback_never_runs_for_a_listen_that_had_exact_candidates(bq):
    """The single most important blocking invariant."""
    r = one(bq, f"""
        SELECT COUNT(*) AS violations FROM (
          SELECT listen_hash
          FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
          GROUP BY listen_hash
          HAVING COUNTIF(block_method = 'EXACT') > 0
             AND COUNTIF(block_method = 'FALLBACK') > 0)
    """)
    assert r["violations"] == 0


@pytest.mark.integration_readonly
def test_candidate_grain_is_unique(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT FORMAT('%t|%t|%t|%t|%t', listen_hash,
                     candidate_recording_mbid, block_method, blocking_version,
                     candidate_run_id)) AS distinct_grain
        FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
    """)
    assert r["n"] == r["distinct_grain"] == CANDIDATES


@pytest.mark.integration_readonly
def test_no_candidate_comes_from_an_empty_or_partial_key(bq):
    """PARTIAL is half a key; using it would collide every unromanisable recording by an
    artist into one bucket. EMPTY would match everything unmatchable."""
    r = one(bq, f"""
        SELECT
          (SELECT COUNTIF(block_key IS NULL OR block_key = '')
             FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`) AS empty_keys,
          (SELECT COUNT(*)
             FROM `{PROJECT}.splitsheet_silver.silver_match_candidates` c
             JOIN `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
               USING (listen_hash)
             WHERE n.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
               AND n.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
               AND ((c.block_method = 'EXACT'    AND n.exact_key_status    != 'AVAILABLE')
                 OR (c.block_method = 'FALLBACK' AND n.fallback_key_status != 'AVAILABLE'))
          ) AS from_unusable_key
    """)
    assert r["empty_keys"] == 0
    assert r["from_unusable_key"] == 0


@pytest.mark.integration_readonly
def test_every_candidate_recording_exists_in_the_canonical_snapshot(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS orphans
        FROM `{PROJECT}.splitsheet_silver.silver_match_candidates` c
        LEFT JOIN `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings` k
          ON k.recording_mbid = c.candidate_recording_mbid
        WHERE k.recording_mbid IS NULL
    """)
    assert r["orphans"] == 0


# --- label blindness --------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_candidate_table_is_label_blind(bq):
    cols = {r["column_name"] for r in bq.query(f"""
        SELECT column_name FROM `{PROJECT}.splitsheet_silver`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'silver_match_candidates'
    """).result()}
    assert not (cols & SILVER_BANNED)


@pytest.mark.integration_readonly
def test_matcher_identity_writes_silver_but_still_cannot_read_eval():
    """Re-proved after the IAM change that granted silver write."""
    import google.auth
    from google.api_core.exceptions import Forbidden
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes)
    c = bigquery.Client(project=PROJECT, credentials=creds)

    # can read its inputs
    assert one(c, f"SELECT COUNT(*) AS n "
                  f"FROM `{PROJECT}.splitsheet_bronze.canonical_blocking_index`")["n"] > 0
    # and is still denied the labels
    with pytest.raises(Forbidden, match="(?i)access denied"):
        c.query(f"SELECT COUNT(*) FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`"
                ).result()


# --- evaluation classes are kept separate -----------------------------------------------

@pytest.mark.integration_readonly
def test_label_absent_from_canonical_is_its_own_class(bq):
    """Blocking cannot propose a candidate that is not in its index, so those listens must
    never be counted as a blocking failure."""
    r = one(bq, f"""
        SELECT COUNTIF(l.mapper_recording_mbid IS NOT NULL AND k.recording_mbid IS NULL)
                 AS label_not_in_canonical,
               COUNTIF(l.mapper_recording_mbid IS NULL) AS not_evaluable
        FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels` l
        LEFT JOIN `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings` k
          ON k.recording_mbid = l.mapper_recording_mbid
    """)
    assert r["label_not_in_canonical"] > 0, "class exists and must be reported separately"
    assert r["not_evaluable"] > 0


# --- destructive: invoke the real builder -----------------------------------------------

def _run_builder(tmp_path, *extra: str):
    repo = pathlib.Path(__file__).parents[2]
    return subprocess.run(
        [sys.executable, str(repo / "src/matching/build_blocking.py"),
         "--project", PROJECT, "--norm-version", "1.0.0+0bc0dd643e06",
         "--blocking-version", "staged-1.0.0",
         "--out", str(tmp_path / "r.json"), *extra],
        capture_output=True, text=True, cwd=repo, check=False)


def _state(bq) -> dict:
    return one(bq, f"""
        SELECT (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`)
                 AS candidates,
               (SELECT COUNT(DISTINCT candidate_run_id)
                  FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`) AS run_ids,
               (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
                  WHERE {PERIOD}) AS normalized
    """)


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_rerun_is_idempotent(bq, tmp_path):
    before = _state(bq)
    proc = _run_builder(tmp_path)
    assert proc.returncode == 0, proc.stderr[-800:]
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["candidate_rows"] == CANDIDATES
    assert report["listens_with_both_stages"] == 0
    assert _state(bq) == before


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_failure_before_publication_preserves_the_previous_run(bq, tmp_path):
    before = _state(bq)
    proc = _run_builder(tmp_path, "--fail-before-publish")
    assert proc.returncode != 0
    assert "INJECTED FAILURE" in (proc.stdout + proc.stderr)
    assert _state(bq) == before

    from google.cloud import bigquery
    c = bigquery.Client(project=PROJECT)
    for t in ("stg_listens_normalized", "stg_match_candidates"):
        c.query(f"DROP TABLE IF EXISTS `{PROJECT}.splitsheet_silver.{t}`").result()
