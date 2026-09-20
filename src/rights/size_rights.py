"""Measure the payout universe and estimate the rights data BEFORE generating any of it.

Read-only against the frozen Phase 4B result. Produces the numbers that decide how big the
modeled rights dataset has to be, so that generating millions of rows is a costed decision
rather than a side effect of running a script.

THE POLICY THIS MEASURES, and it is a policy, not a matching result:

    A technical match is not an authorisation to pay. SCORED_FALLBACK_UNIQUE showed 3.2138%
    disagreement with the reference label on the validation partition against 0.0055% for
    structural acceptance -- materially worse -- and no approved business risk policy exists
    that would justify paying on it. So this first publication classifies that class as
    payout_eligible = false, hold_reason = MATCH_RISK_POLICY.

    The matcher is NOT modified to achieve this, no new threshold is chosen, and this policy is
    NOT presented as validated by the consumed validation partition. It is a business rule
    applied downstream of an unchanged match result, and it is reversible by changing the
    policy alone.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from rights.generator import load_rights_model

PROJECT = "ss-de-944054e7"
MAX_BYTES = 100 * 1024**3
PERIOD = (
    "listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
    "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
)

# The eligibility rule, in one place, as SQL. dbt reads the same rule from
# models/intermediate/int_payout_eligibility.sql; this script exists to size it first.
RISK_HELD_METHODS = ("SCORED_FALLBACK_UNIQUE",)

# Row-size assumptions for turning counts into bytes. The SHAPE of the model is read from
# config/rights_model.yml rather than restated here: the first pass hard-coded 3 recordings per
# holder, estimated 1,647,897 holders for 2,471,846 recordings, and was rejected on inspection
# for modelling a world where nearly every recording has its own publisher. Reading the config
# means the estimate cannot disagree with what the generator will do.
BYTES_PER_SPLIT_ROW = 120
BYTES_PER_HOLDER_ROW = 220
ON_DEMAND_USD_PER_TIB = 6.25


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()

    def q(sql: str, label: str):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
        out = [dict(r) for r in job.result()]
        stats.append(
            {
                "step": label,
                "job_id": job.job_id,
                "bytes_billed": job.total_bytes_billed,
                "slot_ms": job.slot_millis,
            }
        )
        print(f"  {label:<34} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return out

    held = ", ".join(f"'{m}'" for m in RISK_HELD_METHODS)
    universe = q(
        f"""
        SELECT COUNT(*) AS listens,
               COUNTIF(match_status = 'MATCHED') AS matched,
               COUNTIF(match_status != 'MATCHED') AS unmatched,
               COUNTIF(match_status = 'MATCHED' AND match_method NOT IN ({held}))
                 AS payout_eligible_matches,
               COUNTIF(match_status = 'MATCHED' AND match_method IN ({held}))
                 AS risk_held_matches,
               COUNT(DISTINCT matched_recording_mbid) AS distinct_matched_recordings,
               COUNT(DISTINCT IF(match_status = 'MATCHED' AND match_method NOT IN ({held}),
                                 matched_recording_mbid, NULL))
                 AS distinct_eligible_recordings,
               COUNT(DISTINCT IF(match_status = 'MATCHED' AND match_method IN ({held}),
                                 matched_recording_mbid, NULL))
                 AS distinct_risk_held_recordings
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
    """,
        "payout universe",
    )[0]

    by_method = q(
        f"""
        SELECT match_method, match_status,
               match_method IN ({held}) AS risk_held,
               COUNT(*) AS listens,
               COUNT(DISTINCT matched_recording_mbid) AS distinct_recordings
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD}
        GROUP BY 1, 2, 3 ORDER BY listens DESC
    """,
        "by match_method",
    )

    # The aggregation the temporal join will actually run against: streams per recording per
    # day, not per listen. This is what sizes the ownership join, and it is much smaller than
    # the listen count.
    grain = q(
        f"""
        SELECT COUNT(*) AS recording_days,
               SUM(streams) AS streams,
               APPROX_QUANTILES(streams, 100)[OFFSET(50)] AS median_streams_per_recording_day,
               MAX(streams) AS max_streams_per_recording_day
        FROM (
          SELECT matched_recording_mbid, DATE(listened_at) AS listen_date,
                 COUNT(*) AS streams
          FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
          WHERE {PERIOD} AND match_status = 'MATCHED'
            AND match_method NOT IN ({held})
          GROUP BY 1, 2)
    """,
        "eligible recording-days",
    )[0]

    overlap = q(
        f"""
        SELECT COUNT(*) AS recordings_in_both_groups FROM (
          SELECT matched_recording_mbid
          FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
          WHERE {PERIOD} AND match_status = 'MATCHED'
          GROUP BY 1
          HAVING COUNTIF(match_method IN ({held})) > 0
             AND COUNTIF(match_method NOT IN ({held})) > 0)
    """,
        "recordings in both groups",
    )[0]

    # Rights are generated for every distinctly matched recording, eligible or risk-held: a
    # recording held for match risk today becomes eligible under a future policy with no
    # regeneration, and some recordings are already in both groups.
    model = load_rights_model()
    recordings = int(universe["distinct_matched_recordings"])
    holders_mean = sum(
        n * w
        for n, w in zip(
            model.holders_per_recording, model.holders_per_recording_weights, strict=True
        )
    )
    intervals_mean = 1.0 + model.change_share_of_recordings
    holders = model.holder_count
    split_rows = round(recordings * holders_mean * intervals_mean)
    defect_rows = sum(model.defect_counts.values())

    estimate = {
        "rights_model_version": model.version,
        "rights_generated_for": "every distinctly matched recording, eligible or risk-held",
        "why": (
            "a recording held for match risk today becomes eligible under a future policy "
            "with no rights regeneration, and "
            f"{int(overlap['recordings_in_both_groups']):,} recordings already appear in "
            "both groups"
        ),
        "expected_rights_holders": holders,
        "expected_ownership_split_rows": split_rows,
        "expected_defect_recordings": defect_rows,
        "expected_rate_card_rows": len(model.rate_intervals),
        "recordings_per_holder_mean": round(recordings / model.assignable_holders, 2),
        "estimated_ownership_bytes": split_rows * BYTES_PER_SPLIT_ROW,
        "estimated_holder_bytes": holders * BYTES_PER_HOLDER_ROW,
        "estimated_total_source_bytes": (
            split_rows * BYTES_PER_SPLIT_ROW + holders * BYTES_PER_HOLDER_ROW
        ),
        "estimated_temporal_join_input_bytes": (
            int(grain["recording_days"]) * 60 + split_rows * BYTES_PER_SPLIT_ROW
        ),
        "assumptions": {
            "holders_per_recording_mean": round(holders_mean, 4),
            "intervals_per_ownership_mean": round(intervals_mean, 4),
            "holder_count_source": "config/rights_model.yml holders.count",
            "bytes_per_split_row": BYTES_PER_SPLIT_ROW,
            "bytes_per_holder_row": BYTES_PER_HOLDER_ROW,
        },
        "rejected_first_hypothesis": {
            "expected_rights_holders": 1647897,
            "assumed_recordings_per_holder": 3.0,
            "why_rejected": (
                "1.6M holders for 2.47M recordings models a catalogue where "
                "almost every recording has its own publisher; real catalogues "
                "concentrate, so the holder count was set explicitly to 60,000 "
                "with a documented skew and the estimate redone"
            ),
        },
    }
    est_tib = (
        estimate["estimated_total_source_bytes"]
        + estimate["estimated_temporal_join_input_bytes"] * 4
    ) / 1024**4
    estimate["estimated_materialisation_and_query_tib"] = round(est_tib, 6)
    estimate["estimated_list_price_equivalent_usd"] = round(est_tib * ON_DEMAND_USD_PER_TIB, 4)
    estimate["cost_caveat"] = (
        "list-price equivalent of processing consumption; actual "
        "monetary cost UNKNOWN without billing evidence"
    )

    report = {
        "artifact": "rights_sizing",
        "read_only": True,
        "source": "splitsheet_silver.silver_listen_matches (frozen Phase 4B result)",
        "policy": {
            "principle": "a technical match is not an authorisation to pay",
            "risk_held_methods": list(RISK_HELD_METHODS),
            "hold_reason": "MATCH_RISK_POLICY",
            "basis": (
                "validation-partition disagreement 3.2138% for SCORED_FALLBACK_UNIQUE "
                "against 0.0055% for STRUCTURAL_EXACT_UNIQUE, and no approved business "
                "risk policy for paying on it"
            ),
            "not_validated_by": (
                "the consumed validation partition; this is a business rule "
                "applied downstream of an unchanged matcher"
            ),
            "matcher_modified": False,
            "new_threshold_chosen": False,
        },
        "universe": {k: int(v) for k, v in dict(universe).items()},
        "by_method": by_method,
        "eligible_recording_days": {k: int(v) for k, v in dict(grain).items()},
        "recordings_in_both_groups": int(overlap["recordings_in_both_groups"]),
        "estimate_before_generation": estimate,
        "bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    u = report["universe"]
    print(f"\n  listens                     {u['listens']:>12,}")
    print(f"  matched                     {u['matched']:>12,}")
    print(f"  unmatched                   {u['unmatched']:>12,}")
    print(
        f"  payout-eligible matches     {u['payout_eligible_matches']:>12,}"
        f"  ({100 * u['payout_eligible_matches'] / u['listens']:.4f}% of listens)"
    )
    print(
        f"  risk-held matches           {u['risk_held_matches']:>12,}"
        f"  ({100 * u['risk_held_matches'] / u['listens']:.4f}%)"
    )
    print(
        f"  distinct recordings: matched {u['distinct_matched_recordings']:>11,}  "
        f"eligible {u['distinct_eligible_recordings']:,}  "
        f"risk-held {u['distinct_risk_held_recordings']:,}  "
        f"in both {report['recordings_in_both_groups']:,}"
    )
    g = report["eligible_recording_days"]
    print(
        f"  eligible recording-days     {g['recording_days']:>12,}  "
        f"streams {g['streams']:,}  median {g['median_streams_per_recording_day']}"
    )
    e = estimate
    print(f"\n  ESTIMATE BEFORE GENERATION  (model {e['rights_model_version']})")
    print(
        f"    rights holders            {e['expected_rights_holders']:>12,}"
        f"   ~{e['recordings_per_holder_mean']} recordings per assignable holder"
    )
    print(f"    ownership split rows      {e['expected_ownership_split_rows']:>12,}")
    print(f"    deliberate defect rows    {e['expected_defect_recordings']:>12,}")
    print(f"    source bytes              {e['estimated_total_source_bytes']:>12,}")
    print(
        f"    materialisation + queries {e['estimated_materialisation_and_query_tib']} TiB "
        f"~ ${e['estimated_list_price_equivalent_usd']} list-price equivalent"
    )


if __name__ == "__main__":
    main()
