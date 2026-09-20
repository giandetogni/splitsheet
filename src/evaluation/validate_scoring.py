"""Evaluate the PUBLISHED match results. The validation partition is opened exactly once.

EVALUATION SIDE OF THE FIREWALL: reads splitsheet_eval as the human ADC identity. It reads
the published table rather than recomputing anything, so what is measured here is what was
shipped.

RULES OF THIS SCRIPT, which are the point of it:

  * config/scoring_rules.yml must already be frozen. The digest is verified on load, and the
    version that produced the published rows is compared against the version on disk. A
    mismatch stops the run: evaluating rows produced by a different configuration than the one
    being reported would make the number meaningless.
  * The validation partition is consumed by this run. If the result is unsatisfactory, the
    remedy is a NEW scoring_version plus a new validation strategy or new data -- not a
    retuned threshold measured on the same partition.
  * The original holdout is reported as a PREVIOUSLY OBSERVED evaluation partition. It was
    inspected in the Phase 3B fallback analysis and the Phase 4 preflight, so it is history,
    not a blind test.
  * The ListenBrainz mapper output is a correlated reference label, not independent ground
    truth. Agreement metrics must not be presented as absolute matching accuracy.

Disagreement is decomposed, because the three causes have different owners:

    RANKING_ERROR        reference was among the candidates, the score chose another
    REF_NOT_RETRIEVED    reference exists in the snapshot, blocking never proposed it
    REF_NOT_IN_SNAPSHOT  reference absent from the canonical snapshot -- not evaluable
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from evaluation.split import load_split_config
from matching.scoring import load_scoring_rules

PROJECT = "ss-de-944054e7"
MAX_BYTES = 200 * 1024**3
PERIOD = (
    "listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
    "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
)
SNAPSHOT = "2026-07-17"

PARTITION_LABELS = {
    "validation": "blind test partition, opened exactly once under this scoring_version",
    "calibration": "partition the weights and thresholds were chosen on -- not a test",
    "holdout": "PREVIOUSLY OBSERVED evaluation partition, reported as history",
}


def metrics_sql(
    partition_sql: str, partition: str, candidate_run_id: str, select_expr: str, group_alias: str
) -> str:
    return f"""
    WITH labelled AS (
      SELECT l.listen_hash, l.mapper_recording_mbid,
             {partition_sql} AS eval_partition,
             (k.recording_mbid IS NOT NULL) AS reference_in_snapshot
      FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels` l
      LEFT JOIN (SELECT recording_mbid
                 FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
                 WHERE snapshot_date = DATE '{SNAPSHOT}') k
        ON k.recording_mbid = l.mapper_recording_mbid
      WHERE l.mapper_recording_mbid IS NOT NULL
    ),
    ref_in_block AS (
      SELECT c.listen_hash, LOGICAL_OR(c.candidate_recording_mbid = l.mapper_recording_mbid)
               AS reference_in_block
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates` c
      JOIN labelled l USING (listen_hash)
      WHERE c.candidate_run_id = '{candidate_run_id}'
      GROUP BY c.listen_hash
    ),
    joined AS (
      SELECT m.*, l.mapper_recording_mbid, l.reference_in_snapshot,
             IFNULL(b.reference_in_block, FALSE) AS reference_in_block,
             (m.matched_recording_mbid = l.mapper_recording_mbid) AS agrees
      FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` m
      JOIN labelled l USING (listen_hash)
      LEFT JOIN ref_in_block b USING (listen_hash)
      WHERE {PERIOD} AND l.eval_partition = '{partition}'
    )
    SELECT {select_expr},
           COUNT(*) AS labelled_listens,
           COUNTIF(reference_in_block) AS reference_in_block,
           COUNTIF(NOT reference_in_snapshot) AS reference_not_in_snapshot,
           COUNTIF(match_status = 'MATCHED') AS matched,
           COUNTIF(match_status = 'MATCHED' AND reference_in_snapshot) AS matched_evaluable,
           COUNTIF(match_status = 'MATCHED' AND agrees) AS agree,
           COUNTIF(match_status = 'MATCHED' AND NOT agrees AND reference_in_block)
             AS ranking_error,
           COUNTIF(match_status = 'MATCHED' AND NOT agrees AND NOT reference_in_block
                   AND reference_in_snapshot) AS ref_not_retrieved,
           COUNTIF(match_status = 'MATCHED' AND NOT agrees AND NOT reference_in_snapshot)
             AS ref_not_in_snapshot,
           COUNTIF(failure_reason = 'AMBIGUOUS_TIE') AS ambiguous_tie,
           COUNTIF(failure_reason = 'BELOW_THRESHOLD') AS below_threshold,
           COUNTIF(match_status = 'UNRESOLVED' AND reference_in_block)
             AS unresolved_with_reference_in_block,
           ROUND(AVG(match_score), 6) AS avg_score
    FROM joined
    GROUP BY {group_alias} ORDER BY {group_alias}"""


def rates(r: dict) -> dict:
    matched, na = int(r["matched"]), int(r["ref_not_in_snapshot"])
    evaluable = matched - na
    judged = int(r["ranking_error"]) + int(r["ref_not_retrieved"])
    return {
        "coverage": (
            round(matched / int(r["labelled_listens"]), 6) if r["labelled_listens"] else None
        ),
        "evaluable_accepted": evaluable,
        "agreement": round(int(r["agree"]) / evaluable, 6) if evaluable else None,
        "disagreement": round(judged / evaluable, 6) if evaluable else None,
        "ranking_error": int(r["ranking_error"]),
        "ref_not_retrieved": int(r["ref_not_retrieved"]),
        "ref_not_in_snapshot": na,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-run-id", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    rules = load_scoring_rules()
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
        print(f"  {label:<40} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    published = q(
        f"""
        SELECT DISTINCT scoring_version, match_run_id, candidate_run_id
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` WHERE {PERIOD}
    """,
        "published version check",
    )
    if len(published) != 1:
        raise SystemExit(f"expected exactly one published run, found {published}")
    if published[0]["scoring_version"] != rules.version:
        raise SystemExit(
            f"published rows were scored under {published[0]['scoring_version']} but "
            f"config/scoring_rules.yml is now {rules.version}: refusing to report a metric "
            f"for a configuration that did not produce the rows"
        )

    report = {
        "opened_once": True,
        "primary_partition": "validation",
        "scoring_version": rules.version,
        "match_run_id": published[0]["match_run_id"],
        "candidate_run_id": args.candidate_run_id,
        "partition_status": PARTITION_LABELS,
        "label_status": (
            "correlated reference label, not independent ground truth; "
            "agreement is not absolute matching accuracy"
        ),
        "partitions": {},
    }

    for partition in ("validation", "calibration", "holdout"):
        overall = q(
            metrics_sql(psql, partition, args.candidate_run_id, "'ALL' AS scope", "scope"),
            f"{partition}: overall",
        )
        by_method = q(
            metrics_sql(psql, partition, args.candidate_run_id, "match_method", "match_method"),
            f"{partition}: by match_method",
        )
        by_info = q(
            metrics_sql(
                psql,
                partition,
                args.candidate_run_id,
                "blocking_key_information_class",
                "blocking_key_information_class",
            ),
            f"{partition}: by information class",
        )
        report["partitions"][partition] = {
            "status": PARTITION_LABELS[partition],
            "overall": {"raw": overall[0], "rates": rates(overall[0])},
            "by_method": [
                {"match_method": r["match_method"], "raw": r, "rates": rates(r)} for r in by_method
            ],
            "by_information_class": [
                {"class": r["blocking_key_information_class"], "raw": r, "rates": rates(r)}
                for r in by_info
            ],
        }

    report["bytes_billed"] = sum(s["bytes_billed"] or 0 for s in stats)
    report["slot_ms"] = sum(s["slot_ms"] or 0 for s in stats)
    report["wall_seconds"] = round(time.time() - t0, 1)
    report["jobs"] = stats
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    for partition in ("validation", "calibration", "holdout"):
        p = report["partitions"][partition]
        o, rt = p["overall"]["raw"], p["overall"]["rates"]
        print(f"\n== {partition.upper()}  ({p['status']})")
        print(
            f"  labelled listens {int(o['labelled_listens']):>10,}   "
            f"coverage {rt['coverage']}   evaluable accepted {rt['evaluable_accepted']:,}"
        )
        print(
            f"  agreement {rt['agreement']}   disagreement {rt['disagreement']}   "
            f"(ranking_error={rt['ranking_error']:,} not_retrieved={rt['ref_not_retrieved']:,} "
            f"not_in_snapshot={rt['ref_not_in_snapshot']:,})"
        )
        print(
            f"  ambiguous_tie {int(o['ambiguous_tie']):,}   "
            f"below_threshold {int(o['below_threshold']):,}   "
            f"unresolved_with_reference_in_block "
            f"{int(o['unresolved_with_reference_in_block']):,}"
        )
        for m in p["by_method"]:
            r, mr = m["raw"], m["rates"]
            print(
                f"    {m['match_method']:<25} listens={int(r['labelled_listens']):>9,} "
                f"matched={int(r['matched']):>9,} evaluable={mr['evaluable_accepted']:>9,} "
                f"agreement={mr['agreement']} disagreement={mr['disagreement']}"
            )


if __name__ == "__main__":
    main()
