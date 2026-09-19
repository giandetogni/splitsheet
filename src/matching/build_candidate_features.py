"""Compute label-blind features for the candidate pairs that need a decision.

Runs as the matcher service account, which cannot read splitsheet_eval. Every feature comes
from src/matching/features.py, so the SQL below is generated rather than written -- the
Python the unit tests pin and the SQL BigQuery runs come from one definition.

UNIVERSE, and why EXACT-unique is absent:

    132,852 exact multi-candidate  ->    933,139 pairs
    319,001 fallback unique        ->    319,001 pairs
    217,545 fallback multi         ->  1,793,068 pairs
                                      -----------
                                       3,045,208 pairs

EXACT-unique contributes 31,421,104 more pairs and is excluded on purpose: that decision is
structural (the exact key produced exactly one candidate) and no score participates in it,
so computing similarity there would be 10x the work for an output nothing reads. Excluding
it is a cost decision, not a correctness one, and it is recorded here rather than implied.

Failure-safe: stage, validate, publish in one transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from matching import features as F

PROJECT = "ss-de-944054e7"
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
MAX_BYTES = 400 * 1024**3
PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")

EXPECTED_PAIRS = 3_045_208
EXPECTED_LISTENS = 132_852 + 319_001 + 217_545


def client_as_matcher():
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes)
    return bigquery.Client(project=PROJECT, credentials=creds)


def run(client, sql: str, label: str, stats: list):
    from google.cloud import bigquery

    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES))
    rows = [dict(r) for r in job.result()]
    stats.append({"step": label, "job_id": job.job_id,
                  "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
                  "duration_ms": int((job.ended - job.started).total_seconds() * 1000)})
    print(f"  {label:<22} billed={job.total_bytes_billed or 0:>14,} "
          f"slot_ms={job.slot_millis or 0:>10,}", flush=True)
    return rows


def feature_sql(candidate_run_id: str, feature_run_id: str, norm_version: str) -> str:
    """Generated from features.py so the two implementations cannot drift apart."""
    ratio = F.sql_ascii_retention_ratio(
        "l.artist_normalized_unicode", "l.recording_normalized_unicode",
        "IF(p.block_method = 'EXACT', l.lookup_exact, l.lookup_fallback)")
    return f"""
    WITH universe AS (
      SELECT listen_hash, COUNT(*) AS candidate_count, MIN(block_method) AS block_method
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
      WHERE candidate_run_id = '{candidate_run_id}'
      GROUP BY listen_hash
      -- Aliases, not repeated aggregates: `MIN(block_method)` here would be MIN over the
      -- SELECT alias of the same name, which BigQuery rejects as an aggregate of an
      -- aggregate.
      HAVING candidate_count > 1 OR block_method = 'FALLBACK'
    ),
    pairs AS (
      SELECT c.listen_hash, c.candidate_recording_mbid, c.block_method,
             u.candidate_count
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates` c
      JOIN universe u USING (listen_hash)
      WHERE c.candidate_run_id = '{candidate_run_id}'
    )
    SELECT
      p.listen_hash, p.candidate_recording_mbid, p.block_method, p.candidate_count,
      {F.sql_information_class(ratio)} AS blocking_key_information_class,
      {ratio} AS ascii_retention_ratio,
      {F.sql_feature_columns('l', 'k')},
      -- Probe only. Compared casefolded, NOT normalized: it exists to measure whether
      -- release is worth a fourth normalization surface, not to score anything yet.
      IF(l.release_name IS NULL OR TRIM(l.release_name) = '', NULL,
         LOWER(TRIM(l.release_name)) = k.release_lower) AS release_lower_exact,
      '{F.FEATURE_VERSION}' AS feature_version,
      '{norm_version}' AS normalization_version,
      '{candidate_run_id}' AS candidate_run_id,
      '{feature_run_id}' AS feature_run_id,
      CURRENT_TIMESTAMP() AS created_at
    FROM pairs p
    JOIN (SELECT listen_hash, artist_normalized_unicode, recording_normalized_unicode,
                 lookup_exact, lookup_fallback, release_name
          FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
          WHERE {PERIOD}) l USING (listen_hash)
    JOIN `{PROJECT}.splitsheet_bronze.canonical_match_texts` k
      ON k.recording_mbid = p.candidate_recording_mbid
    """


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--norm-version", required=True)
    ap.add_argument("--candidate-run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-before-publish", action="store_true")
    args = ap.parse_args()

    c = client_as_matcher()
    stats: list = []
    t0 = time.time()

    feature_run_id = "feat:" + hashlib.sha256(
        f"{args.norm_version}|{args.candidate_run_id}|{F.FEATURE_VERSION}".encode()
    ).hexdigest()[:16]
    print(f"feature_version {F.FEATURE_VERSION}  feature_run_id {feature_run_id}", flush=True)

    # Before staging, before the joins and before the transaction: the features are a
    # function of the candidate set, the canonical texts and the feature version alone,
    # and all three are in the run id. If the published table already carries this
    # identity at the ratified size, rebuilding it would spend ~12.4 GB to reproduce
    # what is there and rewrite every row for a fresh CURRENT_TIMESTAMP.
    current = run(c, f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT feature_run_id) AS fruns, MIN(feature_run_id) AS frun,
               COUNT(DISTINCT candidate_run_id) AS cruns, MIN(candidate_run_id) AS crun,
               COUNT(DISTINCT normalization_version) AS nvers,
               MIN(normalization_version) AS nver,
               COUNT(DISTINCT feature_version) AS fvers, MIN(feature_version) AS fver
        FROM `{PROJECT}.splitsheet_silver.silver_candidate_features`
    """, "inspect_target", stats)[0]
    already = (int(current["n"]) == EXPECTED_PAIRS
               and int(current["fruns"]) == 1 and current["frun"] == feature_run_id
               and int(current["cruns"]) == 1 and current["crun"] == args.candidate_run_id
               and int(current["nvers"]) == 1 and current["nver"] == args.norm_version
               and int(current["fvers"]) == 1 and current["fver"] == F.FEATURE_VERSION)
    if already:
        print(f"target already holds {feature_run_id}: nothing written")
        report = {
            "feature_version": F.FEATURE_VERSION, "feature_run_id": feature_run_id,
            "candidate_run_id": args.candidate_run_id, "skipped": True,
            "feature_rows": int(current["n"]),
            "normalization_version": args.norm_version,
            "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
            "wall_seconds": round(time.time() - t0, 1),
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)
        print(json.dumps(report, indent=1))
        return

    stg = f"{PROJECT}.splitsheet_silver.stg_candidate_features"
    run(c, f"CREATE OR REPLACE TABLE `{stg}` CLUSTER BY listen_hash AS "
           + feature_sql(args.candidate_run_id, feature_run_id, args.norm_version),
        "stage_features", stats)

    v = run(c, f"""
        SELECT COUNT(*) AS pairs, COUNT(DISTINCT listen_hash) AS listens,
               COUNT(DISTINCT FORMAT('%t|%t', listen_hash, candidate_recording_mbid))
                 AS distinct_grain,
               COUNTIF(artist_token_similarity IS NULL
                    OR recording_token_similarity IS NULL
                    OR artist_string_similarity IS NULL
                    OR recording_string_similarity IS NULL) AS null_features,
               COUNTIF(artist_string_similarity < 0 OR artist_string_similarity > 1
                    OR recording_string_similarity < 0 OR recording_string_similarity > 1
                    OR artist_token_similarity < 0 OR artist_token_similarity > 1
                    OR recording_token_similarity < 0 OR recording_token_similarity > 1)
                 AS out_of_range,
               COUNTIF(block_method = 'EXACT' AND candidate_count = 1) AS exact_unique_leaked,
               COUNTIF(release_lower_exact IS NULL) AS release_null
        FROM `{stg}`
    """, "validate_features", stats)[0]

    checks = {
        "pair_count_matches_candidate_table": int(v["pairs"]) == EXPECTED_PAIRS,
        "listen_count_matches_universe": int(v["listens"]) == EXPECTED_LISTENS,
        "one_row_per_listen_candidate": int(v["distinct_grain"]) == int(v["pairs"]),
        "no_null_scored_features": int(v["null_features"]) == 0,
        "all_features_in_unit_range": int(v["out_of_range"]) == 0,
        "no_exact_unique_leaked_in": int(v["exact_unique_leaked"]) == 0,
    }
    print("  validation:", json.dumps(checks), flush=True)
    if not all(checks.values()):
        raise SystemExit(f"feature staging failed validation: {checks} {dict(v)}")

    if args.fail_before_publish:
        raise SystemExit("INJECTED FAILURE after staging validation, before publication.")

    run(c, f"""
        BEGIN TRANSACTION;
        DELETE FROM `{PROJECT}.splitsheet_silver.silver_candidate_features` WHERE TRUE;
        INSERT INTO `{PROJECT}.splitsheet_silver.silver_candidate_features`
        SELECT listen_hash, candidate_recording_mbid, block_method, candidate_count,
               blocking_key_information_class, ascii_retention_ratio,
               artist_unicode_exact, recording_unicode_exact,
               artist_token_similarity, recording_token_similarity,
               artist_string_similarity, recording_string_similarity,
               release_lower_exact, feature_version, normalization_version,
               candidate_run_id, feature_run_id, created_at
        FROM `{stg}`;
        COMMIT TRANSACTION;
    """, "publish_atomic", stats)
    run(c, f"DROP TABLE IF EXISTS `{stg}`", "drop_staging", stats)

    profile = run(c, f"""
        SELECT block_method, candidate_count > 1 AS multi, blocking_key_information_class,
               COUNT(*) AS pairs, COUNT(DISTINCT listen_hash) AS listens,
               ROUND(AVG(artist_token_similarity), 4) AS avg_artist_token,
               ROUND(AVG(recording_token_similarity), 4) AS avg_recording_token,
               ROUND(AVG(artist_string_similarity), 4) AS avg_artist_string,
               ROUND(AVG(recording_string_similarity), 4) AS avg_recording_string,
               ROUND(AVG(CAST(artist_unicode_exact AS INT64)), 4) AS rate_artist_exact,
               ROUND(AVG(CAST(recording_unicode_exact AS INT64)), 4) AS rate_recording_exact
        FROM `{PROJECT}.splitsheet_silver.silver_candidate_features`
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """, "profile", stats)

    report = {
        "feature_version": F.FEATURE_VERSION,
        "feature_run_id": feature_run_id,
        "candidate_run_id": args.candidate_run_id,
        "validation": checks,
        "counts": {k: int(x) for k, x in dict(v).items()},
        "profile_by_stage": profile,
        "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "total_slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(json.dumps({k: report[k] for k in
                      ("feature_run_id", "validation", "total_bytes_billed")}, indent=1))
    for p in profile:
        print(f"  {p['block_method']:<9} multi={p['multi']!s:<5} "
              f"{p['blocking_key_information_class']:<26} pairs={p['pairs']:>9,} "
              f"art_tok={p['avg_artist_token']} rec_tok={p['avg_recording_token']}")


if __name__ == "__main__":
    main()
