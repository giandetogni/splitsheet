"""Choose weights, thresholds and margins on the calibration partition. Nothing else.

EVALUATION SIDE OF THE FIREWALL: reads splitsheet_eval, so it runs as the human ADC
identity, never as the matcher. The output of this script is a decision recorded in
config/scoring_rules.yml; the matcher then reads only that config.

THE SELECTION RULE IS FIXED BEFORE THE TABLE IS READ, which is the only way a "conservative
choice" means anything:

    Among the evaluated weight sets, exact thresholds, fallback thresholds and margins,
    choose the configuration with the HIGHEST accepted coverage whose disagreement with the
    reference label, among EVALUABLE accepted decisions, is <= 2.0% in EVERY scored stage
    (EXACT_MULTI, FALLBACK_UNIQUE, FALLBACK_MULTI). If no configuration satisfies that,
    report the fact and select the configuration with the lowest worst-stage disagreement
    instead, rather than relaxing the bound to make an answer appear.

EVALUABLE, and why the denominator is not simply "accepted": an accepted decision can only be
judged if the reference recording exists in the canonical snapshot at all. When the mapper
names a recording our 2026-07-17 snapshot does not contain, our candidate may be the same
song under a different MBID and nothing in this data can say. Phase 3B already established
that treatment for LABEL_NOT_IN_CANONICAL_SNAPSHOT; this is the same class, so those listens
leave the bound's denominator and are reported separately instead of being counted as errors.
Every accepted disagreement is therefore split three ways:

    RANKING_ERROR        the reference WAS among the candidates and the score chose another
                         -- the only class that is a scoring failure
    REF_NOT_RETRIEVED    the reference exists in the snapshot but the block never proposed it
                         -- a blocking recall failure, which still risks a wrong payment
    REF_NOT_IN_SNAPSHOT  the reference is not in the snapshot -- not evaluable

The bound covers RANKING_ERROR + REF_NOT_RETRIEVED over evaluable accepted decisions: both
can pay the wrong holder, the third cannot be judged either way.

2.0% is chosen against two measured references: unscored fallback-unique disagreed with the
reference label on 22.96% of dev listens, and structurally decided exact-unique disagreed on
0.0147%. A scored acceptance that disagrees more than 2% would not be worth the complexity;
one at 0.0147% is not reachable from a fallback key.

WHY THE LABEL CANNOT SETTLE THIS ALONE: the ListenBrainz mapper output is a correlated
reference label, not independent ground truth. It shares normalization intuitions with this
pipeline, so "agreement" overstates correctness and the errors are not independent. That is
why the bound is a ceiling on ACCEPTANCE rather than a target to be optimised towards.
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
MAX_ACCEPTED_DISAGREEMENT = 0.02
SCORED_STAGES = ("EXACT_MULTI", "FALLBACK_UNIQUE", "FALLBACK_MULTI")

# Candidate weight sets. Each is a hypothesis to be measured, not a preference: the feature
# analysis found the artist features separating in the WRONG direction on both fallback
# stages (AUC 0.32-0.34) while the recording features held 0.66-0.98, so sets that shrink or
# drop the artist side must be on the list for the comparison to be real.
#
# Order: artist_unicode_exact, recording_unicode_exact, artist_token_similarity,
# recording_token_similarity, artist_string_similarity, recording_string_similarity,
# release_lower_exact.
WEIGHT_SETS: dict[str, tuple[float, ...]] = {
    "equal_six": (1, 1, 1, 1, 1, 1, 0),
    "auc_proportional": (0.29, 0.44, 0.32, 0.46, 0.31, 0.45, 0.43),
    "recording_only": (0, 1, 0, 1, 0, 1, 0),
    "recording_release": (0, 1, 0, 1, 0, 1, 1),
    "recording_heavy": (0.1, 1, 0.1, 1, 0.1, 1, 0.5),
    "recording_release_eq": (0, 0.8, 0, 1, 0, 0.9, 0.9),
}
FEATURE_NAMES = [
    "artist_unicode_exact",
    "recording_unicode_exact",
    "artist_token_similarity",
    "recording_token_similarity",
    "artist_string_similarity",
    "recording_string_similarity",
    "release_lower_exact",
]
FEATURE_EXPRS = [
    "CAST(f.artist_unicode_exact AS INT64)",
    "CAST(f.recording_unicode_exact AS INT64)",
    "f.artist_token_similarity",
    "f.recording_token_similarity",
    "f.artist_string_similarity",
    "f.recording_string_similarity",
    "CAST(f.release_lower_exact AS INT64)",
]

# Extended upward after the first grid: nothing in 0.55-0.85 came near the 2% bound on any
# stage, so the question became whether a strict operating point exists at all rather than
# which loose one to prefer. The bound itself was NOT moved.
EXACT_THRESHOLDS = [0.55, 0.65, 0.75, 0.85, 0.90, 0.95, 0.99]
FALLBACK_THRESHOLDS = [0.65, 0.75, 0.85, 0.90, 0.95, 0.99]
MARGINS = [0.02, 0.05, 0.10, 0.20]

# A stage that accepts almost nothing cannot demonstrate a disagreement rate: at 1,000
# accepted listens a 2% bound is 20 disagreements, which is measurable; at 10 accepted it is
# noise, and at 0 accepted the rate is 0/0, which the first run's selection rule scored as a
# perfect 0% and duly "won" with a configuration that decided nothing. This floor closes that
# degeneracy. It is a fix to the rule's arithmetic, not a relaxation of the 2% bound.
MIN_ACCEPTED_FOR_ELIGIBILITY = 1_000


def weight_struct_array() -> str:
    rows = [
        "STRUCT('"
        + name
        + "' AS weight_set, "
        + ", ".join(f"CAST({v} AS FLOAT64) AS w{i}" for i, v in enumerate(w))
        + ")"
        for name, w in WEIGHT_SETS.items()
    ]
    return "[" + ", ".join(rows) + "]"


def score_expr(prefix: str = "w.w") -> str:
    """Weighted mean over the AVAILABLE features.

    NULL means "this comparison could not be made", not "this comparison failed": 1.4% of
    the calibration pairs have no submitted release. Such a pair drops the feature AND its
    weight from the denominator, so a listen without a release is scored on the features it
    does have instead of being penalised for a field it never sent.
    """
    num = " + ".join(f"IF({e} IS NULL, 0, {prefix}{i} * {e})" for i, e in enumerate(FEATURE_EXPRS))
    den = " + ".join(f"IF({e} IS NULL, 0, {prefix}{i})" for i, e in enumerate(FEATURE_EXPRS))
    return f"SAFE_DIVIDE({num}, NULLIF({den}, 0))"


def per_listen_cte(partition_sql: str, partition: str) -> str:
    """Top-1 and top-2 per listen per weight set, plus the segmentation dimensions."""
    latin = (
        r"CHAR_LENGTH(REGEXP_REPLACE(CONCAT(artist_normalized_unicode, "
        r"recording_normalized_unicode), r'[^\p{Latin}0-9]', ''))"
    )
    alnum = (
        r"CHAR_LENGTH(REGEXP_REPLACE(CONCAT(artist_normalized_unicode, "
        r"recording_normalized_unicode), r'[^\p{L}\p{N}]', ''))"
    )
    return f"""
    WITH labelled AS (
      SELECT l.listen_hash, l.mapper_recording_mbid, {partition_sql} AS eval_partition,
             (k.recording_mbid IS NOT NULL) AS reference_in_snapshot
      FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels` l
      LEFT JOIN (SELECT recording_mbid
                 FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
                 WHERE snapshot_date = DATE '2026-07-17') k
        ON k.recording_mbid = l.mapper_recording_mbid
      WHERE l.mapper_recording_mbid IS NOT NULL
    ),
    listen_script AS (
      SELECT listen_hash,
             IF(SAFE_DIVIDE({latin}, NULLIF({alnum}, 0)) >= 0.5, 'LATIN', 'NON_LATIN')
               AS script_class
      FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
      WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
        AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    ),
    scored AS (
      SELECT f.listen_hash, f.candidate_recording_mbid, f.candidate_count,
             f.blocking_key_information_class, s.script_class,
             CONCAT(f.block_method, IF(f.candidate_count > 1, '_MULTI', '_UNIQUE')) AS stage,
             (f.candidate_recording_mbid = l.mapper_recording_mbid) AS is_ref,
             l.reference_in_snapshot,
             w.weight_set, {score_expr()} AS score
      FROM `{PROJECT}.splitsheet_silver.silver_candidate_features` f
      JOIN labelled l USING (listen_hash)
      JOIN listen_script s USING (listen_hash)
      CROSS JOIN UNNEST({weight_struct_array()}) w
      WHERE l.eval_partition = '{partition}'
    ),
    ranked AS (
      SELECT *,
             -- Deterministic ordering, with the MBID only as a final tie-break so the query
             -- reproduces. It decides NOTHING: an equal top-2 is refused as a tie below,
             -- never awarded to whichever row happened to sort first.
             ROW_NUMBER() OVER (PARTITION BY weight_set, listen_hash
                                ORDER BY score DESC, candidate_recording_mbid) AS rn
      FROM scored
    ),
    per_listen AS (
      SELECT weight_set, listen_hash, stage, candidate_count,
             blocking_key_information_class, script_class, reference_in_snapshot,
             MAX(IF(rn = 1, score, NULL)) AS top1,
             MAX(IF(rn = 2, score, NULL)) AS top2,
             LOGICAL_OR(rn = 1 AND is_ref) AS top1_is_ref,
             LOGICAL_OR(is_ref) AS reference_in_block
      FROM ranked GROUP BY 1, 2, 3, 4, 5, 6, 7
    )"""


def outcome_expr() -> str:
    """The decision policy, as one expression, mirroring src/matching/scoring.py.

    Order matters and is the policy: threshold first, then uniqueness, then tie evidence.
    A single candidate can never be a tie, because there is no top-2 to be close to.
    """
    return """
      CASE WHEN top1 < applied_threshold THEN 'BELOW_THRESHOLD'
           WHEN candidate_count = 1 THEN 'ACCEPTED'
           WHEN (top1 - top2) <= tie_epsilon THEN 'AMBIGUOUS_TIE'
           WHEN (top1 - top2) < min_margin THEN 'AMBIGUOUS_TIE'
           ELSE 'ACCEPTED' END"""


def grid_sql(partition_sql: str, partition: str, tie_epsilon: float) -> str:
    return f"""
    {per_listen_cte(partition_sql, partition)},
    graded AS (
      SELECT p.*, exact_threshold, fallback_threshold, min_margin,
             {tie_epsilon} AS tie_epsilon,
             IF(p.stage = 'EXACT_MULTI', exact_threshold, fallback_threshold)
               AS applied_threshold
      FROM per_listen p
      CROSS JOIN UNNEST({EXACT_THRESHOLDS}) AS exact_threshold
      CROSS JOIN UNNEST({FALLBACK_THRESHOLDS}) AS fallback_threshold
      CROSS JOIN UNNEST({MARGINS}) AS min_margin
    )
    SELECT weight_set, exact_threshold, fallback_threshold, min_margin, stage,
           COUNT(*) AS listens,
           COUNTIF(reference_in_block) AS reference_in_block,
           COUNTIF({outcome_expr()} = 'ACCEPTED') AS accepted,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND top1_is_ref) AS accepted_agree,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref) AS accepted_disagree,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND reference_in_block) AS ranking_error,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND NOT reference_in_block AND reference_in_snapshot)
             AS ref_not_retrieved,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND NOT reference_in_snapshot) AS ref_not_in_snapshot,
           COUNTIF({outcome_expr()} = 'AMBIGUOUS_TIE') AS ambiguous_tie,
           COUNTIF({outcome_expr()} = 'BELOW_THRESHOLD') AS below_threshold
    FROM graded
    GROUP BY 1, 2, 3, 4, 5"""


def breakdown_sql(partition_sql: str, partition: str, chosen: dict, dim: str) -> str:
    return f"""
    {per_listen_cte(partition_sql, partition)},
    graded AS (
      SELECT p.*, {chosen['exact_threshold']} AS exact_threshold,
             {chosen['fallback_threshold']} AS fallback_threshold,
             {chosen['min_margin']} AS min_margin, {chosen['tie_epsilon']} AS tie_epsilon,
             IF(p.stage = 'EXACT_MULTI', {chosen['exact_threshold']},
                {chosen['fallback_threshold']}) AS applied_threshold
      FROM per_listen p
      WHERE p.weight_set = '{chosen['weight_set']}'
    )
    SELECT stage, {dim} AS segment, COUNT(*) AS listens,
           COUNTIF(reference_in_block) AS reference_in_block,
           COUNTIF({outcome_expr()} = 'ACCEPTED') AS accepted,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND top1_is_ref) AS accepted_agree,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref) AS accepted_disagree,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND reference_in_block) AS ranking_error,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND NOT reference_in_block AND reference_in_snapshot)
             AS ref_not_retrieved,
           COUNTIF({outcome_expr()} = 'ACCEPTED' AND NOT top1_is_ref
                   AND NOT reference_in_snapshot) AS ref_not_in_snapshot,
           COUNTIF({outcome_expr()} = 'AMBIGUOUS_TIE') AS ambiguous_tie,
           COUNTIF({outcome_expr()} = 'BELOW_THRESHOLD') AS below_threshold
    FROM graded GROUP BY 1, 2 ORDER BY 1, 2"""


def choose(grid: list[dict]) -> tuple[dict, dict]:
    """Apply the selection rule stated in the module docstring. No judgement calls here."""
    cells: dict[tuple, dict] = {}
    for r in grid:
        key = (r["weight_set"], r["exact_threshold"], r["fallback_threshold"], r["min_margin"])
        c = cells.setdefault(key, {"accepted": 0, "listens": 0, "stages": {}})
        c["accepted"] += int(r["accepted"])
        c["listens"] += int(r["listens"])
        acc, na = int(r["accepted"]), int(r["ref_not_in_snapshot"])
        evaluable = acc - na
        judged = int(r["ranking_error"]) + int(r["ref_not_retrieved"])
        rate = (judged / evaluable) if evaluable else 1.0
        c["stages"][r["stage"]] = {
            "accepted": acc,
            "evaluable_accepted": evaluable,
            "disagreement": rate,
            "total_disagreement": (int(r["accepted_disagree"]) / acc) if acc else None,
            "ranking_error": int(r["ranking_error"]),
            "ref_not_retrieved": int(r["ref_not_retrieved"]),
            "ref_not_in_snapshot": na,
            "agreement": (int(r["accepted_agree"]) / evaluable) if evaluable else None,
            "ambiguous_tie": int(r["ambiguous_tie"]),
            "below_threshold": int(r["below_threshold"]),
        }
    scored = []
    for key, c in cells.items():
        worst = (
            max(
                s["disagreement"]
                for s in c["stages"].values()
                if s["evaluable_accepted"] >= MIN_ACCEPTED_FOR_ELIGIBILITY
            )
            if any(
                s["evaluable_accepted"] >= MIN_ACCEPTED_FOR_ELIGIBILITY
                for s in c["stages"].values()
            )
            else 1.0
        )
        # A cell that accepts nothing in a stage trivially satisfies the bound, so require
        # every scored stage to be present AND to have accepted enough listens for its rate
        # to mean something.
        complete = all(
            s in c["stages"]
            and c["stages"][s]["evaluable_accepted"] >= MIN_ACCEPTED_FOR_ELIGIBILITY
            for s in SCORED_STAGES
        )
        scored.append(
            {
                "key": key,
                "accepted": c["accepted"],
                "worst_disagreement": worst,
                "complete": complete,
                "stages": c["stages"],
            }
        )
    eligible = [
        s for s in scored if s["complete"] and s["worst_disagreement"] <= MAX_ACCEPTED_DISAGREEMENT
    ]
    if eligible:
        best = max(eligible, key=lambda s: (s["accepted"], -s["worst_disagreement"]))
        rule = f"highest accepted coverage with worst-stage disagreement <= {MAX_ACCEPTED_DISAGREEMENT}"
    else:
        best = min(
            [s for s in scored if s["complete"]],
            key=lambda s: (s["worst_disagreement"], -s["accepted"]),
        )
        rule = (
            "NO configuration met the disagreement bound; fell back to the lowest "
            "worst-stage disagreement, bound NOT relaxed"
        )
    ws, et, ft, mm = best["key"]
    return (
        {
            "weight_set": ws,
            "exact_threshold": et,
            "fallback_threshold": ft,
            "min_margin": mm,
            "selection_rule": rule,
            "accepted_listens": best["accepted"],
            "worst_stage_disagreement": best["worst_disagreement"],
            "per_stage": best["stages"],
        },
        {"cells_evaluated": len(cells), "eligible_cells": len(eligible)},
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tie-epsilon", type=float, default=0.01)
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
            f"  {label:<34} billed={job.total_bytes_billed or 0:>13,} "
            f"slot_ms={job.slot_millis or 0:>9,}",
            flush=True,
        )
        return rows

    grid = q(grid_sql(psql, args.partition, args.tie_epsilon), "threshold/weight grid")
    chosen, meta = choose(grid)
    chosen["tie_epsilon"] = args.tie_epsilon
    chosen["weights"] = dict(zip(FEATURE_NAMES, WEIGHT_SETS[chosen["weight_set"]], strict=True))

    by_info = q(
        breakdown_sql(psql, args.partition, chosen, "blocking_key_information_class"),
        "chosen: by information class",
    )
    by_script = q(
        breakdown_sql(psql, args.partition, chosen, "script_class"), "chosen: by script class"
    )

    report = {
        "partition": args.partition,
        "label_status": (
            "correlated reference label, not independent ground truth; "
            "agreement is not absolute matching accuracy"
        ),
        "selection_rule": chosen["selection_rule"],
        "max_accepted_disagreement": MAX_ACCEPTED_DISAGREEMENT,
        "min_evaluable_accepted_for_eligibility": MIN_ACCEPTED_FOR_ELIGIBILITY,
        "disagreement_denominator": (
            "evaluable accepted decisions = accepted minus those "
            "whose reference is absent from the canonical snapshot"
        ),
        "min_accepted_for_eligibility": MIN_ACCEPTED_FOR_ELIGIBILITY,
        "weight_sets_evaluated": {k: list(v) for k, v in WEIGHT_SETS.items()},
        "grid": {
            "exact_thresholds": EXACT_THRESHOLDS,
            "fallback_thresholds": FALLBACK_THRESHOLDS,
            "margins": MARGINS,
            "tie_epsilon": args.tie_epsilon,
            **meta,
        },
        "chosen": chosen,
        "trade_off_table": grid,
        "chosen_by_information_class": by_info,
        "chosen_by_script_class": by_script,
        "bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    print(
        f"\nchosen: {json.dumps({k: chosen[k] for k in
        ('weight_set', 'exact_threshold', 'fallback_threshold', 'min_margin', 'tie_epsilon')})}"
    )
    print(f"  rule: {chosen['selection_rule']}")
    for stage, s in sorted(chosen["per_stage"].items()):
        print(
            f"  {stage:<16} accepted={s['accepted']:>7,} evaluable={s['evaluable_accepted']:>7,} "
            f"disagreement={round(s['disagreement'], 6)} "
            f"(rank_err={s['ranking_error']:,} not_retrieved={s['ref_not_retrieved']:,} "
            f"not_in_snapshot={s['ref_not_in_snapshot']:,}) "
            f"tie={s['ambiguous_tie']:>6,} below={s['below_threshold']:>7,}"
        )
    print("\nby information class:")
    for r in by_info:
        ev = int(r["accepted"]) - int(r["ref_not_in_snapshot"])
        judged = int(r["ranking_error"]) + int(r["ref_not_retrieved"])
        print(
            f"  {r['stage']:<16} {r['segment']:<26} listens={r['listens']:>8,} "
            f"accepted={int(r['accepted']):>7,} evaluable={ev:>7,} "
            f"disagreement={(judged / ev if ev else 0):.6f}"
        )
    print("\nby script class:")
    for r in by_script:
        ev = int(r["accepted"]) - int(r["ref_not_in_snapshot"])
        judged = int(r["ranking_error"]) + int(r["ref_not_retrieved"])
        print(
            f"  {r['stage']:<16} {r['segment']:<11} listens={r['listens']:>8,} "
            f"accepted={int(r['accepted']):>7,} evaluable={ev:>7,} "
            f"disagreement={(judged / ev if ev else 0):.6f}"
        )


if __name__ == "__main__":
    main()
