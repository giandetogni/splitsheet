"""What is still unmatched, by cause and by volume. Operational, not evaluative.

Reads no reference label: this is a description of the published result, so it can be run by
either identity. It exists to answer "what would the next unit of work actually fix", which is
a question about concentration, not about accuracy.

PII: the output carries only SUBMITTED artist and recording strings, which are content, plus
counts. No user_id, no recording_msid, no listen_hash. That is checked, not assumed.
"""

from __future__ import annotations

import argparse
import json
import time

PROJECT = "ss-de-944054e7"
MAX_BYTES = 200 * 1024**3
PERIOD = (
    "listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
    "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
)
EXPECTED_LISTENS = 38_199_641
TOP_N = 20

BANNED_OUTPUT_FIELDS = {"user_id", "recording_msid", "listen_hash", "mapper_recording_mbid"}


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
        rows = [dict(r) for r in job.result()]
        stats.append(
            {
                "step": label,
                "job_id": job.job_id,
                "bytes_billed": job.total_bytes_billed,
                "slot_ms": job.slot_millis,
            }
        )
        print(f"  {label:<30} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    latin = (
        r"CHAR_LENGTH(REGEXP_REPLACE(CONCAT(n.artist_normalized_unicode, "
        r"n.recording_normalized_unicode), r'[^\p{Latin}0-9]', ''))"
    )
    alnum = (
        r"CHAR_LENGTH(REGEXP_REPLACE(CONCAT(n.artist_normalized_unicode, "
        r"n.recording_normalized_unicode), r'[^\p{L}\p{N}]', ''))"
    )

    by_reason = q(
        f"""
        SELECT failure_reason, COUNT(*) AS listens,
               ROUND(100 * COUNT(*) / {EXPECTED_LISTENS}, 4) AS pct_of_all_listens,
               COUNT(DISTINCT FORMAT('%t|%t', block_method, match_method)) AS methods,
               ROUND(AVG(candidate_count), 3) AS avg_candidate_count
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD} AND match_status = 'UNRESOLVED'
        GROUP BY 1 ORDER BY listens DESC
    """,
        "unresolved by reason",
    )

    top = q(
        f"""
        WITH u AS (
          SELECT m.failure_reason, n.artist_name, n.recording_name,
                 IF(SAFE_DIVIDE({latin}, NULLIF({alnum}, 0)) >= 0.5, 'LATIN', 'NON_LATIN')
                   AS script_class,
                 m.candidate_count, m.blocking_key_information_class, m.match_score
          FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` m
          JOIN `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
            USING (listen_hash)
          WHERE m.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
            AND m.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
            AND n.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
            AND n.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
            AND m.match_status = 'UNRESOLVED'
        ),
        g AS (
          SELECT failure_reason, artist_name, recording_name,
                 ANY_VALUE(script_class) AS script_class,
                 MAX(candidate_count) AS candidate_count,
                 ANY_VALUE(blocking_key_information_class) AS information_class,
                 ROUND(AVG(match_score), 4) AS avg_score,
                 COUNT(*) AS listens
          FROM u GROUP BY 1, 2, 3
        ),
        r AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY failure_reason
                                       ORDER BY listens DESC, artist_name, recording_name)
                    AS rank_in_reason,
                 SUM(listens) OVER (PARTITION BY failure_reason) AS reason_listens
          FROM g
        )
        SELECT failure_reason, rank_in_reason, artist_name, recording_name, script_class,
               information_class, candidate_count, avg_score, listens,
               ROUND(100 * listens / reason_listens, 4) AS pct_of_reason,
               ROUND(100 * listens / {EXPECTED_LISTENS}, 6) AS pct_of_all_listens
        FROM r WHERE rank_in_reason <= {TOP_N}
        ORDER BY failure_reason, rank_in_reason
    """,
        "top combinations per reason",
    )

    leaked = BANNED_OUTPUT_FIELDS & (set(top[0]) | set(by_reason[0]))
    if leaked:
        raise SystemExit(f"output would expose {sorted(leaked)}")

    report = {
        "artifact": "top_unmatched",
        "top_n_per_reason": TOP_N,
        "fields_deliberately_absent": sorted(BANNED_OUTPUT_FIELDS),
        "unresolved_by_reason": by_reason,
        "top_combinations": top,
        "bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    print("\nunresolved by reason:")
    for r in by_reason:
        print(
            f"  {r['failure_reason']:<26} {int(r['listens']):>10,}  "
            f"{r['pct_of_all_listens']:>8}%  avg_candidates={r['avg_candidate_count']}"
        )
    for reason in [r["failure_reason"] for r in by_reason]:
        rows = [t for t in top if t["failure_reason"] == reason][:TOP_N]
        share = sum(int(t["listens"]) for t in rows)
        total = next(int(r["listens"]) for r in by_reason if r["failure_reason"] == reason)
        print(
            f"\n== {reason}: top {len(rows)} combinations = {share:,} listens "
            f"({100 * share / total:.4f}% of this reason)"
        )
        for t in rows[:10]:
            a = (t["artist_name"] or "")[:34]
            rec = (t["recording_name"] or "")[:38]
            print(
                f"  {t['rank_in_reason']:>3}. {int(t['listens']):>7,} "
                f"{t['script_class']:<9} c={int(t['candidate_count']):>3} "
                f"{a:<34} | {rec}"
            )


if __name__ == "__main__":
    main()
