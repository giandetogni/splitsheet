"""INTEGRATION tests for the scored match result. Not unit tests: these need GCP and cost bytes.

The read-only ones assert the ten published invariants plus two agreement properties that
matter more than any of them:

  * the SQL BigQuery ran computes the same features as the Python the unit suite pins;
  * the published match_score is the score the frozen config produces from the published
    features.

Both are checked on real rows. Two implementations that are never compared is how drift
happens.

The destructive ones invoke the real builders and are excluded from CI.
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

LISTENS = 38_199_641
FEATURE_PAIRS = 3_045_208
CANDIDATE_RUN = "blk:c005e9a56b1ec542"
NORM_VERSION = "1.0.0+0bc0dd643e06"
PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")

BANNED = {"user_id", "recording_msid", "mapper_recording_mbid", "label_available",
          "artist_credit_id", "score", "popularity"}

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from matching import features as F
from matching.scoring import load_scoring_rules

RULES = load_scoring_rules()


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


# --- the ten published invariants ---------------------------------------------------------

@pytest.mark.integration_readonly
def test_all_ten_invariants_hold_on_the_published_table(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_hash,
               COUNTIF(match_status = 'MATCHED' AND matched_recording_mbid IS NULL)
                 AS matched_without_mbid,
               COUNTIF(match_status = 'UNRESOLVED' AND matched_recording_mbid IS NOT NULL)
                 AS unresolved_with_mbid,
               COUNTIF(match_status = 'UNRESOLVED' AND failure_reason IS NULL)
                 AS unresolved_without_reason,
               COUNTIF(match_status = 'MATCHED' AND failure_reason IS NOT NULL)
                 AS matched_with_reason,
               COUNTIF(failure_reason = 'AMBIGUOUS_TIE' AND candidate_count < 2)
                 AS tie_without_two_candidates,
               COUNTIF(failure_reason = 'AMBIGUOUS_TIE' AND matched_recording_mbid IS NOT NULL)
                 AS tie_with_mbid,
               COUNTIF(match_method = 'SCORED_FALLBACK_UNIQUE' AND match_status = 'MATCHED'
                       AND (match_score IS NULL OR match_score < {RULES.fallback_threshold}))
                 AS fallback_unique_below_threshold,
               COUNTIF(match_status = 'MATCHED' AND candidate_count > 1
                       AND (score_margin IS NULL
                            OR score_margin < {RULES.minimum_score_margin}))
                 AS arbitrary_tiebreak,
               COUNTIF(failure_reason = 'UNKNOWN' OR match_method = 'UNKNOWN'
                       OR match_status NOT IN ('MATCHED', 'UNRESOLVED')) AS unknown
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
    """)
    assert r["n"] == LISTENS
    assert r["distinct_hash"] == LISTENS, "one row per listen is the whole point of the table"
    assert r["matched_without_mbid"] == 0
    assert r["unresolved_with_mbid"] == 0
    assert r["unresolved_without_reason"] == 0
    assert r["matched_with_reason"] == 0
    assert r["tie_without_two_candidates"] == 0, "a tie needs at least two scored candidates"
    assert r["tie_with_mbid"] == 0
    assert r["fallback_unique_below_threshold"] == 0
    assert r["arbitrary_tiebreak"] == 0
    assert r["unknown"] == 0


@pytest.mark.integration_readonly
def test_structural_acceptance_carries_no_score(bq):
    """A number here would imply a computation that did not happen."""
    r = one(bq, f"""
        SELECT COUNTIF(match_method = 'STRUCTURAL_EXACT_UNIQUE'
                       AND (match_score IS NOT NULL OR score_margin IS NOT NULL)) AS bad,
               COUNTIF(match_method = 'STRUCTURAL_EXACT_UNIQUE') AS structural
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD}
    """)
    assert r["bad"] == 0
    assert r["structural"] == 31_421_104


@pytest.mark.integration_readonly
def test_fallback_unique_never_has_a_margin(bq):
    """No top-2 exists, so the field that would justify a tie must be NULL."""
    r = one(bq, f"""
        SELECT COUNTIF(match_method = 'SCORED_FALLBACK_UNIQUE'
                       AND score_margin IS NOT NULL) AS with_margin,
               COUNTIF(match_method = 'SCORED_FALLBACK_UNIQUE'
                       AND failure_reason = 'AMBIGUOUS_TIE') AS called_a_tie
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD}
    """)
    assert r["with_margin"] == 0
    assert r["called_a_tie"] == 0


@pytest.mark.integration_readonly
def test_published_rows_were_scored_under_the_frozen_config(bq):
    r = one(bq, f"""
        SELECT COUNT(DISTINCT scoring_version) AS versions,
               MIN(scoring_version) AS version, COUNT(DISTINCT match_run_id) AS runs
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD}
    """)
    assert r["versions"] == 1
    assert r["runs"] == 1
    assert r["version"] == RULES.version


