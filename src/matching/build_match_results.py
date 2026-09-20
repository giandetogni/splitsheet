"""Score the candidates and publish exactly one match result per listen.

Runs as the matcher service account, which cannot read splitsheet_eval. Nothing here consults
a reference label; the thresholds it applies were chosen on the calibration partition by
src/evaluation/calibrate.py and frozen into config/scoring_rules.yml, which is the only
channel between the two sides.

FOUR DECISION PATHS, three of them scored:

  EXACT + one candidate    STRUCTURAL_EXACT_UNIQUE. MATCHED without a score, because "the
                           exact key produced exactly one candidate" is a structural fact.
                           match_score and score_margin are NULL: reporting a number here
                           would imply a computation that did not happen.
  EXACT + several          SCORED_EXACT_MULTIPLE. Threshold and margin.
  FALLBACK + one           SCORED_FALLBACK_UNIQUE. Absolute threshold only. There is no top-2,
                           so this path can never produce an AMBIGUOUS_TIE.
  FALLBACK + several       SCORED_FALLBACK_MULTIPLE. Threshold and margin.
  no candidate             NO_CANDIDATES.

UNIQUENESS BY CONSTRUCTION, not by later assertion: the sole-candidate CTE selects only groups
of exactly one row, so MIN over that grain is that row. The previous version used ANY_VALUE
guarded by a downstream validation, which is a check that the arbitrary pick happened to be
safe rather than a guarantee that no arbitrary pick was made.

Failure-safe: stage, validate ten invariants, publish in one transaction, drop staging.
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
from matching.scoring import load_scoring_rules

PROJECT = "ss-de-944054e7"
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
MAX_BYTES = 400 * 1024**3
PERIOD = (
    "listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
    "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
)
EXPECTED_LISTENS = 38_199_641


def client_as_matcher():
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes
    )
    return bigquery.Client(project=PROJECT, credentials=creds)


def run(client, sql: str, label: str, stats: list):
    from google.cloud import bigquery

    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
    rows = [dict(r) for r in job.result()]
    stats.append(
        {
            "step": label,
            "job_id": job.job_id,
            "bytes_billed": job.total_bytes_billed,
            "slot_ms": job.slot_millis,
            "duration_ms": int((job.ended - job.started).total_seconds() * 1000),
        }
    )
    print(
        f"  {label:<22} billed={job.total_bytes_billed or 0:>14,} "
        f"slot_ms={job.slot_millis or 0:>10,}",
        flush=True,
    )
    return rows


def stage_sql(rules, args, run_id: str) -> str:
    """One row per listen. Every expression that implements a rule is generated from config."""
    ratio = F.sql_ascii_retention_ratio(
        "n.artist_normalized_unicode",
        "n.recording_normalized_unicode",
        "IF(cnt.block_method = 'EXACT', n.lookup_exact, n.lookup_fallback)",
    )
    return f"""
    WITH cnt AS (
      SELECT listen_hash, COUNT(*) AS candidate_count, MIN(block_method) AS block_method
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
      WHERE candidate_run_id = '{args.candidate_run_id}'
      GROUP BY listen_hash
    ),
    -- Uniqueness BY CONSTRUCTION: only groups of exactly one row survive the HAVING, so MIN
    -- over that grain returns that one row rather than an arbitrary member of a set.
    sole AS (
      SELECT listen_hash, MIN(candidate_recording_mbid) AS sole_candidate
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
      WHERE candidate_run_id = '{args.candidate_run_id}'
      GROUP BY listen_hash
      HAVING COUNT(*) = 1
    ),
    scored AS (
      SELECT listen_hash, candidate_recording_mbid, {rules.sql_score()} AS score
      FROM `{PROJECT}.splitsheet_silver.silver_candidate_features`
      WHERE candidate_run_id = '{args.candidate_run_id}'
    ),
    ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY listen_hash
                                   -- MBID only for reproducibility of the row order. It never
                                   -- decides an outcome: an equal top-2 is refused as a tie
                                   -- below, never awarded to whichever row sorted first.
                                   ORDER BY score DESC, candidate_recording_mbid) AS rn
      FROM scored
    ),
    tops AS (
      SELECT listen_hash,
             MAX(IF(rn = 1, score, NULL)) AS top1,
             MAX(IF(rn = 2, score, NULL)) AS top2,
             MAX(IF(rn = 1, candidate_recording_mbid, NULL)) AS top1_mbid,
             -- Guards the accept branch: if two DIFFERENT candidates share the top score, the
             -- margin is zero and the tie rule refuses them, so this can only be >1 when the
             -- outcome is already AMBIGUOUS_TIE.
             COUNTIF(rn <= 2) AS scored_candidates
      FROM ranked GROUP BY listen_hash
    ),
    j AS (
      SELECT n.listen_hash, n.listened_at,
             n.normalization_status, n.exact_key_status, n.fallback_key_status,
             IFNULL(cnt.candidate_count, 0) AS candidate_count,
             cnt.block_method, sole.sole_candidate,
             t.top1, t.top2, t.top1_mbid, IFNULL(t.scored_candidates, 0) AS scored_candidates,
             {F.sql_information_class(ratio)} AS blocking_key_information_class
      FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
      LEFT JOIN cnt USING (listen_hash)
      LEFT JOIN sole USING (listen_hash)
      LEFT JOIN tops t USING (listen_hash)
      WHERE {PERIOD}
    ),
    decided AS (
      SELECT *,
             (block_method = 'EXACT' AND candidate_count = 1) AS structural_unique,
             {rules.sql_decision()} AS outcome
      FROM j
    )
    SELECT
      listen_hash, listened_at,
      CASE WHEN structural_unique THEN sole_candidate
           WHEN outcome = '{"ACCEPTED"}' THEN top1_mbid
           ELSE NULL END AS matched_recording_mbid,
      IF(structural_unique OR outcome = 'ACCEPTED', 'MATCHED', 'UNRESOLVED') AS match_status,
      CASE WHEN block_method = 'EXACT' THEN 'C'
           WHEN block_method = 'FALLBACK' THEN 'D'
           ELSE 'E' END AS match_tier,
      CASE WHEN structural_unique THEN 'STRUCTURAL_EXACT_UNIQUE'
           WHEN candidate_count = 0 THEN 'NO_CANDIDATES'
           WHEN block_method = 'EXACT' THEN 'SCORED_EXACT_MULTIPLE'
           WHEN candidate_count = 1 THEN 'SCORED_FALLBACK_UNIQUE'
           ELSE 'SCORED_FALLBACK_MULTIPLE' END AS match_method,
      -- NULL, not zero, wherever no score was computed: structural acceptance and listens
      -- with no candidate at all.
      IF(structural_unique, NULL, top1) AS match_score,
      -- NULL whenever there is no second candidate to be near. This is what makes
      -- "fallback-unique cannot be an ambiguous tie" visible in the data.
      IF(structural_unique, NULL, top1 - top2) AS score_margin,
      CASE
        WHEN structural_unique OR outcome = 'ACCEPTED' THEN NULL
        WHEN normalization_status = 'MISSING_ARTIST'          THEN 'MISSING_ARTIST'
        WHEN normalization_status = 'MISSING_RECORDING'       THEN 'MISSING_RECORDING'
        WHEN normalization_status = 'NO_ALPHANUMERIC_CONTENT' THEN 'NO_ALPHANUMERIC_CONTENT'
        WHEN exact_key_status = 'PARTIAL'                     THEN 'NO_LOOKUP_KEY_PARTIAL'
        WHEN exact_key_status = 'EMPTY'                       THEN 'NO_LOOKUP_KEY_EMPTY'
        ELSE outcome END AS failure_reason,
      block_method, candidate_count, blocking_key_information_class,
      '{args.norm_version}' AS normalization_version,
      '{args.blocking_version}' AS blocking_version,
      '{rules.version}' AS scoring_version,
      '{args.candidate_run_id}' AS candidate_run_id,
      '{run_id}' AS match_run_id,
      CURRENT_TIMESTAMP() AS matched_at
    FROM decided
    """


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--norm-version", required=True)
    ap.add_argument("--blocking-version", required=True)
    ap.add_argument("--candidate-run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-before-publish", action="store_true")
    args = ap.parse_args()

    rules = load_scoring_rules()
    c = client_as_matcher()
    stats: list = []
    t0 = time.time()

    # Deterministic from content and config, never from wall-clock time: the same inputs under
    # the same rules produce the same match_run_id, which is what makes a re-run detectable as
    # a re-run rather than as a new result.
    run_id = (
        "match:"
        + hashlib.sha256(
            f"{args.norm_version}|{args.blocking_version}|{args.candidate_run_id}|"
            f"{rules.version}|{F.FEATURE_VERSION}".encode()
        ).hexdigest()[:16]
    )
    print(
        f"scoring {rules.version}  feature {F.FEATURE_VERSION}  match_run_id {run_id}", flush=True
    )

    # Before staging, before the scoring pass and before the transaction: the result is a
    # function of the candidates, the features and the frozen rules alone, and all of them
    # are in the run id. If the period already holds this identity at full size, rebuilding
    # it would spend ~30.9 GB to reproduce what is there and rewrite every row for a fresh
    # matched_at. The PERIOD filter is not optional: the table requires a partition filter,
    # so a guard without it is rejected before it can read anything.
    current = run(
        c,
        f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT match_run_id) AS mruns, MIN(match_run_id) AS mrun,
               COUNT(DISTINCT candidate_run_id) AS cruns, MIN(candidate_run_id) AS crun,
               COUNT(DISTINCT scoring_version) AS svers, MIN(scoring_version) AS sver,
               COUNT(DISTINCT normalization_version) AS nvers,
               MIN(normalization_version) AS nver,
               COUNT(DISTINCT blocking_version) AS bvers, MIN(blocking_version) AS bver
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
    """,
        "inspect_target",
        stats,
    )[0]
    already = (
        int(current["n"]) == EXPECTED_LISTENS
        and int(current["mruns"]) == 1
        and current["mrun"] == run_id
        and int(current["cruns"]) == 1
        and current["crun"] == args.candidate_run_id
        and int(current["svers"]) == 1
        and current["sver"] == rules.version
        and int(current["nvers"]) == 1
        and current["nver"] == args.norm_version
        and int(current["bvers"]) == 1
        and current["bver"] == args.blocking_version
    )
    if already:
        print(f"period already holds {run_id}: nothing written")
        report = {
            "artifact": "silver_listen_matches",
            "match_run_id": run_id,
            "skipped": True,
            "scoring_version": rules.version,
            "feature_version": F.FEATURE_VERSION,
            "candidate_run_id": args.candidate_run_id,
            "normalization_version": args.norm_version,
            "blocking_version": args.blocking_version,
            "matched_rows": int(current["n"]),
            "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
            "wall_seconds": round(time.time() - t0, 1),
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)
        print(json.dumps(report, indent=1))
        return

    stg = f"{PROJECT}.splitsheet_silver.stg_listen_matches"
    run(
        c,
        f"CREATE OR REPLACE TABLE `{stg}` PARTITION BY DATE(listened_at) AS "
        + stage_sql(rules, args, run_id),
        "stage_matches",
        stats,
    )

    v = run(
        c,
        f"""
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
                       AND (match_score IS NULL OR match_score < {rules.fallback_threshold}))
                 AS fallback_accepted_below_threshold,
               COUNTIF(match_method = 'SCORED_EXACT_MULTIPLE' AND match_status = 'MATCHED'
                       AND (match_score IS NULL OR match_score < {rules.exact_threshold}
                            OR score_margin IS NULL
                            OR score_margin < {rules.minimum_score_margin}))
                 AS exact_multi_accepted_below_policy,
               COUNTIF(match_method = 'SCORED_FALLBACK_MULTIPLE' AND match_status = 'MATCHED'
                       AND (match_score IS NULL OR match_score < {rules.fallback_threshold}
                            OR score_margin IS NULL
                            OR score_margin < {rules.minimum_score_margin}))
                 AS fallback_multi_accepted_below_policy,
               COUNTIF(match_method = 'STRUCTURAL_EXACT_UNIQUE'
                       AND (match_score IS NOT NULL OR score_margin IS NOT NULL))
                 AS structural_carrying_a_score,
               COUNTIF(match_status = 'MATCHED' AND candidate_count > 1
                       AND (score_margin IS NULL
                            OR score_margin < {rules.minimum_score_margin}))
                 AS arbitrary_tiebreak,
               COUNTIF(failure_reason = 'UNKNOWN' OR match_method = 'UNKNOWN')
                 AS unknown_bucket,
               COUNTIF(match_score IS NOT NULL AND (match_score < 0 OR match_score > 1))
                 AS score_out_of_range
        FROM `{stg}`
    """,
        "validate_matches",
        stats,
    )[0]

    checks = {
        "one_row_per_listen": int(v["n"]) == EXPECTED_LISTENS
        and int(v["distinct_hash"]) == EXPECTED_LISTENS,
        "matched_always_has_mbid": int(v["matched_without_mbid"]) == 0,
        "unresolved_never_has_mbid": int(v["unresolved_with_mbid"]) == 0,
        "unresolved_always_has_reason": int(v["unresolved_without_reason"]) == 0,
        "matched_never_has_reason": int(v["matched_with_reason"]) == 0,
        "tie_always_has_two_candidates": int(v["tie_without_two_candidates"]) == 0,
        "tie_never_has_mbid": int(v["tie_with_mbid"]) == 0,
        "fallback_unique_accepted_above_threshold": int(v["fallback_accepted_below_threshold"])
        == 0,
        "scored_acceptance_respects_threshold_and_margin": int(
            v["exact_multi_accepted_below_policy"]
        )
        == 0
        and int(v["fallback_multi_accepted_below_policy"]) == 0,
        "structural_unique_carries_no_score": int(v["structural_carrying_a_score"]) == 0,
        "no_arbitrary_tiebreak": int(v["arbitrary_tiebreak"]) == 0,
        "no_unknown_bucket": int(v["unknown_bucket"]) == 0,
        "score_in_unit_range": int(v["score_out_of_range"]) == 0,
    }
    print("  validation:", json.dumps(checks), flush=True)
    if not all(checks.values()):
        raise SystemExit(f"match staging failed validation: {checks} {dict(v)}")

    if args.fail_before_publish:
        raise SystemExit("INJECTED FAILURE after staging validation, before publication.")

    run(
        c,
        f"""
        BEGIN TRANSACTION;
        DELETE FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD};
        INSERT INTO `{PROJECT}.splitsheet_silver.silver_listen_matches`
        SELECT listen_hash, listened_at, matched_recording_mbid, match_status, match_tier,
               match_method, match_score, score_margin, failure_reason, block_method,
               candidate_count, blocking_key_information_class, normalization_version,
               blocking_version, scoring_version, candidate_run_id, match_run_id, matched_at
        FROM `{stg}`;
        COMMIT TRANSACTION;
    """,
        "publish_atomic",
        stats,
    )
    run(c, f"DROP TABLE IF EXISTS `{stg}`", "drop_staging", stats)

    dist = run(
        c,
        f"""
        SELECT match_method, match_status, failure_reason,
               blocking_key_information_class, COUNT(*) AS listens,
               ROUND(100 * COUNT(*) / {EXPECTED_LISTENS}, 4) AS pct,
               ROUND(AVG(match_score), 4) AS avg_score,
               ROUND(AVG(score_margin), 4) AS avg_margin
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
        GROUP BY 1, 2, 3, 4 ORDER BY listens DESC
    """,
        "distribution",
        stats,
    )

    coverage = run(
        c,
        f"""
        SELECT COUNTIF(match_status = 'MATCHED') AS matched,
               ROUND(100 * COUNTIF(match_status = 'MATCHED') / COUNT(*), 4) AS matched_pct,
               COUNTIF(match_method = 'STRUCTURAL_EXACT_UNIQUE') AS structural,
               COUNTIF(match_status = 'MATCHED' AND match_method != 'STRUCTURAL_EXACT_UNIQUE')
                 AS scored_acceptances
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
    """,
        "coverage",
        stats,
    )[0]

    report = {
        "artifact": "silver_listen_matches",
        "match_run_id": run_id,
        "scoring_version": rules.version,
        "feature_version": F.FEATURE_VERSION,
        "candidate_run_id": args.candidate_run_id,
        "validation": checks,
        "counts": {k: int(x) for k, x in dict(v).items()},
        "coverage": {k: float(x) for k, x in dict(coverage).items()},
        "distribution": dist,
        "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "total_slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    print(
        f"\n  matched {int(coverage['matched']):,} of {EXPECTED_LISTENS:,} "
        f"({coverage['matched_pct']}%)  structural={int(coverage['structural']):,}  "
        f"scored={int(coverage['scored_acceptances']):,}\n"
    )
    for d in dist:
        print(
            f"  {d['match_method']:<25} {d['match_status']:<11} "
            f"{(d['failure_reason'] or '-'):<24} "
            f"{d['blocking_key_information_class'][:6]:<7} {d['listens']:>10,}  "
            f"{d['pct']:>8}%  score={d['avg_score']}"
        )


if __name__ == "__main__":
    main()
