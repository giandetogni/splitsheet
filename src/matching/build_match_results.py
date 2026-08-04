"""Assign one match result per listen. Label-blind, tie-preserving.

Runs as the matcher service account, which cannot read splitsheet_eval. The tier policy in
config/match_tiers.yml is applied as written; nothing here consults a reference label.

THE ONE RULE THAT MATTERS: more than one candidate is never resolved into a winner.
PROJECT_SPEC.md states that paying the wrong rights holder is worse than suspending
payment, and blocking returns candidates without ranking, so any pick among them would be
arbitrary by construction. Ties become UNRESOLVED with an AMBIGUOUS_TIE reason and a NULL
recording MBID.

Failure reasons are assigned by first match in a fixed order, so a listen with no usable
key is never reported as "no candidates found" -- those are different operational problems
with different fixes.

Failure-safe: stage, validate, publish in one transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import time

PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")
EXPECTED_LISTENS = 38_199_641
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
MAX_BYTES = 200 * 1024**3
TIER_CONFIG = pathlib.Path(__file__).parents[2] / "config/match_tiers.yml"


def load_tier_policy() -> dict:
    """Read the policy so confidences and the tie rule come from config, not from code."""
    import yaml

    with open(TIER_CONFIG) as fh:
        raw = yaml.safe_load(fh)
    tiers = {t["id"]: t for t in raw["tiers"]}
    if raw["ambiguity"]["break_ties"] or raw["ambiguity"]["select_among_multiple"] != "never":
        raise SystemExit("tier policy would break ties; refusing to run")
    for t in ("A", "B"):
        if tiers[t]["implemented"]:
            raise SystemExit(f"tier {t} is marked implemented but has no data source")
    return {
        "version": str(raw["version"]),
        "conf_c": tiers["C"]["confidence"],
        "conf_d": tiers["D"]["confidence"],
        "digest": hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:12],
    }


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
    stats.append({"step": label, "job_id": job.job_id,
                  "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
                  "duration_ms": int((job.ended - job.started).total_seconds() * 1000)})
    print(f"  {label:<24} billed={job.total_bytes_billed or 0:>14,} "
          f"slot_ms={job.slot_millis or 0:>10,}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--norm-version", required=True)
    ap.add_argument("--blocking-version", required=True)
    ap.add_argument("--candidate-run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-before-publish", action="store_true")
    args = ap.parse_args()

    pol = load_tier_policy()
    p = args.project
    c = client_as_matcher(p)
    stats: list = []
    t0 = time.time()

    run_id = "match:" + hashlib.sha256(
        f"{args.norm_version}|{args.blocking_version}|{args.candidate_run_id}|"
        f"{pol['version']}|{pol['digest']}".encode()).hexdigest()[:16]
    print(f"tier_policy {pol['version']}+{pol['digest']}   match_run_id {run_id}")

    stg = f"{p}.splitsheet_silver.stg_listen_matches"

    run(c, f"""
        CREATE OR REPLACE TABLE `{stg}`
        PARTITION BY DATE(listened_at) AS
        WITH cand AS (
          SELECT listen_hash, COUNT(*) AS candidate_count,
                 ANY_VALUE(block_method) AS block_method,
                 -- Safe only because a single candidate is the only case that reads it.
                 ANY_VALUE(candidate_recording_mbid) AS sole_candidate
          FROM `{p}.splitsheet_silver.silver_match_candidates`
          GROUP BY listen_hash
        ),
        j AS (
          SELECT n.listen_hash, n.listened_at,
                 n.normalization_status, n.exact_key_status, n.fallback_key_status,
                 IFNULL(k.candidate_count, 0) AS candidate_count,
                 k.block_method, k.sole_candidate
          FROM `{p}.splitsheet_silver.silver_listens_normalized` n
          LEFT JOIN cand k USING (listen_hash)
          WHERE {PERIOD}
        )
        SELECT
          listen_hash, listened_at,
          IF(candidate_count = 1, 'MATCHED', 'UNRESOLVED') AS match_status,
          CASE WHEN candidate_count = 1 AND block_method = 'EXACT'    THEN 'C'
               WHEN candidate_count = 1 AND block_method = 'FALLBACK' THEN 'D'
               ELSE 'E' END AS match_tier,
          -- NULL for every tie, by policy. Never a chosen winner.
          IF(candidate_count = 1, sole_candidate, NULL) AS matched_recording_mbid,
          CAST(CASE WHEN candidate_count = 1 AND block_method = 'EXACT'    THEN {pol['conf_c']}
                    WHEN candidate_count = 1 AND block_method = 'FALLBACK' THEN {pol['conf_d']}
                    ELSE 0 END AS NUMERIC) AS tier_confidence,
          block_method, candidate_count,
          -- Exactly one reason, first match wins, order is deliberate.
          CASE
            WHEN candidate_count = 1 THEN NULL
            WHEN normalization_status = 'MISSING_ARTIST'           THEN 'MISSING_ARTIST'
            WHEN normalization_status = 'MISSING_RECORDING'        THEN 'MISSING_RECORDING'
            WHEN normalization_status = 'NO_ALPHANUMERIC_CONTENT'  THEN 'NO_ALPHANUMERIC_CONTENT'
            WHEN exact_key_status = 'PARTIAL'                      THEN 'NO_LOOKUP_KEY_PARTIAL'
            WHEN exact_key_status = 'EMPTY'                        THEN 'NO_LOOKUP_KEY_EMPTY'
            WHEN candidate_count > 1 AND block_method = 'EXACT'     THEN 'AMBIGUOUS_TIE_EXACT'
            WHEN candidate_count > 1 AND block_method = 'FALLBACK'  THEN 'AMBIGUOUS_TIE_FALLBACK'
            ELSE 'NO_BLOCK_CANDIDATES' END AS failure_reason,
          '{args.norm_version}' AS normalization_version,
          '{args.blocking_version}' AS blocking_version,
          '{pol['version']}+{pol['digest']}' AS tier_policy_version,
          '{args.candidate_run_id}' AS candidate_run_id,
          '{run_id}' AS match_run_id,
          CURRENT_TIMESTAMP() AS created_at
        FROM j
    """, "stage_matches", stats)

    v = run(c, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_hash,
               COUNTIF(match_status='MATCHED' AND matched_recording_mbid IS NULL) AS matched_without_mbid,
               COUNTIF(match_status='UNRESOLVED' AND failure_reason IS NULL) AS unresolved_without_reason,
               COUNTIF(match_status='MATCHED' AND failure_reason IS NOT NULL) AS matched_with_reason,
               COUNTIF(candidate_count > 1 AND matched_recording_mbid IS NOT NULL) AS tie_resolved,
               COUNTIF(failure_reason = 'UNKNOWN') AS unknown_bucket
        FROM `{stg}`
    """, "validate_matches", stats)[0]
    checks = {
        "one_row_per_listen": int(v["n"]) == EXPECTED_LISTENS
        and int(v["distinct_hash"]) == EXPECTED_LISTENS,
        "matched_always_has_mbid": int(v["matched_without_mbid"]) == 0,
        "unresolved_always_has_reason": int(v["unresolved_without_reason"]) == 0,
        "matched_never_has_reason": int(v["matched_with_reason"]) == 0,
        "no_tie_was_resolved": int(v["tie_resolved"]) == 0,
        "no_unknown_bucket": int(v["unknown_bucket"]) == 0,
    }
    print("  validation:", json.dumps(checks))
    if not all(checks.values()):
        raise SystemExit(f"match staging failed validation: {checks} {v}")

    if args.fail_before_publish:
        raise SystemExit("INJECTED FAILURE after staging validation, before publication.")

    run(c, f"""
        BEGIN TRANSACTION;
        DELETE FROM `{p}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD};
        INSERT INTO `{p}.splitsheet_silver.silver_listen_matches`
        SELECT * FROM `{stg}`;
        COMMIT TRANSACTION;
    """, "publish_atomic", stats)
    run(c, f"DROP TABLE IF EXISTS `{stg}`", "drop_staging", stats)

    dist = run(c, f"""
        SELECT match_tier, match_status, failure_reason, COUNT(*) AS listens,
               ROUND(100*COUNT(*)/{EXPECTED_LISTENS}, 4) AS pct
        FROM `{p}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
        GROUP BY 1,2,3 ORDER BY listens DESC
    """, "tier_distribution", stats)

    report = {
        "match_run_id": run_id,
        "tier_policy_version": f"{pol['version']}+{pol['digest']}",
        "validation": checks,
        "distribution": dist,
        "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "total_slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print()
    for d in dist:
        print(f"  tier {d['match_tier']} {d['match_status']:<11} "
              f"{str(d['failure_reason'] or '-'):<26} {d['listens']:>10,}  {d['pct']:>8}%")


if __name__ == "__main__":
    main()