@pytest.mark.integration_readonly
def test_failure_reasons_are_a_closed_set(bq):
    got = {r["failure_reason"] for r in rows(bq, f"""
        SELECT DISTINCT failure_reason
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD} AND failure_reason IS NOT NULL
    """)}
    allowed = {"MISSING_ARTIST", "MISSING_RECORDING", "NO_ALPHANUMERIC_CONTENT",
               "NO_LOOKUP_KEY_PARTIAL", "NO_LOOKUP_KEY_EMPTY", "NO_BLOCK_CANDIDATES",
               "BELOW_THRESHOLD", "AMBIGUOUS_TIE"}
    assert got <= allowed, f"unexpected failure reasons: {got - allowed}"
    # The pre-scoring placeholders must be gone from the final table.
    assert not (got & {"FALLBACK_REQUIRES_SCORING", "MULTIPLE_CANDIDATES_UNSCORED_EXACT",
                       "MULTIPLE_CANDIDATES_UNSCORED_FALLBACK"})


@pytest.mark.integration_readonly
def test_matches_and_features_are_label_blind(bq):
    for table in ("silver_listen_matches", "silver_candidate_features"):
        cols = {r["column_name"] for r in rows(bq, f"""
            SELECT column_name FROM `{PROJECT}.splitsheet_silver`.INFORMATION_SCHEMA.COLUMNS
            WHERE table_name = '{table}'
        """)}
        assert not (cols & BANNED), f"{table} exposes {cols & BANNED}"


