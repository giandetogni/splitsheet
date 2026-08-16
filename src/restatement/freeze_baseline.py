"""Freeze the Phase 5B financial publication as an immutable baseline, then decompose it.

TWO JOBS, both read-only against the publication:

  1. FREEZE. Record the publication's identity, its versions, its row count, its totals and a
     REPRODUCIBLE digest of its content, persist all of it in a registry table, and prove the
     publication is still queryable. After this runs, no step of Phase 6 may modify those rows --
     and the digest is what makes that claim checkable rather than asserted.

  2. BLACK-BOX REPORT. Decompose all 38,199,641 listens into the five terminal states with streams,
     shares, and -- where a rate legitimately exists -- an illustrative modeled suspended amount.

THE LANGUAGE MATTERS AND IS NOT DECORATION. Every amount here is an "illustrative modeled suspended
amount over real listening events": the listens are REAL ListenBrainz events, the rate card and
ownership are MODELED, and a suspended amount is money that was NOT paid. None of it is revenue,
observed or otherwise.

RATE_CARD_GAP GETS NO AMOUNT. Not the previous rate, not the next rate, not an average, not zero.
Its streams are reported with amount UNKNOWN, because the modeled rate card genuinely does not
cover those three days and inventing a number for them would be inventing money. Every other
category CAN be valued for the days a rate exists -- that is not imputation, it is applying the
rate that the card actually defines for that date to streams that were withheld for a different
reason.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from payout.digest import publication_digest_sql
from payout.policy import attribution_run_id, load_payout_policy

PROJECT = "ss-de-944054e7"
DATASET = "splitsheet_dbt"
MAX_BYTES = 100 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25

FACT = f"{PROJECT}.{DATASET}.fct_royalty_attribution"
DISPOSITION = f"{PROJECT}.{DATASET}.int_financial_disposition"
REGISTRY = f"{PROJECT}.{DATASET}.publication_registry"
RATE_CARD = f"{PROJECT}.splitsheet_rights.rate_card"

PUBLICATION_ID = "pub:v1"
AMOUNT_LANGUAGE = "illustrative modeled suspended amount over real listening events"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--publication-id", default=PUBLICATION_ID)
    args = ap.parse_args()

    from google.cloud import bigquery

    policy = load_payout_policy()
    run_id = attribution_run_id(policy, "PUBLISHED")
    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()

    def q(sql: str, label: str):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=MAX_BYTES, use_query_cache=False))
        rows = [dict(r) for r in job.result()]
        stats.append({"step": label, "job_id": job.job_id,
                      "bytes_billed": job.total_bytes_billed, "slot_ms": job.slot_millis,
                      "duration_ms": int((job.ended - job.started).total_seconds() * 1000)})
        print(f"  {label:<44} billed={job.total_bytes_billed or 0:>13,}", flush=True)
        return rows

    # --- 1. identity and digest, in two queries -------------------------------------------
    #
    # Two queries rather than one because the first attempt combined the digest with the identity
    # aggregates and BigQuery killed it: "Resources exceeded ... Peak usage: 119% of limit". The
    # digest itself is now a two-level bucketed hash (src/payout/digest.py) for the same reason.
    identity = q(f"""
        SELECT
          COUNT(*) AS row_count,
          COUNT(DISTINCT rights_holder_id) AS holders,
          COUNT(DISTINCT recording_mbid) AS recordings,
          MIN(payout_policy_version) AS payout_policy_version,
          MIN(rule_version_id) AS rule_version_id,
          MIN(publication_status) AS publication_status,
          MIN(published_at) AS published_at,
          MAX(published_at) AS last_published_at,
          SUM(holder_payout) AS portfolio_paid
        FROM `{FACT}` WHERE attribution_run_id = '{run_id}'
    """, "publication identity")[0]

    digest = q(publication_digest_sql(FACT, run_id), "publication content digest")[0]
    if int(digest["row_count"]) != int(identity["row_count"]):
        raise SystemExit("the digest covered a different number of rows than the identity query")
    if str(digest["total_holder_payout"]) != str(identity["portfolio_paid"]):
        raise SystemExit("the digest covered a different total than the identity query")
    identity["content_digest"] = digest["content_digest"]

    gross = q(f"""
        SELECT SUM(gross_royalty) AS portfolio_gross, COUNT(*) AS financial_groups
        FROM (
          SELECT MAX(gross_royalty) AS gross_royalty
          FROM `{FACT}` WHERE attribution_run_id = '{run_id}'
          GROUP BY period, recording_mbid, split_version_id, rate_card_id)
    """, "portfolio gross")[0]

    versions = q(f"""
        SELECT MIN(normalization_version) AS normalization_version,
               MIN(scoring_version) AS scoring_version,
               MIN(match_run_id) AS match_run_id,
               COUNT(DISTINCT match_run_id) AS match_runs
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    """, "upstream versions")[0]

    baseline = {
        "publication_id": args.publication_id,
        "attribution_run_id": run_id,
        "normalization_version": versions["normalization_version"],
        "scoring_version": versions["scoring_version"],
        "match_run_id": versions["match_run_id"],
        "payout_policy_version": identity["payout_policy_version"],
        "rights_version": policy.rights_version,
        "rule_version_id": identity["rule_version_id"],
        "row_count": int(identity["row_count"]),
        "financial_groups": int(gross["financial_groups"]),
        "portfolio_gross": str(gross["portfolio_gross"]),
        "portfolio_paid": str(identity["portfolio_paid"]),
        "content_digest": identity["content_digest"],
        "published_at": str(identity["published_at"]),
        "publication_status": identity["publication_status"],
        "holders": int(identity["holders"]),
        "recordings": int(identity["recordings"]),
    }
    if baseline["portfolio_gross"] != baseline["portfolio_paid"]:
        raise SystemExit(f"baseline does not close: {baseline['portfolio_gross']} gross vs "
                         f"{baseline['portfolio_paid']} paid")

    # --- 2. persist it ---------------------------------------------------------------------
    #
    # A registry row, not a comment in a doc. Everything Phase 6 compares against comes from here,
    # and MERGE on publication_id means re-running this step cannot create a second baseline.
    q(f"""
        CREATE TABLE IF NOT EXISTS `{REGISTRY}` (
          publication_id STRING NOT NULL,
          attribution_run_id STRING NOT NULL,
          publication_status STRING NOT NULL,
          normalization_version STRING NOT NULL,
          scoring_version STRING NOT NULL,
          match_run_id STRING NOT NULL,
          payout_policy_version STRING NOT NULL,
          rights_version STRING NOT NULL,
          rule_version_id STRING NOT NULL,
          row_count INT64 NOT NULL,
          portfolio_gross NUMERIC NOT NULL,
          portfolio_paid NUMERIC NOT NULL,
          content_digest STRING NOT NULL,
          published_at TIMESTAMP NOT NULL,
          registered_at TIMESTAMP NOT NULL,
          frozen BOOL NOT NULL
        ) OPTIONS(description='Registry of financial publications. A frozen row is a baseline no later phase may modify; content_digest is the reproducible proof.')
    """, "ensure publication registry")

    q(f"""
        MERGE `{REGISTRY}` t
        USING (SELECT '{baseline["publication_id"]}' AS publication_id,
                      '{run_id}' AS attribution_run_id,
                      '{baseline["publication_status"]}' AS publication_status,
                      '{baseline["normalization_version"]}' AS normalization_version,
                      '{baseline["scoring_version"]}' AS scoring_version,
                      '{baseline["match_run_id"]}' AS match_run_id,
                      '{baseline["payout_policy_version"]}' AS payout_policy_version,
                      '{baseline["rights_version"]}' AS rights_version,
                      '{baseline["rule_version_id"]}' AS rule_version_id,
                      {baseline["row_count"]} AS row_count,
                      NUMERIC '{baseline["portfolio_gross"]}' AS portfolio_gross,
                      NUMERIC '{baseline["portfolio_paid"]}' AS portfolio_paid,
                      '{baseline["content_digest"]}' AS content_digest,
                      TIMESTAMP '{baseline["published_at"]}' AS published_at,
                      CURRENT_TIMESTAMP() AS registered_at,
                      TRUE AS frozen) s
        ON t.publication_id = s.publication_id
        WHEN NOT MATCHED THEN INSERT ROW
        -- A frozen baseline is never re-registered with different content. If the digest ever
        -- differs, that is the alarm, not something to overwrite.
        WHEN MATCHED AND t.content_digest != s.content_digest THEN
          UPDATE SET frozen = FALSE
    """, "register the frozen baseline")

    registered = q(f"""
        SELECT publication_id, attribution_run_id, content_digest, row_count,
               portfolio_gross, portfolio_paid, frozen, published_at
        FROM `{REGISTRY}` ORDER BY registered_at
    """, "read back the registry")

    if any(not r["frozen"] for r in registered):
        raise SystemExit(f"a registered publication lost its frozen status: {registered}")

    # --- 3. prove it is still queryable ----------------------------------------------------
    still = q(f"""
        SELECT COUNT(*) AS rows_readable, SUM(holder_payout) AS paid,
               COUNT(DISTINCT attribution_run_id) AS runs
        FROM `{FACT}` WHERE attribution_run_id = '{run_id}'
    """, "prove v1 still queryable")[0]
    queryable = (int(still["rows_readable"]) == baseline["row_count"]
                 and str(still["paid"]) == baseline["portfolio_paid"])

    # --- 4. the black-box decomposition ----------------------------------------------------
    #
    # A rate is a function of the DATE alone, so streams held for a non-rate reason can be valued
    # at the rate their date actually carries. Streams inside the rate-card gap cannot: there is no
    # rate to apply, and their amount is UNKNOWN rather than zero.
    blackbox = q(f"""
        WITH d AS (
          SELECT attribution_status, listen_date, match_status, COUNT(*) AS streams
          FROM `{DISPOSITION}`
          GROUP BY attribution_status, listen_date, match_status
        ),
        priced AS (
          SELECT d.attribution_status, d.match_status, d.streams, d.listen_date,
                 r.rate_per_stream
          FROM d
          LEFT JOIN `{RATE_CARD}` r
                 ON d.listen_date >= r.valid_from AND d.listen_date < r.valid_to
        )
        SELECT
          attribution_status,
          SUM(streams) AS streams,
          SUM(IF(match_status = 'MATCHED', streams, 0)) AS matched_streams,
          SUM(IF(rate_per_stream IS NOT NULL, streams, 0)) AS streams_with_a_rate,
          SUM(IF(rate_per_stream IS NULL, streams, 0)) AS streams_with_amount_unknown,
          ROUND(SUM(IF(rate_per_stream IS NOT NULL,
                       streams * rate_per_stream, NUMERIC '0')), 2) AS modeled_amount,
          COUNT(DISTINCT listen_date) AS days
        FROM priced
        GROUP BY attribution_status
        ORDER BY streams DESC
    """, "black-box decomposition")

    totals = q(f"""
        SELECT COUNT(*) AS listens, COUNTIF(match_status = 'MATCHED') AS matched
        FROM `{DISPOSITION}`
    """, "totals")[0]

    total_listens = int(totals["listens"])
    matched = int(totals["matched"])
    if total_listens != 38_199_641:
        raise SystemExit(f"disposition holds {total_listens} listens, expected 38,199,641")

    categories = []
    for row in blackbox:
        streams = int(row["streams"])
        is_attributable = row["attribution_status"] == "ATTRIBUTABLE"
        categories.append({
            "attribution_status": row["attribution_status"],
            "streams": streams,
            "pct_of_total": round(100 * streams / total_listens, 6),
            "pct_of_matched": (round(100 * int(row["matched_streams"]) / matched, 6)
                               if int(row["matched_streams"]) else None),
            "matched_streams": int(row["matched_streams"]),
            "streams_with_a_rate": int(row["streams_with_a_rate"]),
            "streams_with_amount_unknown": int(row["streams_with_amount_unknown"]),
            "amount": str(row["modeled_amount"]) if int(row["streams_with_a_rate"]) else None,
            "amount_meaning": (
                "illustrative modeled amount ATTRIBUTED over real listening events"
                if is_attributable else AMOUNT_LANGUAGE),
            "amount_unknown_reason": (
                "no rate card row covers these dates; no rate is imputed"
                if int(row["streams_with_amount_unknown"]) else None),
            "days_present": int(row["days"]),
        })

    reconciles = sum(c["streams"] for c in categories) == total_listens

    # THE TWO NUMBERS THAT MUST NOT COEXIST UNEXPLAINED.
    #
    # The black box values ATTRIBUTABLE streams at the rate their date carries, unrounded:
    # 103,179.68. The publication paid 98,284.22. The difference is not a leak and not a mistake --
    # it is the cost of rounding money to cents at the published grain. The financial grain is
    # (recording, split set, rate window) and its median group is ONE stream worth $0.0035, which
    # rounds to $0.00. Three million such groups round away.
    #
    # This is the same root cause as the 3,926,337 zero-value holder rows reported in Phase 5B,
    # now measured in money instead of in rows. It is reported here rather than left as a
    # discrepancy a reader would have to discover.
    attributable = next(c for c in categories if c["attribution_status"] == "ATTRIBUTABLE")
    modeled_unrounded = float(attributable["amount"])
    published_paid = float(baseline["portfolio_paid"])
    rounding_loss = round(modeled_unrounded - published_paid, 2)
    rounding = q(f"""
        SELECT COUNT(*) AS financial_groups,
               COUNTIF(gross_royalty = NUMERIC '0') AS groups_rounded_to_zero,
               COUNTIF(attributable_streams = 1) AS single_stream_groups,
               ROUND(SUM(gross_royalty_unrounded), 2) AS unrounded_group_total,
               ROUND(SUM(gross_royalty), 2) AS rounded_group_total
        FROM (
          SELECT MAX(gross_royalty) AS gross_royalty,
                 MAX(gross_royalty_unrounded) AS gross_royalty_unrounded,
                 MAX(attributable_streams) AS attributable_streams
          FROM `{FACT}` WHERE attribution_run_id = '{run_id}'
          GROUP BY period, recording_mbid, split_version_id, rate_card_id)
    """, "rounding loss at the published grain")[0]
    suspended = sum(float(c["amount"]) for c in categories
                    if c["amount"] and c["attribution_status"] != "ATTRIBUTABLE")

    report = {
        "artifact": "baseline_freeze_and_black_box_report",
        "declaration": {
            "listens": "REAL ListenBrainz events",
            "recordings": "REAL MusicBrainz canonical snapshot",
            "rights_holders_ownership_rate_cards": "MODELED",
            "all_amounts": AMOUNT_LANGUAGE,
            "revenue_claim": "none: no figure here is revenue, observed or otherwise",
            "rate_card_gap_valuation": "UNKNOWN, never imputed",
        },
        "baseline": baseline,
        "registry": registered,
        "baseline_still_queryable": queryable,
        "rounding_reconciliation": {
            "attributable_modeled_unrounded": str(modeled_unrounded),
            "published_paid": baseline["portfolio_paid"],
            "difference_lost_to_rounding": str(rounding_loss),
            "pct_of_modeled_lost": round(100 * rounding_loss / modeled_unrounded, 4),
            "financial_groups": int(rounding["financial_groups"]),
            "groups_rounded_to_zero": int(rounding["groups_rounded_to_zero"]),
            "single_stream_groups": int(rounding["single_stream_groups"]),
            "unrounded_group_total": str(rounding["unrounded_group_total"]),
            "rounded_group_total": str(rounding["rounded_group_total"]),
            "explanation": (
                "money is published to cents at the (recording, split set, rate window) grain, "
                "and the median group is one stream worth $0.0035, which rounds to $0.00. This is "
                "not a leak: it is the measured cost of the published rounding policy at a very "
                "fine grain, and it is the same root cause as the zero-value holder rows."),
            "not_a_restatement_trigger": (
                "fixing this would require a minimum-payment threshold with multi-period balance "
                "carry-forward, which is explicitly out of scope"),
        },
        "black_box": {
            "total_listens": total_listens,
            "matched_listens": matched,
            "reconciles_to_total": reconciles,
            "categories": categories,
            "suspended_amount_total": round(suspended, 2),
            "suspended_amount_meaning": AMOUNT_LANGUAGE,
            "attributed_amount": next(c["amount"] for c in categories
                                      if c["attribution_status"] == "ATTRIBUTABLE"),
        },
        "cost": {
            "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
            "list_price_equivalent_usd": round(
                sum(s.get("bytes_billed") or 0 for s in stats) / 1024**4
                * ON_DEMAND_USD_PER_TIB, 4),
            "caveat": ("list-price equivalent of processing consumption; actual monetary cost "
                       "UNKNOWN without billing evidence"),
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    if not reconciles:
        raise SystemExit("the black-box decomposition does not reconcile to the total")

    print(f"\n  BASELINE {baseline['publication_id']} / {run_id}")
    print(f"    normalization {baseline['normalization_version']}   "
          f"scoring {baseline['scoring_version']}")
    print(f"    payout policy {baseline['payout_policy_version']}   "
          f"rights {baseline['rights_version']}")
    print(f"    rows {baseline['row_count']:,}   gross {baseline['portfolio_gross']}   "
          f"paid {baseline['portfolio_paid']}")
    print(f"    digest {baseline['content_digest'][:32]}...")
    print(f"    published_at {baseline['published_at']}   still queryable: {queryable}")
    print(f"\n  BLACK BOX ({AMOUNT_LANGUAGE}):")
    for c in categories:
        amount = f"{c['amount']:>12}" if c["amount"] else "     UNKNOWN"
        print(f"    {c['attribution_status']:<22} streams={c['streams']:>12,} "
              f"{c['pct_of_total']:>9.4f}%  amount={amount}  "
              f"unknown_streams={c['streams_with_amount_unknown']:>9,}")
    print(f"    {'suspended total':<22} {'':>12}  {'':>9}   "
          f"amount={round(suspended, 2):>12}")
    print("\n  ROUNDING RECONCILIATION")
    print(f"    attributable modeled (unrounded) {modeled_unrounded:>12}")
    print(f"    published paid                   {published_paid:>12}")
    print(f"    lost to cent rounding            {rounding_loss:>12}  "
          f"({round(100 * rounding_loss / modeled_unrounded, 4)}% of modeled)")
    print(f"    groups rounded to zero           "
          f"{int(rounding['groups_rounded_to_zero']):>12,} of "
          f"{int(rounding['financial_groups']):,}")


if __name__ == "__main__":
    main()
