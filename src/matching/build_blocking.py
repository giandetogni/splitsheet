"""Normalize the June corpus and generate staged blocking candidates.

Runs entirely as the matcher service account, which cannot read splitsheet_eval or
bronze_listens. The firewall is therefore not a convention here: the process that builds
these tables has no path to a label even if its SQL asked for one.

Staged precedence, and the ordering is the whole design:

  EXACT     every listen with an AVAILABLE exact key joins the EXACT side of the index.
  FALLBACK  runs ONLY for listens that got zero EXACT candidates.

Phase 0A measured why: applying the aggressive key everywhere drops listens with exactly
one candidate from 73.64% to 51.93%, because it merges recordings MusicBrainz keeps apart
and the merged rows are then indistinguishable using the very text that was removed.

PARTIAL and EMPTY keys never produce a candidate. A key built from half a pair equals the
artist alone and would collide every unromanisable recording by that artist.

No scoring, no threshold, no chosen winner. A listen with several candidates gets several
rows, all equal.

Failure-safe: stage, validate, then publish inside one transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time

PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")
EXPECTED_LISTENS = 38_199_641
# The candidate count is not one of the run id's inputs, so it is pinned per run
# rather than as a standing constant: 34,466,312 was measured for
# blk:c005e9a56b1ec542 and says nothing about a run built from a different index.
# A run id absent from here has no ratified cardinality, so it takes the normal
# path and rebuilds rather than trusting a count nobody proved.
EXPECTED_CANDIDATES = {"blk:c005e9a56b1ec542": 34_466_312}
SNAPSHOT = "2026-07-17"
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
MAX_BYTES = 200 * 1024**3

# U+001F, identical to the separator used when the mapping was built in Python.
def pair_hash_sql(alias: str) -> str:
    """Alias-qualified: both sides of the join carry artist_name/recording_name."""
    return (f"TO_HEX(SHA256(CONCAT({alias}.artist_name, CODE_POINTS_TO_STRING([31]), "
            f"{alias}.recording_name)))")


def client_as_matcher(project: str):
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes)
    return bigquery.Client(project=project, credentials=creds)


def run(client, sql: str, label: str, stats: list):
    from google.cloud import bigquery

    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES))
    rows = [dict(r) for r in job.result()]
    stats.append({
        "step": label, "job_id": job.job_id,
        "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
        "duration_ms": int((job.ended - job.started).total_seconds() * 1000),
        "dml_rows": job.num_dml_affected_rows,
    })
    print(f"  {label:<26} billed={job.total_bytes_billed or 0:>14,} "
          f"slot_ms={job.slot_millis or 0:>10,} rows={job.num_dml_affected_rows}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--norm-version", required=True)
    ap.add_argument("--blocking-version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-before-publish", action="store_true")
    args = ap.parse_args()

    p = args.project
    c = client_as_matcher(p)
    stats: list = []
    t0 = time.time()

    # Deterministic: same inputs and config produce the same run id.
    run_id = "blk:" + hashlib.sha256(
        f"{args.norm_version}|{args.blocking_version}|{SNAPSHOT}|{EXPECTED_LISTENS}".encode()
    ).hexdigest()[:16]
    print(f"candidate_run_id = {run_id}")

    # Before staging, before the two index joins and before the published tables: if the
    # candidates already carry this run id under the same versions and snapshot, the work
    # would reproduce what is already there and rewrite every row for a fresh timestamp.
    current = run(c, f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT candidate_run_id) AS runs, MIN(candidate_run_id) AS run_id,
               COUNT(DISTINCT normalization_version) AS nvers,
               MIN(normalization_version) AS nver,
               COUNT(DISTINCT blocking_version) AS bvers, MIN(blocking_version) AS bver,
               COUNT(DISTINCT canonical_snapshot_date) AS snaps,
               MIN(canonical_snapshot_date) AS snap
        FROM `{p}.splitsheet_silver.silver_match_candidates`
    """, "inspect_target", stats)[0]
    ratified = EXPECTED_CANDIDATES.get(run_id)
    already = (ratified is not None and int(current["n"]) == ratified
               and int(current["runs"]) == 1
               and current["run_id"] == run_id
               and int(current["nvers"]) == 1 and current["nver"] == args.norm_version
               and int(current["bvers"]) == 1 and current["bver"] == args.blocking_version
               and int(current["snaps"]) == 1 and str(current["snap"]) == SNAPSHOT)
    if already:
        print(f"target already holds {run_id}: nothing written")
        report = {
            "candidate_run_id": run_id, "skipped": True,
            "normalization_version": args.norm_version,
            "blocking_version": args.blocking_version,
            "candidate_rows": int(current["n"]),
            "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
            "wall_seconds": round(time.time() - t0, 1),
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)
        print(json.dumps(report, indent=1))
        return

    stg_norm = f"{p}.splitsheet_silver.stg_listens_normalized"
    stg_cand = f"{p}.splitsheet_silver.stg_match_candidates"

    # --- stage normalized listens -------------------------------------------------------
    run(c, f"""
        CREATE OR REPLACE TABLE `{stg_norm}`
        PARTITION BY DATE(listened_at) AS
        SELECT v.listen_hash, v.listened_at, v.artist_name, v.recording_name,
               v.release_name,
               m.artist_normalized_unicode, m.recording_normalized_unicode,
               m.lookup_exact, m.lookup_fallback,
               m.normalization_status, m.exact_key_status, m.fallback_key_status,
               m.normalization_version, m.normalization_rules_sha256,
               v.ingestion_run_id, m.normalization_run_id
        FROM `{p}.splitsheet_bronze.v_matcher_input` v
        JOIN `{p}.splitsheet_bronze.listen_pair_normalization` m
          ON m.pair_hash = {pair_hash_sql('v')}
    """, "stage_normalized", stats)

    v = run(c, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_hash,
               COUNTIF(normalization_version != '{args.norm_version}') AS wrong_version
        FROM `{stg_norm}`
    """, "validate_normalized", stats)[0]
    if int(v["n"]) != EXPECTED_LISTENS or int(v["distinct_hash"]) != EXPECTED_LISTENS \
            or int(v["wrong_version"]) != 0:
        raise SystemExit(f"normalized staging failed validation: {v}")

    # --- stage candidates: EXACT first, FALLBACK only where EXACT found nothing ---------
    run(c, f"""
        CREATE OR REPLACE TABLE `{stg_cand}` AS
        WITH exact_hits AS (
          SELECT n.listen_hash, i.recording_mbid AS candidate_recording_mbid,
                 'EXACT' AS block_method, n.lookup_exact AS block_key
          FROM `{stg_norm}` n
          JOIN `{p}.splitsheet_bronze.canonical_blocking_index` i
            ON i.lookup_stage = 'EXACT' AND i.lookup_key = n.lookup_exact
          WHERE n.exact_key_status = 'AVAILABLE'
        ),
        listens_without_exact AS (
          SELECT n.listen_hash, n.lookup_fallback
          FROM `{stg_norm}` n
          LEFT JOIN (SELECT DISTINCT listen_hash FROM exact_hits) e
            USING (listen_hash)
          WHERE e.listen_hash IS NULL
            AND n.fallback_key_status = 'AVAILABLE'
        ),
        fallback_hits AS (
          SELECT w.listen_hash, i.recording_mbid AS candidate_recording_mbid,
                 'FALLBACK' AS block_method, w.lookup_fallback AS block_key
          FROM listens_without_exact w
          JOIN `{p}.splitsheet_bronze.canonical_blocking_index` i
            ON i.lookup_stage = 'FALLBACK' AND i.lookup_key = w.lookup_fallback
        )
        SELECT * FROM exact_hits
        UNION ALL
        SELECT * FROM fallback_hits
    """, "stage_candidates", stats)

    g = run(c, f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT FORMAT('%t|%t|%t', listen_hash, candidate_recording_mbid,
                                     block_method)) AS distinct_grain,
               COUNTIF(block_key IS NULL OR block_key = '') AS empty_keys,
               (SELECT COUNT(*) FROM (
                  SELECT listen_hash FROM `{stg_cand}` GROUP BY listen_hash
                  HAVING COUNTIF(block_method='EXACT') > 0
                     AND COUNTIF(block_method='FALLBACK') > 0)) AS listens_with_both_stages
        FROM `{stg_cand}`
    """, "validate_candidates", stats)[0]
    if int(g["n"]) != int(g["distinct_grain"]):
        raise SystemExit(f"candidate grain is not unique: {g}")
    if int(g["empty_keys"]) != 0:
        raise SystemExit(f"candidates produced from an empty key: {g}")
    if int(g["listens_with_both_stages"]) != 0:
        raise SystemExit(f"fallback ran for listens that already had exact candidates: {g}")

    if args.fail_before_publish:
        raise SystemExit("INJECTED FAILURE after staging validation, before publication.")

    # --- publish both, atomically -------------------------------------------------------
    run(c, f"""
        BEGIN TRANSACTION;
        DELETE FROM `{p}.splitsheet_silver.silver_listens_normalized` WHERE {PERIOD};
        INSERT INTO `{p}.splitsheet_silver.silver_listens_normalized`
        SELECT * FROM `{stg_norm}`;
        DELETE FROM `{p}.splitsheet_silver.silver_match_candidates` WHERE TRUE;
        INSERT INTO `{p}.splitsheet_silver.silver_match_candidates`
        SELECT listen_hash, candidate_recording_mbid, block_method, block_key,
               DATE '{SNAPSHOT}', '{args.blocking_version}', '{args.norm_version}',
               '{run_id}', CURRENT_TIMESTAMP()
        FROM `{stg_cand}`;
        COMMIT TRANSACTION;
    """, "publish_atomic", stats)

    for t in (stg_norm, stg_cand):
        run(c, f"DROP TABLE IF EXISTS `{t}`", "drop_staging", stats)

    report = {
        "candidate_run_id": run_id,
        "normalization_version": args.norm_version,
        "blocking_version": args.blocking_version,
        "normalized_rows": int(v["n"]),
        "candidate_rows": int(g["n"]),
        "listens_with_both_stages": int(g["listens_with_both_stages"]),
        "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "total_slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in
                      ("candidate_run_id", "normalized_rows", "candidate_rows",
                       "listens_with_both_stages", "total_bytes_billed",
                       "total_slot_ms", "wall_seconds")}, indent=1))


if __name__ == "__main__":
    main()
