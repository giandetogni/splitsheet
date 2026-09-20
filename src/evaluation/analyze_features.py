"""Measure each feature against the mapper reference label, on the calibration partition.

EVALUATION SIDE OF THE FIREWALL. Runs as the human ADC identity because it reads
splitsheet_eval, which the matcher identity cannot. Nothing here writes to silver, and no
output of this script is an input to candidate generation.

THE LABEL IS NOT GROUND TRUTH. The ListenBrainz mapper output is a correlated reference
label, not independent ground truth. Agreement metrics must not be presented as absolute
matching accuracy. A feature that "separates" here separates reference candidates from other
candidates; where the mapper is itself wrong, the feature is being rewarded for agreeing
with a mistake, and the correlation between our normalization and theirs makes that error
non-random.

Partition discipline: calibration only. The validation partition is opened exactly once,
after config/scoring_rules.yml is frozen, by validate_scoring.py. The original holdout is a
previously observed evaluation partition and is reported as history, never as a blind test.

SEPARATION MEASURE: AUC computed exactly, with ties counted as half a win:

    AUC = sum_v [ pos_v * (neg_below_v + 0.5 * neg_v) ] / (pos_total * neg_total)

which is the probability that a reference candidate scores above a non-reference candidate
for the same listen population. 0.5 is no separation; below 0.5 is separation in the wrong
direction.
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

SCORED_FEATURES = [
    ("artist_unicode_exact", "CAST(artist_unicode_exact AS INT64)"),
    ("recording_unicode_exact", "CAST(recording_unicode_exact AS INT64)"),
    ("artist_token_similarity", "artist_token_similarity"),
    ("recording_token_similarity", "recording_token_similarity"),
    ("artist_string_similarity", "artist_string_similarity"),
    ("recording_string_similarity", "recording_string_similarity"),
]
PROBE_FEATURES = [("release_lower_exact", "CAST(release_lower_exact AS INT64)")]


def base_cte(partition_sql: str, partition: str) -> str:
    """Calibration pairs joined to the reference label. The label enters HERE and only here.

    `is_ref` is TRUE when the candidate is the recording the mapper named. Listens whose
    reference is absent from the candidate set contribute only negatives, which is honest:
    blocking cannot propose what it never indexed, and hiding those rows would inflate every
    number below.
    """
    return f"""
    WITH labelled AS (
      SELECT listen_hash, mapper_recording_mbid,
             {partition_sql} AS eval_partition
      FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`
      WHERE mapper_recording_mbid IS NOT NULL
    ),
    base AS (
      SELECT f.*, (f.candidate_recording_mbid = l.mapper_recording_mbid) AS is_ref,
             CONCAT(f.block_method, IF(f.candidate_count > 1, '_MULTI', '_UNIQUE'))
               AS stage,
             l.eval_partition
      FROM `{PROJECT}.splitsheet_silver.silver_candidate_features` f
      JOIN labelled l USING (listen_hash)
      WHERE l.eval_partition = '{partition}'
    )"""


def auc_sql(
    expr: str, strata: str, partition_sql: str, partition: str, extra_filter: str = "TRUE"
) -> str:
    """Exact rank-free AUC per stratum, ties counted as 0.5."""
    return f"""
    {base_cte(partition_sql, partition)},
    d AS (
      SELECT {strata} AS stratum, {expr} AS val,
             COUNTIF(is_ref) AS p, COUNTIF(NOT is_ref) AS n,
             COUNTIF({expr} IS NULL) AS null_count
      FROM base WHERE {extra_filter}
      GROUP BY 1, 2
    ),
    c AS (
      SELECT *, SUM(n) OVER (PARTITION BY stratum ORDER BY val
                             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS n_below
      FROM d
    )
    SELECT stratum, SUM(p) AS reference_pairs, SUM(n) AS other_pairs,
           SUM(null_count) AS null_values,
           SAFE_DIVIDE(SUM(p * (IFNULL(n_below, 0) + 0.5 * n)),
                       SUM(p) * SUM(n)) AS auc
    FROM c GROUP BY 1 ORDER BY 1"""


def dist_sql(expr: str, partition_sql: str, partition: str) -> str:
    return f"""
    {base_cte(partition_sql, partition)}
    SELECT is_ref, COUNT(*) AS pairs,
           COUNTIF({expr} IS NULL) AS null_count,
           ROUND(AVG({expr}), 6) AS mean,
           ROUND(APPROX_QUANTILES({expr}, 10)[OFFSET(1)], 6) AS p10,
           ROUND(APPROX_QUANTILES({expr}, 10)[OFFSET(5)], 6) AS p50,
           ROUND(APPROX_QUANTILES({expr}, 10)[OFFSET(9)], 6) AS p90
    FROM base GROUP BY is_ref ORDER BY is_ref"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--partition", default="calibration")
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
        print(
            f"  {label:<46} billed={job.total_bytes_billed or 0:>13,} "
            f"slot_ms={job.slot_millis or 0:>9,}",
            flush=True,
        )
        return rows

    report: dict = {
        "partition_analysed": args.partition,
        "split_version": cfg.split_version,
        "calibration_split_version": cfg.calibration_split_version,
        "label_status": (
            "correlated reference label, not independent ground truth; "
            "agreement is not absolute matching accuracy"
        ),
    }

    # Partition sizes first: the freeze is only meaningful if the partitions are disjoint
    # and non-trivial, and that has to be shown, not asserted.
    report["partitions"] = q(
        f"""
        SELECT {psql} AS eval_partition,
               COUNT(*) AS labelled_listens,
               COUNT(DISTINCT mapper_recording_mbid) AS distinct_reference_recordings
        FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`
        WHERE mapper_recording_mbid IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """,
        "partition sizes",
    )

    report["overlap_check"] = q(
        f"""
        SELECT COUNT(*) AS recordings_in_both_partitions FROM (
          SELECT mapper_recording_mbid
          FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`
          WHERE mapper_recording_mbid IS NOT NULL
          GROUP BY 1
          HAVING COUNT(DISTINCT {psql}) > 1)
    """,
        "calibration/validation disjointness",
    )

    report["universe"] = q(
        f"""
        {base_cte(psql, args.partition)}
        SELECT stage, blocking_key_information_class, COUNT(*) AS pairs,
               COUNT(DISTINCT listen_hash) AS listens, COUNTIF(is_ref) AS reference_pairs
        FROM base GROUP BY 1, 2 ORDER BY 1, 2
    """,
        "calibration universe",
    )

    report["features"] = {}
    for name, expr in SCORED_FEATURES + PROBE_FEATURES:
        report["features"][name] = {
            "distribution": q(dist_sql(expr, psql, args.partition), f"{name}: distribution"),
            "auc_overall": q(auc_sql(expr, "'ALL'", psql, args.partition), f"{name}: AUC all"),
            "auc_by_stage": q(
                auc_sql(expr, "stage", psql, args.partition), f"{name}: AUC by stage"
            ),
            "auc_by_information_class": q(
                auc_sql(expr, "blocking_key_information_class", psql, args.partition),
                f"{name}: AUC by info class",
            ),
        }

    # Incremental gain: AUC of the equal-weight sum, then the same sum with one feature
    # removed. A feature whose removal does not move the AUC is not carrying anything the
    # others do not already carry.
    def equal_weight_expr(exclude: str | None) -> str:
        used = [e for n, e in SCORED_FEATURES if n != exclude]
        return f"({' + '.join(used)}) / {len(used)}"

    report["combined"] = {
        "all_features": q(
            auc_sql(equal_weight_expr(None), "'ALL'", psql, args.partition), "equal-weight sum: AUC"
        )
    }
    for name, _ in SCORED_FEATURES:
        report["combined"][f"without_{name}"] = q(
            auc_sql(equal_weight_expr(name), "'ALL'", psql, args.partition),
            f"equal-weight sum without {name}",
        )

    report["bytes_billed"] = sum(s["bytes_billed"] or 0 for s in stats)
    report["slot_ms"] = sum(s["slot_ms"] or 0 for s in stats)
    report["wall_seconds"] = round(time.time() - t0, 1)
    report["jobs"] = stats
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    print("\npartitions (labelled listens):")
    for r in report["partitions"]:
        print(
            f"  {r['eval_partition']:<16} listens={r['labelled_listens']:>10,} "
            f"recordings={r['distinct_reference_recordings']:>9,}"
        )
    print(
        f"  recordings in more than one partition: "
        f"{report['overlap_check'][0]['recordings_in_both_partitions']}"
    )

    print(f"\nfeature AUC on {args.partition} (0.5 = no separation):")
    for name, _ in SCORED_FEATURES + PROBE_FEATURES:
        f = report["features"][name]
        a = f["auc_overall"][0]
        by_stage = {r["stratum"]: r["auc"] for r in f["auc_by_stage"]}
        by_info = {r["stratum"]: r["auc"] for r in f["auc_by_information_class"]}
        nulls = sum(int(d["null_count"]) for d in f["distribution"])
        pairs = sum(int(d["pairs"]) for d in f["distribution"])
        print(
            f"  {name:<28} auc={a['auc']}  null_rate={nulls / max(pairs, 1):.6f}  "
            f"ref={a['reference_pairs']:,} other={a['other_pairs']:,}"
        )
        print(
            f"    {'by stage':<12} "
            + "  ".join(
                f"{k}={v if v is None else round(v, 4)}" for k, v in sorted(by_stage.items())
            )
        )
        print(
            f"    {'by info':<12} "
            + "  ".join(
                f"{k}={v if v is None else round(v, 4)}" for k, v in sorted(by_info.items())
            )
        )

    print("\nincremental gain (equal-weight sum):")
    allauc = report["combined"]["all_features"][0]["auc"]
    print(f"  all six features            auc={allauc}")
    for name, _ in SCORED_FEATURES:
        a = report["combined"][f"without_{name}"][0]["auc"]
        print(f"  without {name:<26} auc={a}  delta={round((allauc or 0) - (a or 0), 6)}")


if __name__ == "__main__":
    main()
