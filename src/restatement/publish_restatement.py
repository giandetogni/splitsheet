"""Publish the restated financial statement and the delta, leaving v1 provably untouched.

WHAT THIS DOES NOT DO, and the list matters more than the list of what it does:

  * it does not change the payout policy, the ownership rules, the rate card, the rounding, or the
    largest-remainder tiebreak. Every one of those is read from the same frozen config that produced
    v1, and a mismatch stops the run;
  * it does not modify, update or delete a single row of the v1 publication;
  * it does not choose anything based on how much money moves.

The only input that differs from v1 is the match result: the restated table, built under
normalization 1.1.0 for the affected cohort and copied from v1 for everything else. The same
financial gates then run over it. That is what makes the delta attributable to the trigger rather
than to a policy change wearing a restatement's clothes.

Every amount is an illustrative modeled amount over real listening events. The rate card and
ownership are MODELED.

PROOFS PRODUCED HERE:
  1. v1's content digest is identical before and after the whole operation;
  2. v1's original published_at is unchanged;
  3. v2 is a separate publication with its own attribution_run_id;
  4. the pointer can move to v2, and v1 stays queryable while it is;
  5. SUM(delta) equals the difference between the two portfolio totals, exactly;
  6. no listen disappeared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from payout.digest import publication_digest_sql
from payout.policy import attribution_run_id, load_payout_policy

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 200 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25
REPO = pathlib.Path(__file__).parents[2]
DBT_DIR = REPO / "dbt"
DBT_BIN = REPO / ".venv/bin/dbt"

FACT = f"{PROJECT}.{DATASET}.fct_royalty_attribution"
REGISTRY = f"{PROJECT}.{DATASET}.publication_registry"
POINTER = f"{PROJECT}.{DATASET}.publication_pointer"
RESTATED_MATCHES = f"{PROJECT}.splitsheet_silver.silver_listen_matches_restated"

PRIOR_PUBLICATION_ID = "pub:v1"
NEW_PUBLICATION_ID = "pub:v2"
TRIGGER_REASON = "TRANSLITERATION_KOREAN_JAPANESE_TITLES"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--restatement-run-id", required=True)
    ap.add_argument("--new-normalization-version", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    policy = load_payout_policy()
    prior_run = attribution_run_id(policy, "PUBLISHED")
    # The restated publication's id includes the normalization version, so it is deterministic and
    # cannot collide with v1 even though the payout policy is identical.
    new_run = "attr:" + hashlib.sha256(
        f"{policy.version}|{args.new_normalization_version}|{args.restatement_run_id}"
        f"|RESTATED".encode()).hexdigest()[:16]
    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()

    print(f"restating {PRIOR_PUBLICATION_ID} ({prior_run}) -> {NEW_PUBLICATION_ID} ({new_run})",
          flush=True)
    print(f"  payout policy UNCHANGED: {policy.version}", flush=True)
    print(f"  normalization {policy.scoring_version} scoring UNCHANGED; normalization moved to "
          f"{args.new_normalization_version}", flush=True)

    def q(sql: str, label: str):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=MAX_BYTES, use_query_cache=False))
        rows = [dict(r) for r in job.result()]
        stats.append({"step": label, "job_id": job.job_id,
                      "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
                      "duration_ms": int((job.ended - job.started).total_seconds() * 1000)})
        print(f"  {label:<50} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    def dbt(cmd: list[str], dbt_vars: dict, label: str) -> dict:
        proc = subprocess.run([str(DBT_BIN), *cmd, "--vars", json.dumps(dbt_vars)],
                              cwd=DBT_DIR, env={**os.environ, "DBT_PROFILES_DIR": str(DBT_DIR)},
                              capture_output=True, text=True, check=False, timeout=5400)
        tail = (proc.stdout or "")[-4000:]
        done = [ln for ln in tail.splitlines() if "Done." in ln]
        print(f"  dbt {label:<44} exit={proc.returncode}  "
              f"{done[-1].strip()[-60:] if done else ''}", flush=True)
        if proc.returncode != 0:
            print(tail[-2500:], flush=True)
            raise SystemExit(f"dbt {label} failed with exit {proc.returncode}")
        return {"label": label, "exit_code": proc.returncode,
                "summary": done[-1].strip() if done else None}

    # --- 0. the baseline, and its digest BEFORE anything happens ---------------------------
    baseline = q(f"""
        SELECT publication_id, attribution_run_id, content_digest, row_count, portfolio_paid,
               published_at, frozen
        FROM `{REGISTRY}` WHERE publication_id = '{PRIOR_PUBLICATION_ID}'
    """, "read the frozen baseline from the registry")[0]
    if not baseline["frozen"]:
        raise SystemExit("the baseline is not marked frozen; refusing to restate against it")
    digest_before = q(publication_digest_sql(FACT, prior_run), "v1 digest BEFORE")[0]
    if digest_before["content_digest"] != baseline["content_digest"]:
        raise SystemExit(f"v1 has already changed: registry {baseline['content_digest']} vs "
                         f"actual {digest_before['content_digest']}")

    # The restated match table must exist and cover every listen, or the gates would silently
    # produce a smaller publication.
    restated = q(f"""
        SELECT COUNT(*) AS listens, COUNT(DISTINCT listen_hash) AS distinct_listens,
               MIN(normalization_version) AS normalization_version,
               MIN(scoring_version) AS scoring_version,
               COUNTIF(recording_cohort) AS cohort_rows
        FROM `{RESTATED_MATCHES}`
        WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """, "verify the restated match table")[0]
    if int(restated["listens"]) != 38_199_641 or int(restated["distinct_listens"]) != 38_199_641:
        raise SystemExit(f"restated matches hold {restated['listens']} rows; expected 38,199,641")
    if restated["normalization_version"] != args.new_normalization_version:
        raise SystemExit(f"restated matches carry {restated['normalization_version']}, expected "
                         f"{args.new_normalization_version}")
    if restated["scoring_version"] != policy.scoring_version:
        raise SystemExit("the restated matches were scored under a different scoring version; a "
                         "restatement may not retune the scorer")

    dbt_vars = {
        "payout_policy_version": policy.version,
        "attribution_run_id": new_run,
        "publication_status": "PUBLISHED",
        "matches_source": "restated",
        "prior_publication_id": PRIOR_PUBLICATION_ID,
        "new_publication_id": NEW_PUBLICATION_ID,
        "prior_attribution_run_id": prior_run,
        "restatement_run_id": args.restatement_run_id,
        "trigger_reason": TRIGGER_REASON,
        "prior_normalization_version": baseline.get("normalization_version")
            or "1.0.0+0bc0dd643e06",
        "new_normalization_version": args.new_normalization_version,
        "prior_scoring_version": policy.scoring_version,
        "new_scoring_version": policy.scoring_version,
        "payout_policy_version_expected": policy.version,
        "prior_row_count": int(baseline["row_count"]),
        "prior_portfolio_paid": str(baseline["portfolio_paid"]),
    }

    runs = []
    # --- 1. recompute the financial layer over the restated matches -----------------------
    runs.append(dbt(["build", "--select",
                     "stg_silver__listen_matches+ int_attributable_streams+"],
                    dbt_vars, "recompute financial layer over restated matches"))

    v2 = q(f"""
        SELECT COUNT(*) AS row_count, SUM(holder_payout) AS portfolio_paid,
               COUNT(DISTINCT recording_mbid) AS recordings,
               COUNT(DISTINCT rights_holder_id) AS holders,
               MIN(published_at) AS published_at
        FROM `{FACT}` WHERE attribution_run_id = '{new_run}'
    """, "v2 identity")[0]
    if int(v2["row_count"]) == 0:
        raise SystemExit("the restated publication produced no rows")
    v2_digest = q(publication_digest_sql(FACT, new_run), "v2 digest")[0]

    # --- 2. the delta mart and its reconciliation -----------------------------------------
    runs.append(dbt(["build", "--select", "fct_restatements+"], dbt_vars, "build the delta mart"))

    reconciliation = q(f"""
        SELECT * FROM `{PROJECT}.{DATASET}.restatement_reconciliation`
    """, "restatement reconciliation")[0]

    # --- 3. v1 is untouched ---------------------------------------------------------------
    digest_after = q(publication_digest_sql(FACT, prior_run), "v1 digest AFTER")[0]
    v1_stamp = q(f"""
        SELECT MIN(published_at) AS published_at, COUNT(*) AS row_count,
               SUM(holder_payout) AS portfolio_paid
        FROM `{FACT}` WHERE attribution_run_id = '{prior_run}'
    """, "v1 published_at AFTER")[0]

    immutable = {
        "digest_before": digest_before["content_digest"],
        "digest_after": digest_after["content_digest"],
        "digest_unchanged": digest_before["content_digest"] == digest_after["content_digest"],
        "registry_digest": baseline["content_digest"],
        "matches_registry": digest_after["content_digest"] == baseline["content_digest"],
        "published_at_unchanged": str(v1_stamp["published_at"]) == str(baseline["published_at"]),
        "row_count_unchanged": int(v1_stamp["row_count"]) == int(baseline["row_count"]),
        "portfolio_paid_unchanged": str(v1_stamp["portfolio_paid"]) == str(
            baseline["portfolio_paid"]),
    }
    if not all(v for k, v in immutable.items() if isinstance(v, bool)):
        raise SystemExit(f"the baseline publication changed during the restatement: {immutable}")

    # --- 4. register v2 and move the pointer, then prove v1 is still readable --------------
    q(f"""
        MERGE `{REGISTRY}` t
        USING (SELECT '{NEW_PUBLICATION_ID}' AS publication_id,
                      '{new_run}' AS attribution_run_id,
                      'PUBLISHED' AS publication_status,
                      '{args.new_normalization_version}' AS normalization_version,
                      '{policy.scoring_version}' AS scoring_version,
                      '{args.restatement_run_id}' AS match_run_id,
                      '{policy.version}' AS payout_policy_version,
                      '{policy.rights_version}' AS rights_version,
                      '{policy.rule_version_id}' AS rule_version_id,
                      {int(v2["row_count"])} AS row_count,
                      NUMERIC '{v2["portfolio_paid"]}' AS portfolio_gross,
                      NUMERIC '{v2["portfolio_paid"]}' AS portfolio_paid,
                      '{v2_digest["content_digest"]}' AS content_digest,
                      TIMESTAMP '{v2["published_at"]}' AS published_at,
                      CURRENT_TIMESTAMP() AS registered_at,
                      TRUE AS frozen) s
        ON t.publication_id = s.publication_id
        WHEN NOT MATCHED THEN INSERT ROW
    """, "register v2 in the publication registry")

    q(f"""
        UPDATE `{POINTER}` SET attribution_run_id = '{new_run}',
               payout_policy_version = '{policy.version}', set_at = CURRENT_TIMESTAMP(),
               note = 'restated publication {NEW_PUBLICATION_ID} under normalization '
                      '{args.new_normalization_version}'
        WHERE pointer_name = 'CURRENT'
    """, "move the current pointer to v2")

    pointer_state = q(f"""
        SELECT
          (SELECT attribution_run_id FROM `{POINTER}` WHERE pointer_name = 'CURRENT') AS current_run,
          (SELECT COUNT(*) FROM `{PROJECT}.{DATASET}.fct_royalty_attribution_current`)
            AS current_view_rows,
          (SELECT COUNT(*) FROM `{FACT}` WHERE attribution_run_id = '{prior_run}')
            AS v1_rows_still_readable,
          (SELECT SUM(holder_payout) FROM `{FACT}` WHERE attribution_run_id = '{prior_run}')
            AS v1_total_still_readable
    """, "pointer moved to v2, v1 still queryable")[0]

    runs.append(dbt(["test"], dbt_vars, "dbt test under the restated configuration"))

    report = {
        "artifact": "restatement_publication",
        "declaration": {
            "listens": "REAL", "recordings": "REAL",
            "rights_and_rates": "MODELED",
            "amounts": "illustrative modeled amounts over real listening events",
        },
        "trigger_reason": TRIGGER_REASON,
        "restatement_run_id": args.restatement_run_id,
        "prior": {"publication_id": PRIOR_PUBLICATION_ID, "attribution_run_id": prior_run,
                  "row_count": int(baseline["row_count"]),
                  "portfolio_paid": str(baseline["portfolio_paid"]),
                  "content_digest": baseline["content_digest"],
                  "published_at": str(baseline["published_at"])},
        "new": {"publication_id": NEW_PUBLICATION_ID, "attribution_run_id": new_run,
                "row_count": int(v2["row_count"]),
                "portfolio_paid": str(v2["portfolio_paid"]),
                "content_digest": v2_digest["content_digest"],
                "published_at": str(v2["published_at"]),
                "recordings": int(v2["recordings"]), "holders": int(v2["holders"])},
        "unchanged_inputs": {
            "payout_policy_version": policy.version,
            "scoring_version": policy.scoring_version,
            "rights_version": policy.rights_version,
            "rule_version_id": policy.rule_version_id,
        },
        "changed_input": {
            "prior_normalization_version": dbt_vars["prior_normalization_version"],
            "new_normalization_version": args.new_normalization_version,
        },
        "immutability_of_v1": immutable,
        "pointer": {k: (str(v) if v is not None else None) for k, v in pointer_state.items()},
        "reconciliation": {k: (str(v) if v is not None else None)
                           for k, v in reconciliation.items()},
        "dbt_runs": runs,
        "cost": {
            "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
            "list_price_equivalent_usd": round(
                sum(s.get("bytes_billed") or 0 for s in stats) / 1024**4
                * ON_DEMAND_USD_PER_TIB, 4),
            "caveat": "publisher queries only; dbt reports its own job bytes. Actual monetary cost "
                      "UNKNOWN without billing evidence.",
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    r = reconciliation
    print(f"\n  V1 IMMUTABLE: digest {immutable['digest_unchanged']}, "
          f"published_at {immutable['published_at_unchanged']}, "
          f"rows {immutable['row_count_unchanged']}, total {immutable['portfolio_paid_unchanged']}")
    print(f"  V2: {int(v2['row_count']):,} holder rows, {v2['portfolio_paid']} USD, "
          f"digest {v2_digest['content_digest'][:24]}...")
    print(f"  pointer -> {pointer_state['current_run']}; current view "
          f"{int(pointer_state['current_view_rows']):,} rows; "
          f"v1 still readable: {int(pointer_state['v1_rows_still_readable']):,} rows "
          f"totalling {pointer_state['v1_total_still_readable']}")
    print("\n  RECONCILIATION")
    print(f"    prior paid      {r['prior_portfolio_paid']}")
    print(f"    restated paid   {r['restated_portfolio_paid']}")
    print(f"    difference      {r['portfolio_difference']}")
    print(f"    summed delta    {r['summed_delta']}   exact: {r['delta_reconciles_exactly']}")
    print(f"    listens         {r['listens_total']:,}  none disappeared: "
          f"{r['no_listen_disappeared']}")
    print(f"    newly matched   {r['newly_matched']:,}   still unmatched {r['still_unmatched']:,}   "
          f"matches lost {r['matches_lost']}   newly ambiguous {r['newly_ambiguous']:,}")
    print(f"    holders added   {r['holders_added']:,}   removed {r['holders_removed']:,}   "
          f"increased {r['payouts_increased']:,}   decreased {r['payouts_decreased']:,}")


if __name__ == "__main__":
    main()
