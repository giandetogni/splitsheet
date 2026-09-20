"""The Agust D / 해금 case, before and after, without forcing it to work.

This exists because a restatement is easy to describe in aggregate and easy to fake in the specific.
The instruction was to demonstrate this case honestly and to report it plainly if it does NOT resolve
into a safe match. So the script asks the warehouse what happened and prints that, including a
failure.

No PII: raw artist and recording strings are submitted CONTENT, and the output carries no user_id, no
recording_msid and no listen_hash.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from normalization import load_rules, normalize

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 100 * 1024**3

ARTIST = "Agust D"
RECORDING = "해금"
V1_RULES = pathlib.Path(__file__).parents[2] / "config/normalization_rules_v1.0.0.yml"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prior-run-id", required=True)
    ap.add_argument("--new-run-id", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    v1 = load_rules(V1_RULES)
    v2 = load_rules()
    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()

    def q(sql: str, label: str):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
        rows = [dict(r) for r in job.result()]
        stats.append({"step": label, "job_id": job.job_id, "bytes_billed": job.total_bytes_billed})
        print(f"  {label:<44} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    # --- what the two rule sets do to the strings, locally ---------------------------------
    before_norm = normalize(ARTIST, RECORDING, rules=v1)
    after_norm = normalize(ARTIST, RECORDING, rules=v2)

    # --- what the warehouse says, before and after -----------------------------------------
    states = q(
        f"""
        WITH listens AS (
          SELECT listen_hash FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized`
          WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
            AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
            AND artist_name = '{ARTIST}' AND recording_name = '{RECORDING}'
        )
        SELECT 'v1' AS side, COUNT(*) AS listens,
               MIN(m.match_status) AS match_status, MIN(m.match_method) AS match_method,
               MIN(m.failure_reason) AS failure_reason,
               COUNT(DISTINCT m.matched_recording_mbid) AS distinct_recordings,
               MIN(m.matched_recording_mbid) AS matched_recording_mbid,
               MIN(m.candidate_count) AS candidate_count
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` m
        JOIN listens USING (listen_hash)
        WHERE m.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND m.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
        UNION ALL
        SELECT 'v2', COUNT(*),
               MIN(r.match_status), MIN(r.match_method), MIN(r.failure_reason),
               COUNT(DISTINCT r.matched_recording_mbid), MIN(r.matched_recording_mbid),
               MIN(r.candidate_count)
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches_restated` r
        JOIN listens USING (listen_hash)
        WHERE r.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND r.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """,
        "match state before and after",
    )

    by_side = {row["side"]: row for row in states}
    matched_mbid = by_side.get("v2", {}).get("matched_recording_mbid")

    canonical = []
    if matched_mbid:
        canonical = q(
            f"""
            SELECT recording_mbid, artist_credit_name, recording_name, release_name
            FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
            WHERE snapshot_date = DATE '2026-07-17' AND recording_mbid = '{matched_mbid}'
        """,
            "the canonical recording it matched",
        )

    disposition = (
        q(
            f"""
        SELECT attribution_status, payout_eligible, hold_reason, COUNT(*) AS listens,
               MIN(rate_per_stream) AS rate_per_stream, MIN(split_version_id) AS split_version_id
        FROM `{PROJECT}.{DATASET}.int_financial_disposition`
        WHERE recording_mbid = '{matched_mbid}'
        GROUP BY 1, 2, 3
    """,
            "financial disposition after",
        )
        if matched_mbid
        else []
    )

    money = (
        q(
            f"""
        SELECT attribution_run_id, COUNT(*) AS holder_rows,
               SUM(holder_payout) AS total_payout, SUM(attributable_streams) AS streams,
               MIN(rate_per_stream) AS rate_per_stream
        FROM `{PROJECT}.{DATASET}.fct_royalty_attribution`
        WHERE recording_mbid = '{matched_mbid}'
        GROUP BY attribution_run_id
    """,
            "money for this recording, per publication",
        )
        if matched_mbid
        else []
    )

    delta = (
        q(
            f"""
        SELECT change_type, COUNT(*) AS holders, SUM(prior_payout) AS prior_payout,
               SUM(restated_payout) AS restated_payout, SUM(delta) AS delta
        FROM `{PROJECT}.{DATASET}.fct_restatements`
        WHERE recording_mbid = '{matched_mbid}'
        GROUP BY change_type ORDER BY delta DESC
    """,
            "restatement delta for this recording",
        )
        if matched_mbid
        else []
    )

    v1_state = by_side.get("v1", {})
    v2_state = by_side.get("v2", {})
    resolved_safely = (
        v2_state.get("match_status") == "MATCHED"
        and v2_state.get("match_method") == "STRUCTURAL_EXACT_UNIQUE"
    )

    report = {
        "artifact": "concrete_case",
        "case": {
            "artist": ARTIST,
            "recording": RECORDING,
            "note": "submitted content strings; no user_id, recording_msid or listen_hash here",
        },
        "before": {
            "normalization_version": v1.version,
            "normalized_representation": {
                "artist_normalized_unicode": before_norm.artist_normalized_unicode,
                "recording_normalized_unicode": before_norm.recording_normalized_unicode,
                "lookup_exact": before_norm.lookup_exact,
                "exact_key_status": before_norm.exact_key_status.value,
            },
            "listens": int(v1_state.get("listens", 0)),
            "match_status": v1_state.get("match_status"),
            "match_method": v1_state.get("match_method"),
            "failure_reason": v1_state.get("failure_reason"),
            "candidate_count": int(v1_state.get("candidate_count") or 0),
            "payout_state": "no payout: never matched, so it never reached the financial gates",
        },
        "after": {
            "normalization_version": v2.version,
            "transliterated_representation": {
                "recording_transliterated_for_key": "haegeum",
                "artist_normalized_unicode": after_norm.artist_normalized_unicode,
                "recording_normalized_unicode": after_norm.recording_normalized_unicode,
                "note": (
                    "the stored Unicode value is unchanged and still Korean; only the LOOKUP "
                    "KEY is transliterated"
                ),
                "lookup_exact": after_norm.lookup_exact,
                "exact_key_status": after_norm.exact_key_status.value,
            },
            "listens": int(v2_state.get("listens", 0)),
            "match_status": v2_state.get("match_status"),
            "match_method": v2_state.get("match_method"),
            "failure_reason": v2_state.get("failure_reason"),
            "candidate_count": int(v2_state.get("candidate_count") or 0),
            "matched_recording_mbid": matched_mbid,
            "canonical_recording": canonical,
            "financial_disposition": disposition,
            "money_by_publication": money,
            "restatement_delta": delta,
        },
        "resolved_into_a_safe_match": resolved_safely,
        "honest_verdict": (
            "the case resolved to a single canonical recording through a transliterated exact key"
            if resolved_safely
            else "the case did NOT resolve into a safe unique match; reported as measured rather than "
            "forced to work"
        ),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    print(f"\n  BEFORE ({v1.version})")
    print(f"    raw                 {ARTIST} / {RECORDING}")
    print(
        f"    normalized unicode  {before_norm.artist_normalized_unicode} / "
        f"{before_norm.recording_normalized_unicode}"
    )
    print(
        f"    lookup key          {before_norm.lookup_exact!r}  "
        f"({before_norm.exact_key_status.value})"
    )
    print(f"    listens             {int(v1_state.get('listens', 0)):,}")
    print(
        f"    match               {v1_state.get('match_status')} / "
        f"{v1_state.get('failure_reason')}"
    )
    print("    payout              none: never matched")
    print(f"\n  AFTER ({v2.version})")
    print(
        f"    transliterated key  {after_norm.lookup_exact!r}  "
        f"({after_norm.exact_key_status.value})"
    )
    print(
        f"    normalized unicode  {after_norm.recording_normalized_unicode}  "
        f"(unchanged, still Korean)"
    )
    print(f"    listens             {int(v2_state.get('listens', 0)):,}")
    print(f"    candidates          {int(v2_state.get('candidate_count') or 0)}")
    print(
        f"    match               {v2_state.get('match_status')} / "
        f"{v2_state.get('match_method')} -> {matched_mbid}"
    )
    for row in canonical:
        print(f"    canonical           {row['artist_credit_name']} / {row['recording_name']}")
    for row in disposition:
        print(
            f"    disposition         {row['attribution_status']} "
            f"(eligible={row['payout_eligible']}, listens={int(row['listens']):,}, "
            f"rate={row['rate_per_stream']})"
        )
    for row in money:
        print(
            f"    money               {row['attribution_run_id']}: "
            f"{int(row['holder_rows'])} holder rows, {row['total_payout']} over "
            f"{int(row['streams']):,} streams"
        )
    for row in delta:
        print(
            f"    delta               {row['change_type']}: {row['holders']} holders, "
            f"prior {row['prior_payout']} -> restated {row['restated_payout']} "
            f"(delta {row['delta']})"
        )
    print(f"\n  VERDICT: {report['honest_verdict']}")


if __name__ == "__main__":
    main()
