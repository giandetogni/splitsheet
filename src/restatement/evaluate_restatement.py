"""Evaluate the restatement honestly, starting by inventorying whether an untouched partition exists.

THE INVENTORY COMES FIRST, and its answer is no.

    calibration   CONSUMED. Weights, thresholds and margins were chosen on it in Phase 4B.
    validation    CONSUMED. Opened exactly once under scoring 1.0.0+cb21f9704ff0.
    holdout       PREVIOUSLY OBSERVED. Read in the Phase 3B fallback analysis and the Phase 4
                  preflight.
    NOT_EVALUABLE Listens with no mapper label at all. Not a partition and not a test set: there is
                  nothing to compare against.

So there is NO untouched reference partition, and this script does not manufacture one. It does not
reshuffle consumed data under a new name, and it makes no new claim about generalisation accuracy.

WHAT IT DOES REPORT:

  1. UNSUPERVISED OBSERVABLES, which need no label at all: how many listens moved between states,
     how many newly matched, how many became ambiguous, how the candidate counts are distributed, and
     every change of match method and failure reason.

  2. ONE REFERENCE COMPARISON, restricted to the newly matched cohort and labelled for exactly what
     it is. The narrow claim it supports: these specific labels could not have participated in
     choosing any rule, because these listens produced ZERO candidates under v1 and therefore never
     entered the feature table, the calibration grid or the validation measurement. The transliteration
     rule was frozen in config/restatement_trigger.yml before this ran, and the alternative was chosen
     by an unsupervised probe.

     The broad claim it does NOT support: that this is a blind validation of the restatement. These
     labels sit inside partitions that are already consumed, and the honest limitation is stated
     alongside every number rather than in a footnote.

  3. That this comparison is now CONSUMED too.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from evaluation.split import load_split_config

PROJECT = "ss-de-944054e7"
MAX_BYTES = 200 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25
SNAPSHOT = "2026-07-17"

V1 = f"{PROJECT}.splitsheet_silver.silver_listen_matches"
V2 = f"{PROJECT}.splitsheet_silver.silver_listen_matches_restated"


def period(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return (
        f"{a}listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
        f"AND {a}listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
    )


PARTITION_STATUS = {
    "calibration": "CONSUMED: weights, thresholds and margins were chosen on it",
    "validation": "CONSUMED: opened exactly once under scoring 1.0.0+cb21f9704ff0",
    "holdout": "PREVIOUSLY OBSERVED: read in the Phase 3B fallback analysis and Phase 4 preflight",
    "NOT_EVALUABLE": "no mapper label exists; not a partition and not a test set",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    cfg = load_split_config()
    psql = cfg.partition_sql_expression("mapper_recording_mbid")
    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()

    def q(sql: str, label: str):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
        rows = [dict(r) for r in job.result()]
        stats.append(
            {
                "step": label,
                "job_id": job.job_id,
                "bytes_billed": job.total_bytes_billed,
                "slot_ms": job.slot_millis,
            }
        )
        print(f"  {label:<52} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    # --- 1. unsupervised: every state transition, no label involved ------------------------
    transitions = q(
        f"""
        SELECT v.match_status AS prior_status, v.match_method AS prior_method,
               v.failure_reason AS prior_reason,
               r.match_status AS new_status, r.match_method AS new_method,
               r.failure_reason AS new_reason,
               COUNT(*) AS listens
        FROM `{V2}` r JOIN `{V1}` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')}
        GROUP BY 1, 2, 3, 4, 5, 6
        HAVING COUNT(*) > 0
        ORDER BY listens DESC
    """,
        "state transitions (unsupervised)",
    )

    changed = [
        t
        for t in transitions
        if (t["prior_status"], t["prior_method"], t["prior_reason"])
        != (t["new_status"], t["new_method"], t["new_reason"])
    ]

    candidates = q(
        f"""
        SELECT r.candidate_count, COUNT(*) AS listens
        FROM `{V2}` r JOIN `{V1}` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')} AND r.recording_cohort
          AND v.match_status != 'MATCHED'
        GROUP BY 1 ORDER BY r.candidate_count
    """,
        "candidate counts for the cohort (unsupervised)",
    )

    collisions = q(
        f"""
        SELECT COUNTIF(r.candidate_count > 1) AS multi_candidate_listens,
               COUNTIF(r.candidate_count = 1) AS unique_candidate_listens,
               COUNTIF(r.candidate_count = 0) AS no_candidate_listens,
               SAFE_DIVIDE(COUNTIF(r.candidate_count > 1),
                           COUNTIF(r.candidate_count >= 1)) AS collision_rate
        FROM `{V2}` r JOIN `{V1}` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')} AND r.recording_cohort
          AND v.match_status != 'MATCHED'
    """,
        "candidate collision rate (unsupervised)",
    )[0]

    # --- 2. the one reference comparison, on the newly matched cohort only -----------------
    reference = q(
        f"""
        WITH labelled AS (
          SELECT l.listen_hash, l.mapper_recording_mbid,
                 {psql} AS eval_partition,
                 (k.recording_mbid IS NOT NULL) AS reference_in_snapshot
          FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels` l
          LEFT JOIN (SELECT recording_mbid
                     FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
                     WHERE snapshot_date = DATE '{SNAPSHOT}') k
            ON k.recording_mbid = l.mapper_recording_mbid
          WHERE l.mapper_recording_mbid IS NOT NULL
        ),
        newly AS (
          SELECT r.listen_hash, r.matched_recording_mbid
          FROM `{V2}` r JOIN `{V1}` v USING (listen_hash)
          WHERE {period('r')} AND {period('v')}
            AND v.match_status != 'MATCHED' AND r.match_status = 'MATCHED'
        )
        SELECT l.eval_partition,
               COUNT(*) AS newly_matched_with_a_label,
               COUNTIF(NOT l.reference_in_snapshot) AS reference_absent_from_snapshot,
               COUNTIF(l.reference_in_snapshot
                       AND n.matched_recording_mbid = l.mapper_recording_mbid) AS agree,
               COUNTIF(l.reference_in_snapshot
                       AND n.matched_recording_mbid != l.mapper_recording_mbid) AS disagree
        FROM newly n JOIN labelled l USING (listen_hash)
        GROUP BY l.eval_partition ORDER BY newly_matched_with_a_label DESC
    """,
        "reference comparison on newly matched listens ONLY",
    )

    newly_total = q(
        f"""
        SELECT COUNT(*) AS newly_matched,
               COUNTIF(r.match_method = 'STRUCTURAL_EXACT_UNIQUE') AS structural,
               COUNTIF(r.match_method = 'SCORED_EXACT_MULTIPLE') AS scored_multiple
        FROM `{V2}` r JOIN `{V1}` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')}
          AND v.match_status != 'MATCHED' AND r.match_status = 'MATCHED'
    """,
        "newly matched total",
    )[0]

    evaluable = sum(int(r["agree"]) + int(r["disagree"]) for r in reference)
    agree = sum(int(r["agree"]) for r in reference)
    disagree = sum(int(r["disagree"]) for r in reference)
    unlabelled = int(newly_total["newly_matched"]) - sum(
        int(r["newly_matched_with_a_label"]) for r in reference
    )

    report = {
        "artifact": "restatement_evaluation",
        "partition_inventory": PARTITION_STATUS,
        "untouched_partition_exists": False,
        "consequence": (
            "no new validation was created: consumed partitions were not reshuffled under a new "
            "name, and no new generalisation-accuracy claim is made"
        ),
        "unsupervised": {
            "state_transitions": transitions,
            "distinct_changed_transitions": len(changed),
            "listens_that_changed_state": sum(int(t["listens"]) for t in changed),
            "newly_matched": int(newly_total["newly_matched"]),
            "newly_matched_by_method": {
                "STRUCTURAL_EXACT_UNIQUE": int(newly_total["structural"]),
                "SCORED_EXACT_MULTIPLE": int(newly_total["scored_multiple"]),
            },
            "cohort_candidate_distribution": candidates,
            "candidate_collision": {
                k: (float(v) if v is not None else None) for k, v in collisions.items()
            },
        },
        "reference_comparison": {
            "scope": "newly matched listens only",
            "by_partition": reference,
            "evaluable": evaluable,
            "agree": agree,
            "disagree": disagree,
            "agreement_rate": round(agree / evaluable, 6) if evaluable else None,
            "newly_matched_without_a_label": unlabelled,
            "reference_absent_from_snapshot": sum(
                int(r["reference_absent_from_snapshot"]) for r in reference
            ),
            "narrow_claim_supported": (
                "these specific labels could not have participated in choosing any rule: under v1 "
                "these listens produced zero candidates, so they never entered the feature table, "
                "the calibration grid or the validation measurement, and the transliteration rule "
                "was frozen before this query ran"
            ),
            "broad_claim_NOT_supported": (
                "this is NOT a blind validation of the restatement. These labels sit inside "
                "partitions that are already consumed, the mapper output is a correlated reference "
                "label rather than ground truth, and no generalisation accuracy is claimed"
            ),
            "status": "CONSUMED by this run",
        },
        "cost": {
            "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
            "list_price_equivalent_usd": round(
                sum(s.get("bytes_billed") or 0 for s in stats) / 1024**4 * ON_DEMAND_USD_PER_TIB, 4
            ),
            "caveat": "list-price equivalent; actual monetary cost UNKNOWN without billing evidence",
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    print("\n  PARTITION INVENTORY")
    for name, status in PARTITION_STATUS.items():
        print(f"    {name:<14} {status}")
    print("    => no untouched reference partition exists; none was manufactured")
    print("\n  UNSUPERVISED")
    print(
        f"    listens that changed state   "
        f"{report['unsupervised']['listens_that_changed_state']:>12,}"
    )
    print(f"    newly matched                {int(newly_total['newly_matched']):>12,}")
    print(
        f"    candidate collision rate     "
        f"{report['unsupervised']['candidate_collision']['collision_rate']}"
    )
    print("\n  REFERENCE COMPARISON (newly matched only; NOT a blind validation)")
    print(
        f"    evaluable {evaluable:,}   agree {agree:,}   disagree {disagree:,}   "
        f"rate {report['reference_comparison']['agreement_rate']}"
    )
    for r in reference:
        print(
            f"      {r['eval_partition']:<14} labelled={int(r['newly_matched_with_a_label']):>10,} "
            f"agree={int(r['agree']):>10,} disagree={int(r['disagree']):>8,} "
            f"ref_absent={int(r['reference_absent_from_snapshot']):>8,}"
        )
    print(f"    newly matched with no label at all: {unlabelled:,}")


if __name__ == "__main__":
    main()