@pytest.mark.integration_readonly
def test_feature_table_grain_and_universe(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS pairs,
               COUNT(DISTINCT FORMAT('%t|%t', listen_hash, candidate_recording_mbid)) AS grain,
               COUNT(DISTINCT listen_hash) AS listens,
               COUNTIF(block_method = 'EXACT' AND candidate_count = 1) AS exact_unique
        FROM `{PROJECT}.splitsheet_silver.silver_candidate_features`
        WHERE candidate_run_id = '{CANDIDATE_RUN}'
    """)
    assert r["pairs"] == r["grain"] == FEATURE_PAIRS
    assert r["listens"] == 132_852 + 319_001 + 217_545
    assert r["exact_unique"] == 0, "EXACT-unique is decided structurally and must not be scored"


@pytest.mark.integration_readonly
def test_matcher_identity_still_cannot_read_the_labels():
    import google.auth
    from google.api_core.exceptions import Forbidden
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes)
    c = bigquery.Client(project=PROJECT, credentials=creds)

    assert one(c, f"SELECT COUNT(*) AS n "
                  f"FROM `{PROJECT}.splitsheet_bronze.canonical_match_texts`")["n"] > 0
    with pytest.raises(Forbidden, match="(?i)access denied"):
        c.query(f"SELECT COUNT(*) FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`"
                ).result()


# --- the two agreement properties --------------------------------------------------------

@pytest.mark.integration_readonly
def test_sql_features_agree_with_the_python_implementation(bq):
    """Recompute the published features in Python from the same two texts."""
    sample = rows(bq, f"""
        SELECT f.listen_hash, f.candidate_recording_mbid,
               n.artist_normalized_unicode AS l_artist,
               n.recording_normalized_unicode AS l_recording,
               k.artist_normalized_unicode AS k_artist,
               k.recording_normalized_unicode AS k_recording,
               f.artist_unicode_exact, f.recording_unicode_exact,
               f.artist_token_similarity, f.recording_token_similarity,
               f.artist_string_similarity, f.recording_string_similarity
        FROM `{PROJECT}.splitsheet_silver.silver_candidate_features` f
        JOIN (SELECT listen_hash, artist_normalized_unicode, recording_normalized_unicode
              FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
              WHERE {PERIOD}) n USING (listen_hash)
        JOIN `{PROJECT}.splitsheet_bronze.canonical_match_texts` k
          ON k.recording_mbid = f.candidate_recording_mbid
        -- Deterministic pseudo-random sample: reproducible, and not the first rows of a
        -- clustered table, which would all come from one part of the key space.
        WHERE MOD(ABS(FARM_FINGERPRINT(f.listen_hash)), 4000) = 7
        LIMIT 300
    """)
    assert len(sample) >= 50, f"sample too small to prove anything: {len(sample)}"
    mismatches = []
    for s in sample:
        expected = F.features_for_pair(s["l_artist"], s["l_recording"],
                                       s["k_artist"], s["k_recording"])
        for name, value in expected.items():
            published = s[name]
            if isinstance(value, bool):
                if bool(published) != value:
                    mismatches.append((s["listen_hash"], name, published, value))
            elif abs(float(published) - value) > 1e-9:
                mismatches.append((s["listen_hash"], name, published, value))
    assert not mismatches, f"SQL and Python disagree on {len(mismatches)}: {mismatches[:5]}"


@pytest.mark.integration_readonly
def test_published_score_is_what_the_frozen_config_produces(bq):
    sample = rows(bq, f"""
        WITH scored AS (
          SELECT listen_hash, candidate_recording_mbid,
                 artist_unicode_exact, recording_unicode_exact,
                 artist_token_similarity, recording_token_similarity,
                 artist_string_similarity, recording_string_similarity, release_lower_exact,
                 ROW_NUMBER() OVER (PARTITION BY listen_hash
                                    ORDER BY {RULES.sql_score()} DESC,
                                             candidate_recording_mbid) AS rn
          FROM `{PROJECT}.splitsheet_silver.silver_candidate_features`
          WHERE candidate_run_id = '{CANDIDATE_RUN}'
            AND MOD(ABS(FARM_FINGERPRINT(listen_hash)), 4000) = 7
        )
        SELECT s.*, m.match_score, m.match_status, m.match_method, m.candidate_count
        FROM scored s
        JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` m USING (listen_hash)
        WHERE m.{PERIOD} AND s.rn = 1
        LIMIT 300
    """)
    assert len(sample) >= 50
    feature_names = list(RULES.weights)
    for s in sample:
        expected = RULES.score({n: s[n] for n in feature_names})
        assert s["match_score"] is not None
        assert abs(float(s["match_score"]) - expected) < 1e-9, (
            f"published score {s['match_score']} != config score {expected} "
            f"for {s['listen_hash']}")


@pytest.mark.integration_readonly
def test_every_accepted_decision_would_be_accepted_again_by_the_python_policy(bq):
    """The decision, not just the score: replay decide() over published top1/top2."""
    sample = rows(bq, f"""
        SELECT m.block_method, m.candidate_count, m.match_score, m.score_margin,
               m.match_status, m.match_method, m.failure_reason
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` m
        WHERE m.{PERIOD} AND m.match_method != 'STRUCTURAL_EXACT_UNIQUE'
          AND m.match_method != 'NO_CANDIDATES'
          AND MOD(ABS(FARM_FINGERPRINT(m.listen_hash)), 500) = 3
        LIMIT 2000
    """)
    assert len(sample) >= 100
    for s in sample:
        top1 = float(s["match_score"])
        top2 = None if s["score_margin"] is None else top1 - float(s["score_margin"])
        outcome = RULES.decide(s["block_method"], int(s["candidate_count"]), top1, top2)
        if s["match_status"] == "MATCHED":
            assert outcome == "ACCEPTED", f"published MATCHED but policy says {outcome}: {s}"
        else:
            assert outcome == s["failure_reason"], f"policy says {outcome}: {s}"


# --- destructive: invoke the real builders ------------------------------------------------

def _run_matcher(tmp_path, *extra: str):
    repo = pathlib.Path(__file__).parents[2]
    return subprocess.run(
        [sys.executable, str(repo / "src/matching/build_match_results.py"),
         "--norm-version", NORM_VERSION, "--blocking-version", "staged-1.0.0",
         "--candidate-run-id", CANDIDATE_RUN, "--out", str(tmp_path / "r.json"), *extra],
        capture_output=True, text=True, cwd=repo, check=False)


def _state(bq) -> dict:
    return one(bq, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT match_run_id) AS runs, MIN(match_run_id) AS run_id,
               COUNTIF(match_status = 'MATCHED') AS matched,
               ROUND(SUM(IFNULL(match_score, 0)), 4) AS score_sum,
               MIN(scoring_version) AS scoring_version
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD}
    """)


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_rerun_is_idempotent_and_run_id_is_deterministic(bq, tmp_path):
    before = _state(bq)
    proc = _run_matcher(tmp_path)
    assert proc.returncode == 0, proc.stderr[-1500:]
    report = json.loads((tmp_path / "r.json").read_text())
    assert all(report["validation"].values())
    after = _state(bq)
    assert after == before, "a re-run over the same inputs changed the published result"
    assert report["match_run_id"] == before["run_id"], (
        "same source and config must yield the same match_run_id")


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_failure_before_publication_preserves_the_previous_run(bq, tmp_path):
    before = _state(bq)
    proc = _run_matcher(tmp_path, "--fail-before-publish")
    assert proc.returncode != 0
    assert "INJECTED FAILURE" in (proc.stdout + proc.stderr)
    assert _state(bq) == before

    from google.cloud import bigquery
    c = bigquery.Client(project=PROJECT)
    c.query(f"DROP TABLE IF EXISTS `{PROJECT}.splitsheet_silver.stg_listen_matches`").result()
