"""Publish a financial attribution run, immutably, and prove the properties that make it auditable.

MATCHED != PAYABLE. This script turns the frozen matcher output plus MODELED temporal rights into a
published financial statement, under an explicit `payout_policy_version`. Every amount it produces
is an ILLUSTRATIVE MODELED AMOUNT: the rate card and the ownership splits are MODELED. The listens
and the recordings are real.

WHAT IT DOES, in order:

  1. verifies the frozen inputs still match config/payout_policy.yml -- a different matcher run or
     rights generation is a different financial universe and is refused, not silently mixed;
  2. computes `attribution_run_id` deterministically from those inputs (no wall-clock);
  3. dry-runs the expensive models and records the estimated bytes BEFORE materialising anything;
  4. runs dbt with the policy version, run id and publication status as vars;
  5. creates or moves the publication pointer;
  6. proves immutability and idempotency by re-running and comparing content digests.

IMMUTABILITY IS STRUCTURAL, NOT ADVISORY: the fact model is append-only, keyed by a deterministic
run id, and guarded so a re-run inserts zero rows. It is NOT protected against a deliberate
`dbt build --full-refresh`, which is recorded as a known limitation rather than implied away.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from payout.policy import attribution_run_id, load_payout_policy

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 100 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25
REPO = pathlib.Path(__file__).parents[2]
DBT_DIR = REPO / "dbt"
# The venv binary rather than `uv run`: two dbt invocations stalled in the parse phase under memory
# pressure with no BigQuery job issued. See docs/runbook.md.
DBT_BIN = REPO / ".venv/bin/dbt"

POINTER_TABLE = f"{PROJECT}.{DATASET}.publication_pointer"
FACT_TABLE = f"{PROJECT}.{DATASET}.fct_royalty_attribution"

FINANCIAL_MODELS = ["int_financial_disposition", "int_attributable_streams",
                    "fct_royalty_attribution", "fct_royalty_attribution_current",
                    "royalty_reconciliation"]


def bq_client():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def run_query(client, sql: str, label: str, stats: list, dry_run: bool = False):
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES, dry_run=dry_run,
                                  use_query_cache=False)
    job = client.query(sql, job_config=cfg)
    if dry_run:
        stats.append({"step": label, "dry_run": True,
                      "estimated_bytes": job.total_bytes_processed})
        print(f"  DRY RUN {label:<34} estimated={job.total_bytes_processed or 0:>13,}", flush=True)
        return None
    rows = [dict(r) for r in job.result()]
    stats.append({"step": label, "job_id": job.job_id,
                  "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
                  "duration_ms": int((job.ended - job.started).total_seconds() * 1000)})
    print(f"  {label:<42} billed={job.total_bytes_billed or 0:>13,}", flush=True)
    return rows


def verify_frozen_inputs(client, policy, stats) -> dict:
    """The publisher refuses to price a different universe than the policy was written for."""
    r = run_query(client, f"""
        SELECT
          (SELECT COUNT(DISTINCT match_run_id)
             FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
             WHERE listened_at >= TIMESTAMP '{policy.period_start} 00:00:00+00'
               AND listened_at <  TIMESTAMP '{policy.period_end} 00:00:00+00') AS match_runs,
          (SELECT MIN(match_run_id)
             FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
             WHERE listened_at >= TIMESTAMP '{policy.period_start} 00:00:00+00'
               AND listened_at <  TIMESTAMP '{policy.period_end} 00:00:00+00') AS match_run_id,
          (SELECT MIN(scoring_version)
             FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
             WHERE listened_at >= TIMESTAMP '{policy.period_start} 00:00:00+00'
               AND listened_at <  TIMESTAMP '{policy.period_end} 00:00:00+00') AS scoring_version,
          (SELECT MIN(rights_version)
             FROM `{PROJECT}.splitsheet_rights.ownership_splits`) AS rights_version,
          (SELECT MIN(generation_run_id)
             FROM `{PROJECT}.splitsheet_rights.ownership_splits`) AS rights_generation_run_id,
          (SELECT MIN(rule_version_id) FROM `{PROJECT}.splitsheet_rights.rate_card`)
            AS rule_version_id
    """, "verify frozen inputs", stats)[0]

    expected = {
        "match_runs": 1,
        "match_run_id": policy.match_run_id,
        "scoring_version": policy.scoring_version,
        "rights_version": policy.rights_version,
        "rights_generation_run_id": policy.rights_generation_run_id,
        "rule_version_id": policy.rule_version_id,
    }
    mismatches = {k: (r[k], v) for k, v in expected.items() if r[k] != v}
    if mismatches:
        raise SystemExit(
            f"the warehouse no longer holds the inputs this policy was written for: "
            f"{mismatches}. Publishing would price a different universe under the same "
            f"payout_policy_version.")
    return {k: r[k] for k in expected}


def ensure_pointer_table(client, stats) -> None:
    run_query(client, f"""
        CREATE TABLE IF NOT EXISTS `{POINTER_TABLE}` (
          pointer_name STRING NOT NULL
            OPTIONS(description='CURRENT names the publication in force.'),
          attribution_run_id STRING NOT NULL,
          payout_policy_version STRING NOT NULL,
          set_at TIMESTAMP NOT NULL,
          note STRING
        ) OPTIONS(description='Publication pointer. Mutable BY DESIGN and deliberately outside dbt: moving it changes which immutable publication is in force and touches no published row.')
    """, "ensure pointer table", stats)


def set_pointer(client, run_id: str, policy_version: str, note: str, stats) -> None:
    """MERGE on pointer_name: the pointer is the one mutable object in the financial layer, and it
    contains no amounts. Published rows are never touched by this."""
    run_query(client, f"""
        MERGE `{POINTER_TABLE}` t
        USING (SELECT 'CURRENT' AS pointer_name, '{run_id}' AS attribution_run_id,
                      '{policy_version}' AS payout_policy_version,
                      CURRENT_TIMESTAMP() AS set_at, '{note}' AS note) s
        ON t.pointer_name = s.pointer_name
        WHEN MATCHED THEN UPDATE SET
          attribution_run_id = s.attribution_run_id,
          payout_policy_version = s.payout_policy_version,
          set_at = s.set_at, note = s.note
        WHEN NOT MATCHED THEN INSERT (pointer_name, attribution_run_id, payout_policy_version,
                                      set_at, note)
        VALUES (s.pointer_name, s.attribution_run_id, s.payout_policy_version, s.set_at, s.note)
    """, f"set pointer -> {run_id}", stats)


def dbt(args: list[str], dbt_vars: dict, label: str) -> dict:
    """Run dbt and capture its outcome. A non-zero exit is a failure, and so is a timeout: neither
    is ever read as success."""
    cmd = [str(DBT_BIN), *args, "--vars", json.dumps(dbt_vars)]
    env = {**dict(__import__("os").environ), "DBT_PROFILES_DIR": str(DBT_DIR)}
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=DBT_DIR, env=env, capture_output=True, text=True,
                          check=False, timeout=3600)
    tail = (proc.stdout or "")[-4000:]
    ok = proc.returncode == 0
    summary = [ln for ln in tail.splitlines() if "Done." in ln or "Completed" in ln]
    print(f"  dbt {label:<38} exit={proc.returncode}  {summary[-1].strip() if summary else ''}",
          flush=True)
    if not ok:
        print(tail[-2500:], flush=True)
        raise SystemExit(f"dbt {label} failed with exit {proc.returncode}")
    return {"label": label, "exit_code": proc.returncode,
            "seconds": round(time.time() - t0, 1),
            "summary": summary[-1].strip() if summary else None}


def content_digest(client, run_id: str, stats, label: str) -> dict:
    """A digest over the published rows, so 'unchanged' is provable rather than asserted.

    published_at is deliberately excluded: it is wall-clock metadata, and the claim being tested is
    that the AMOUNTS and their keys are untouched.
    """
    r = run_query(client, f"""
        SELECT COUNT(*) AS rows_published,
               TO_HEX(SHA256(STRING_AGG(row_key, '\\n' ORDER BY row_key))) AS content_digest,
               SUM(holder_payout) AS total_holder_payout,
               MIN(published_at) AS first_published_at
        FROM (
          SELECT FORMAT('%t|%t|%t|%t|%t|%t|%t', recording_mbid, rights_holder_id,
                        split_version_id, rate_card_id, holder_share_pct, holder_payout,
                        gross_royalty) AS row_key,
                 holder_payout, published_at
          FROM `{FACT_TABLE}`
          WHERE attribution_run_id = '{run_id}')
    """, label, stats)[0]
    return {k: (str(v) if v is not None else None) for k, v in r.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="PUBLISHED", choices=["PUBLISHED", "REHEARSAL"])
    ap.add_argument("--skip-dry-run", action="store_true")
    ap.add_argument("--prove-idempotency", action="store_true",
                    help="re-run the publication and prove the content is unchanged")
    ap.add_argument("--prove-pointer-move", action="store_true",
                    help="move the pointer away and back, proving published rows are untouched")
    args = ap.parse_args()

    policy = load_payout_policy()
    run_id = attribution_run_id(policy, args.label)
    client = bq_client()
    stats: list = []
    dbt_runs: list = []
    t0 = time.time()

    print(f"payout policy {policy.version}  label {args.label}  "
          f"attribution_run_id {run_id}", flush=True)

    frozen = verify_frozen_inputs(client, policy, stats)
    print(f"  frozen inputs verified: {frozen['match_run_id']} / {frozen['rights_version']}",
          flush=True)

    dbt_vars = {
        "payout_policy_version": policy.version,
        "attribution_run_id": run_id,
        "publication_status": args.label,
    }

    # --- dry run BEFORE materialising anything expensive ---------------------------------
    if not args.skip_dry_run:
        for model in ("int_financial_disposition", "royalty_reconciliation"):
            compiled = DBT_DIR / "target/compiled/splitsheet/models"
            paths = list(compiled.rglob(f"{model}.sql")) if compiled.exists() else []
            if paths:
                run_query(client, paths[0].read_text(), f"estimate {model}", stats, dry_run=True)

    ensure_pointer_table(client, stats)

    # --- build ---------------------------------------------------------------------------
    dbt_runs.append(dbt(["build", "--select",
                         "int_financial_disposition+ int_attributable_streams+"], dbt_vars,
                        "build financial layer"))

    first = content_digest(client, run_id, stats, "publication content digest")
    if int(first["rows_published"]) == 0:
        raise SystemExit("publication produced no rows")
    set_pointer(client, run_id, policy.version,
                f"{args.label} under payout policy {policy.version}", stats)

    proofs: dict = {"first_publication": first}

    # --- idempotency: same inputs, same run id, zero new rows ----------------------------
    if args.prove_idempotency:
        dbt_runs.append(dbt(["run", "--select", "fct_royalty_attribution"], dbt_vars,
                            "re-run publication (idempotency)"))
        second = content_digest(client, run_id, stats, "digest after re-run")
        proofs["after_rerun"] = second
        proofs["idempotent"] = (
            second["rows_published"] == first["rows_published"]
            and second["content_digest"] == first["content_digest"]
            and second["total_holder_payout"] == first["total_holder_payout"]
            and second["first_published_at"] == first["first_published_at"])
        if not proofs["idempotent"]:
            raise SystemExit(f"re-run changed the publication: {first} -> {second}")

    # --- pointer movement does not destroy anything --------------------------------------
    if args.prove_pointer_move:
        before = content_digest(client, run_id, stats, "digest before pointer move")
        set_pointer(client, "attr:none", policy.version,
                    "deliberate no-current-publication state, to prove pointer moves are "
                    "non-destructive", stats)
        during = content_digest(client, run_id, stats, "digest while pointer moved away")
        current_rows = run_query(client, f"""
            SELECT COUNT(*) AS n FROM `{PROJECT}.{DATASET}.fct_royalty_attribution_current`
        """, "current view while pointer moved away", stats)[0]
        set_pointer(client, run_id, policy.version,
                    f"restored to {args.label} publication", stats)
        after = content_digest(client, run_id, stats, "digest after pointer restored")
        proofs["pointer_move"] = {
            "published_rows_before": before["rows_published"],
            "published_rows_while_moved": during["rows_published"],
            "published_rows_after": after["rows_published"],
            "current_view_rows_while_moved": int(current_rows["n"]),
            "digest_unchanged": (before["content_digest"] == during["content_digest"]
                                 == after["content_digest"]),
            "previous_publication_still_queryable": int(during["rows_published"]) > 0,
        }
        if not proofs["pointer_move"]["digest_unchanged"]:
            raise SystemExit("moving the pointer changed published content")

    # --- test the whole thing ------------------------------------------------------------
    dbt_runs.append(dbt(["test"], dbt_vars, "dbt test"))

    reconciliation = run_query(client, f"""
        SELECT attribution_status, listens, streams, pct_of_all_listens, distinct_recordings,
               total_gross_royalty, total_holder_payout, holder_rows, recordings_paid, holders_paid
        FROM `{PROJECT}.{DATASET}.royalty_reconciliation`
        ORDER BY listens DESC
    """, "reconciliation", stats)

    # gross_royalty is a GROUP-level value repeated on every holder row, so summing it across rows
    # would multiply it by the holder count. The total gross is the sum of one gross per group, and
    # it must equal the total of the holder payouts -- which is the closure invariant at portfolio
    # level rather than per group.
    money = run_query(client, f"""
        WITH per_group AS (
          SELECT period, recording_mbid, split_version_id, rate_card_id,
                 MAX(gross_royalty) AS gross_royalty,
                 SUM(holder_payout) AS group_holder_payout,
                 MAX(remainder_cents) AS remainder_cents,
                 MAX(holder_count) AS holder_count
          FROM `{FACT_TABLE}` WHERE attribution_run_id = '{run_id}'
          GROUP BY period, recording_mbid, split_version_id, rate_card_id
        )
        SELECT
          (SELECT COUNT(*) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS holder_rows,
          (SELECT SUM(holder_payout) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS total_holder_payout,
          (SELECT MIN(holder_payout) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS min_holder_payout,
          (SELECT MAX(holder_payout) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS max_holder_payout,
          (SELECT COUNTIF(holder_payout < NUMERIC '0') FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS negative_payouts,
          (SELECT COUNTIF(holder_payout = NUMERIC '0') FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS zero_payouts,
          (SELECT COUNT(DISTINCT rights_holder_id) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS holders_paid,
          (SELECT COUNT(DISTINCT recording_mbid) FROM `{FACT_TABLE}`
             WHERE attribution_run_id = '{run_id}') AS recordings_paid,
          (SELECT COUNT(*) FROM per_group) AS financial_groups,
          (SELECT SUM(gross_royalty) FROM per_group) AS total_gross_royalty,
          (SELECT SUM(remainder_cents) FROM per_group) AS remainder_cents_distributed,
          (SELECT COUNTIF(group_holder_payout != gross_royalty) FROM per_group)
            AS groups_that_do_not_close
    """, "money summary", stats)[0]
    if int(money["groups_that_do_not_close"]) != 0:
        raise SystemExit(f"{money['groups_that_do_not_close']} groups do not close in cents")
    if str(money["total_gross_royalty"]) != str(money["total_holder_payout"]):
        raise SystemExit(
            f"portfolio total does not close: gross {money['total_gross_royalty']} vs paid "
            f"{money['total_holder_payout']}")

    estimated = sum(s.get("estimated_bytes") or 0 for s in stats)
    billed = sum(s.get("bytes_billed") or 0 for s in stats)
    report = {
        "artifact": "royalty_attribution_publication",
        "declaration": {
            "listenbrainz_listens": "REAL", "musicbrainz_recordings": "REAL",
            "rights_holders": "MODELED", "ownership_splits": "MODELED",
            "rate_cards": "MODELED",
            "amounts": "illustrative modeled amounts, not observed industry payouts",
        },
        "payout_policy_version": policy.version,
        "publication_label": args.label,
        "attribution_run_id": run_id,
        "frozen_inputs_verified": frozen,
        "policy": {
            "hold_methods": list(policy.hold_methods),
            "rate_imputation": "FORBIDDEN",
            "rounding": "largest_remainder, single rounding point, tiebreak "
                        + policy.tiebreak,
            "terminal_states": list(policy.attribution_states),
            "fact_grain": list(policy.fact_grain),
        },
        "proofs": proofs,
        "reconciliation": reconciliation,
        "money": {k: (str(v) if v is not None else None) for k, v in money.items()},
        "dbt_runs": dbt_runs,
        "cost": {
            "estimated_bytes_dry_run": estimated,
            "bytes_billed_queries": billed,
            "list_price_equivalent_usd": round(billed / 1024**4 * ON_DEMAND_USD_PER_TIB, 4),
            "caveat": ("list-price equivalent of processing consumption for this script's own "
                       "queries; dbt's own job bytes are reported separately by dbt. Actual "
                       "monetary cost UNKNOWN without billing evidence."),
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    print(f"\n  published {int(money['holder_rows']):,} holder rows over "
          f"{int(money['financial_groups']):,} financial groups")
    print(f"  gross {money['total_gross_royalty']} == paid {money['total_holder_payout']} "
          f"{policy.currency}   negative={money['negative_payouts']}  "
          f"zero={money['zero_payouts']}  remainder cents distributed="
          f"{int(money['remainder_cents_distributed']):,}")
    print("\n  waterfall:")
    for r in reconciliation:
        print(f"    {r['attribution_status']:<22} listens={r['listens']:>12,}  "
              f"{r['pct_of_all_listens']:>10}%")
    if proofs.get("idempotent") is not None:
        print(f"\n  idempotent re-run: {proofs['idempotent']}")
    if "pointer_move" in proofs:
        pm = proofs["pointer_move"]
        print(f"  pointer moved away and back: digest unchanged={pm['digest_unchanged']}, "
              f"current view rows while moved={pm['current_view_rows_while_moved']}, "
              f"previous publication still queryable={pm['previous_publication_still_queryable']}")


if __name__ == "__main__":
    main()
