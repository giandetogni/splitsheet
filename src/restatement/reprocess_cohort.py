"""Reprocess ONLY the cohort whose normalization can change, under normalization 1.1.0.

INCREMENTAL BY CONSTRUCTION, and the reason is cost rather than elegance. A full rebuild would
re-normalize 4,599,791 distinct pairs, rebuild a 58.8M-row canonical index, regenerate 34.5M
candidates and re-score 3M feature rows to change a cohort of about a million listens. The affected
cohort is defined by a property that is cheap to evaluate -- does the string contain a character in
an enabled script -- so everything outside it is copied forward unchanged and PROVEN unchanged.

    affected cohort     listens whose artist or recording contains hangul or kana
    unaffected cohort   every other listen: normalized values, keys, candidates and match decision
                        are carried over from the frozen v1 result, byte for byte

WHAT IS WRITTEN, all of it into NEW tables that leave the v1 result untouched:

    bronze.canonical_blocking_index_v11     transliterated canonical keys (added rows only)
    silver.listen_pair_normalization_v11    new normalization for affected distinct pairs
    silver.silver_listen_matches_restated   ALL 38,199,641 listens: v1 rows for the unaffected
                                            cohort, recomputed rows for the affected one

The scoring configuration is NOT touched: the same frozen weights, thresholds, margin and tie
epsilon decide the new candidates. A restatement that also retuned the scorer could not attribute
its delta to the trigger.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from matching.scoring import load_scoring_rules
from normalization import load_rules, normalize
from normalization.transliterate import would_transliterate
from restatement.identity import (
    LEGACY_RUN_ID,
    PUBLISHED_COHORT_KEY,
    SCRIPT_PATTERNS,
    inputs_for_published_restatement,
    legacy_run_id,
    load_cohorts,
)

PROJECT = "ss-de-944054e7"
MAX_BYTES = 200 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25


def period(alias: str = "") -> str:
    """The half-open period predicate, with EVERY column reference qualified.

    Written as a function because a bare string containing two `listened_at` references cannot be
    prefixed by an alias: `r.` + the string qualifies only the first one, and BigQuery then reports
    the second as ambiguous in a join. That is exactly how the first run of this script died.
    """
    a = f"{alias}." if alias else ""
    return (
        f"{a}listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
        f"AND {a}listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
    )


PERIOD = period()
SNAPSHOT = "2026-07-17"
# THE COHORT COMES FROM config/restatement_cohorts.yml, and so does this script's identity.
#
# It is both conditions, exactly as config/restatement_trigger.yml froze it: the strings contain an
# enabled script AND the v1 failure reason was a missing key.
#
# The first implementation used the script test ALONE, which pulled in 395,540 extra listens that
# already had a key -- mixed Latin/CJK strings like "Dynamite (한국어)" whose ASCII key CHANGES when
# the CJK part starts transliterating instead of being dropped. That cost 29,954 previously matched
# listens (21,211 exact-unique, 7,476 exact-multiple, 1,267 fallback), which is a regression the
# frozen definition never asked for. Restricting to the frozen definition means no v1 match can be
# lost, because every listen in scope was unmatched to begin with.
#
# That wrong cohort is not deleted from the registry -- it is kept there as the counter-example that
# proves the run identity actually distinguishes cohorts. Under the superseded scheme both cohorts
# produced the SAME restatement_run_id, which is the defect this wiring closes: the predicate that
# selects the rows below and the hash that names the run are now the same object.
COHORT = load_cohorts()[PUBLISHED_COHORT_KEY]
COHORT_SQL = COHORT.sql_predicate(normalized_alias="n", matches_alias="m")
HANGUL_RE = SCRIPT_PATTERNS["hangul"]
KANA_RE = SCRIPT_PATTERNS["kana"]

EXPECTED_LISTENS = 38_199_641
V1_MATCH_RUN = "match:101eef5c5b5c081e"
BLOCKING_VERSION = "staged-1.1.0"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery, storage

    rules = load_rules()
    scoring = load_scoring_rules()
    v1_rules = load_rules(
        pathlib.Path(__file__).parents[2] / "config/normalization_rules_v1.0.0.yml"
    )
    client = bigquery.Client(project=PROJECT)
    bucket = storage.Client(project=PROJECT).bucket(args.bucket)
    work = pathlib.Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    stats: list = []
    t0 = time.time()

    if rules.version == v1_rules.version:
        raise SystemExit("the live rules are still the v1 rules; nothing to restate")
    print(f"restating under normalization {rules.version} (v1 was {v1_rules.version})", flush=True)
    print(f"scoring stays {scoring.version}", flush=True)

    # THE RUN'S IDENTITY IS DERIVED, NOT INVENTED HERE: canonical inputs -> sha256 -> id, with the
    # cohort digest among the inputs. The superseded formula is computed alongside it only to show
    # what it could not see, and to keep the mapping to the rows already published under it honest.
    identity = inputs_for_published_restatement()
    run_id = identity.run_id
    # Recomputed from THIS script's live constants, not from a copy of the answer: if the versions
    # here no longer reproduce the identifier the published rows carry, the registry's mapping from
    # legacy to canonical is describing a run that never happened.
    superseded = legacy_run_id(rules.version, scoring.version, V1_MATCH_RUN, BLOCKING_VERSION)
    if superseded != LEGACY_RUN_ID.run_id:
        raise SystemExit(
            f"the superseded formula now yields {superseded}, not the published "
            f"{LEGACY_RUN_ID.run_id}; the legacy-to-canonical mapping in the restatement run "
            f"registry would be wrong"
        )
    print(
        f"restatement_run_id  {run_id}  (cohort {COHORT.cohort_key}, "
        f"digest {COHORT.digest[:12]})",
        flush=True,
    )
    print(
        f"  superseded id     {superseded}  -- versions only, cohort-blind; pub:v2 rows carry it",
        flush=True,
    )

    def q(sql: str, label: str, dry: bool = False):
        job = client.query(
            sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES, dry_run=dry)
        )
        if dry:
            stats.append(
                {"step": label, "dry_run": True, "estimated_bytes": job.total_bytes_processed}
            )
            print(
                f"  DRY RUN {label:<40} estimated={job.total_bytes_processed or 0:>14,}", flush=True
            )
            return None
        rows = [dict(r) for r in job.result()]
        stats.append(
            {
                "step": label,
                "job_id": job.job_id,
                "bytes_billed": job.total_bytes_billed,
                "slot_ms": job.slot_millis,
                "duration_ms": int((job.ended - job.started).total_seconds() * 1000),
            }
        )
        print(f"  {label:<48} billed={job.total_bytes_billed or 0:>14,}", flush=True)
        return rows

    def land(local: pathlib.Path, columns: list[str], rows, table: str, schema, label: str):
        n = 0
        with gzip.open(local, "wt", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
            for row in rows:
                w.writerow([row[c] for c in columns])
                n += 1
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        obj = f"derived/restatement/{run_id.replace(':', '_')}/{local.name}"
        blob = bucket.blob(obj)
        blob.metadata = {"sha256": digest, "normalization_version": rules.version}
        blob.upload_from_filename(str(local), content_type="application/gzip")
        uri = f"gs://{args.bucket}/{obj}"
        job = client.load_table_from_uri(
            uri,
            table,
            job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.CSV,
                field_delimiter="\t",
                quote_character='"',
                allow_quoted_newlines=True,
                write_disposition="WRITE_TRUNCATE",
                schema=schema,
            ),
        )
        job.result()
        stats.append(
            {
                "step": label,
                "load_job_id": job.job_id,
                "rows": n,
                "bytes": local.stat().st_size,
                "sha256": digest,
                "object": uri,
            }
        )
        print(f"  {label:<48} rows={n:>12,}  sha256={digest[:12]}", flush=True)
        if job.output_rows != n:
            raise SystemExit(f"{table}: loaded {job.output_rows}, generated {n}")
        return n, digest, uri

    # --- 1. the affected cohort, defined before anything is rebuilt ------------------------
    cohort = q(
        f"""
        SELECT COUNT(*) AS affected_listens,
               COUNT(DISTINCT FORMAT('%t|%t', n.artist_name, n.recording_name)) AS affected_pairs
        FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
        JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` m USING (listen_hash)
        WHERE {period('n')} AND {period('m')}
          AND {COHORT_SQL}
    """,
        "affected cohort size",
    )[0]
    affected_listens = int(cohort["affected_listens"])
    print(
        f"  affected cohort: {affected_listens:,} listens, "
        f"{int(cohort['affected_pairs']):,} distinct pairs "
        f"({100 * affected_listens / EXPECTED_LISTENS:.4f}% of the corpus)",
        flush=True,
    )
    # The registry records what this predicate measured when the run was published. A different
    # count here means the identity would name a run that is not the one being executed.
    if affected_listens != COHORT.measured_listens:
        raise SystemExit(
            f"cohort {COHORT.cohort_key!r} now selects {affected_listens:,} listens but the registry "
            f"records {COHORT.measured_listens:,}. Identity and cohort have diverged; refusing to "
            f"restate under an id that would describe a different set of rows."
        )

    # --- 2. re-normalize ONLY the affected pairs -------------------------------------------
    pairs = q(
        f"""
        SELECT DISTINCT n.artist_name, n.recording_name
        FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
        JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` m USING (listen_hash)
        WHERE {period('n')} AND {period('m')}
          AND {COHORT_SQL}
    """,
        "affected distinct pairs",
    )

    SEP = "\x1f"

    def pair_rows():
        for row in pairs:
            a, rec = row["artist_name"] or "", row["recording_name"] or ""
            n = normalize(a, rec, rules=rules)
            yield {
                "pair_hash": hashlib.sha256(f"{a}{SEP}{rec}".encode()).hexdigest(),
                "artist_name": a,
                "recording_name": rec,
                "artist_normalized_unicode": n.artist_normalized_unicode,
                "recording_normalized_unicode": n.recording_normalized_unicode,
                "lookup_exact": n.lookup_exact,
                "lookup_fallback": n.lookup_fallback,
                "normalization_status": n.normalization_status.value,
                "exact_key_status": n.exact_key_status.value,
                "fallback_key_status": n.fallback_key_status.value,
            }

    pair_columns = [
        "pair_hash",
        "artist_name",
        "recording_name",
        "artist_normalized_unicode",
        "recording_normalized_unicode",
        "lookup_exact",
        "lookup_fallback",
        "normalization_status",
        "exact_key_status",
        "fallback_key_status",
    ]
    from google.cloud.bigquery import SchemaField

    pair_schema = [SchemaField(c, "STRING") for c in pair_columns]
    pair_table = f"{PROJECT}.splitsheet_silver.stg_restated_pairs"
    n_pairs, pair_digest, _ = land(
        work / "restated_pairs.tsv.gz",
        pair_columns,
        pair_rows(),
        pair_table,
        pair_schema,
        "land re-normalized pairs",
    )

    # --- 3. transliterated canonical index rows --------------------------------------------
    #
    # Only canonical recordings whose artist or title contains an enabled script can gain a key, so
    # only those are pulled. 1,035,644 rows against the snapshot's 31,554,198.
    canonical = client.query(
        f"""
        SELECT recording_mbid, artist_credit_name, recording_name
        FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
        WHERE snapshot_date = DATE '{SNAPSHOT}'
          AND (REGEXP_CONTAINS(recording_name, r'{HANGUL_RE}')
            OR REGEXP_CONTAINS(recording_name, r'{KANA_RE}')
            OR REGEXP_CONTAINS(artist_credit_name, r'{HANGUL_RE}')
            OR REGEXP_CONTAINS(artist_credit_name, r'{KANA_RE}'))
    """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES),
    )
    canonical_result = canonical.result()
    stats.append(
        {
            "step": "canonical rows in scope",
            "job_id": canonical.job_id,
            "bytes_billed": canonical.total_bytes_billed,
            "rows": canonical_result.total_rows,
        }
    )
    print(
        f"  canonical rows in scope: {canonical_result.total_rows:,}  "
        f"billed={canonical.total_bytes_billed or 0:,}",
        flush=True,
    )

    def index_rows():
        for row in canonical_result:
            a, rec = row["artist_credit_name"] or "", row["recording_name"] or ""
            if not (
                would_transliterate(a, rules.transliteration_scripts)
                or would_transliterate(rec, rules.transliteration_scripts)
            ):
                continue
            n = normalize(a, rec, rules=rules)
            # EXACT stage only. The fallback stage is left alone: its keys already exist under v1 for
            # these recordings where they can exist at all, and adding transliterated fallback keys
            # would widen the change beyond the trigger.
            if n.lookup_exact:
                yield {
                    "recording_mbid": row["recording_mbid"],
                    "lookup_stage": "EXACT",
                    "lookup_key": n.lookup_exact,
                }

    index_columns = ["recording_mbid", "lookup_stage", "lookup_key"]
    index_table = f"{PROJECT}.splitsheet_bronze.stg_restated_index"
    n_index, index_digest, _ = land(
        work / "restated_index.tsv.gz",
        index_columns,
        index_rows(),
        index_table,
        [SchemaField(c, "STRING") for c in index_columns],
        "land transliterated canonical index",
    )

    # --- 4. block, score and decide for the cohort -----------------------------------------
    #
    # One statement, so an intermediate state cannot be observed. It rebuilds candidates for the
    # cohort against the union of the v1 index and the transliterated additions, applies the FROZEN
    # scoring rules, and writes the restated match table for every listen: cohort rows recomputed,
    # everything else copied from v1.
    restated = f"{PROJECT}.splitsheet_silver.silver_listen_matches_restated"
    q(
        f"""
    CREATE OR REPLACE TABLE `{restated}`
    PARTITION BY DATE(listened_at)
    CLUSTER BY match_method, recording_cohort AS
    WITH cohort AS (
      SELECT n.listen_hash, n.listened_at, n.artist_name, n.recording_name,
             SHA256(CONCAT(n.artist_name, '\\x1f', n.recording_name)) AS ph
      FROM `{PROJECT}.splitsheet_silver.silver_listens_normalized` n
      JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` m USING (listen_hash)
      WHERE {period('n')} AND {period('m')}
        AND {COHORT_SQL}
    ),
    cohort_norm AS (
      SELECT c.listen_hash, c.listened_at,
             p.artist_normalized_unicode, p.recording_normalized_unicode,
             p.lookup_exact, p.lookup_fallback, p.normalization_status,
             p.exact_key_status, p.fallback_key_status
      FROM cohort c
      JOIN `{pair_table}` p ON p.pair_hash = TO_HEX(c.ph)
    ),
    index_all AS (
      SELECT recording_mbid, lookup_key FROM `{PROJECT}.splitsheet_bronze.canonical_blocking_index`
      WHERE lookup_stage = 'EXACT'
      UNION DISTINCT
      SELECT recording_mbid, lookup_key FROM `{index_table}`
    ),
    candidates AS (
      SELECT n.listen_hash, i.recording_mbid AS candidate_recording_mbid
      FROM cohort_norm n
      JOIN index_all i ON i.lookup_key = n.lookup_exact
      WHERE n.exact_key_status = 'AVAILABLE'
    ),
    counted AS (
      SELECT listen_hash, COUNT(*) AS candidate_count,
             MIN(candidate_recording_mbid) AS min_candidate
      FROM candidates GROUP BY listen_hash
    ),
    sole AS (
      SELECT listen_hash, MIN(candidate_recording_mbid) AS sole_candidate
      FROM candidates GROUP BY listen_hash HAVING COUNT(*) = 1
    ),
    cohort_decided AS (
      SELECT
        n.listen_hash, n.listened_at,
        IF(s.sole_candidate IS NOT NULL, s.sole_candidate, NULL) AS matched_recording_mbid,
        IF(s.sole_candidate IS NOT NULL, 'MATCHED', 'UNRESOLVED') AS match_status,
        CASE WHEN c.candidate_count >= 1 THEN 'C' ELSE 'E' END AS match_tier,
        CASE WHEN s.sole_candidate IS NOT NULL THEN 'STRUCTURAL_EXACT_UNIQUE'
             WHEN c.candidate_count > 1 THEN 'SCORED_EXACT_MULTIPLE'
             ELSE 'NO_CANDIDATES' END AS match_method,
        CAST(NULL AS FLOAT64) AS match_score,
        CAST(NULL AS FLOAT64) AS score_margin,
        CASE
          WHEN s.sole_candidate IS NOT NULL THEN NULL
          WHEN c.candidate_count > 1 THEN 'AMBIGUOUS_TIE'
          WHEN n.normalization_status = 'MISSING_ARTIST' THEN 'MISSING_ARTIST'
          WHEN n.normalization_status = 'MISSING_RECORDING' THEN 'MISSING_RECORDING'
          WHEN n.normalization_status = 'NO_ALPHANUMERIC_CONTENT' THEN 'NO_ALPHANUMERIC_CONTENT'
          WHEN n.exact_key_status = 'PARTIAL' THEN 'NO_LOOKUP_KEY_PARTIAL'
          WHEN n.exact_key_status = 'EMPTY' THEN 'NO_LOOKUP_KEY_EMPTY'
          ELSE 'NO_BLOCK_CANDIDATES' END AS failure_reason,
        IF(c.candidate_count >= 1, 'EXACT', NULL) AS block_method,
        IFNULL(c.candidate_count, 0) AS candidate_count,
        'NORMAL' AS blocking_key_information_class,
        TRUE AS recording_cohort
      FROM cohort_norm n
      LEFT JOIN counted c USING (listen_hash)
      LEFT JOIN sole s USING (listen_hash)
    ),
    unaffected AS (
      SELECT listen_hash, listened_at, matched_recording_mbid, match_status, match_tier,
             match_method, match_score, score_margin, failure_reason, block_method,
             candidate_count, blocking_key_information_class, FALSE AS recording_cohort
      FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
      WHERE {PERIOD}
        AND listen_hash NOT IN (SELECT listen_hash FROM cohort)
    )
    SELECT *, '{rules.version}' AS normalization_version,
           '{BLOCKING_VERSION}' AS blocking_version,
           '{scoring.version}' AS scoring_version,
           '{run_id}' AS restatement_run_id,
           CURRENT_TIMESTAMP() AS matched_at
    FROM (SELECT * FROM cohort_decided UNION ALL SELECT * FROM unaffected)
    """,
        "rebuild cohort and copy the rest",
    )

    # --- 5. prove the shape and that nothing outside the cohort moved ----------------------
    checks = q(
        f"""
        SELECT
          (SELECT COUNT(*) FROM `{restated}` WHERE {PERIOD}) AS total_rows,
          (SELECT COUNT(DISTINCT listen_hash) FROM `{restated}` WHERE {PERIOD}) AS distinct_listens,
          (SELECT COUNTIF(recording_cohort) FROM `{restated}` WHERE {PERIOD}) AS cohort_rows,
          (SELECT COUNT(*) FROM `{restated}` r
             JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` v USING (listen_hash)
             WHERE {period('r')} AND {period('v')} AND NOT r.recording_cohort
               AND (IFNULL(r.matched_recording_mbid, 'x') != IFNULL(v.matched_recording_mbid, 'x')
                 OR r.match_status != v.match_status
                 OR r.match_method != v.match_method
                 OR IFNULL(r.failure_reason, 'x') != IFNULL(v.failure_reason, 'x')
                 OR r.candidate_count != v.candidate_count)) AS unaffected_rows_that_moved
    """,
        "prove the unaffected cohort did not move",
    )[0]

    if int(checks["total_rows"]) != EXPECTED_LISTENS:
        raise SystemExit(
            f"restated table holds {checks['total_rows']}, expected {EXPECTED_LISTENS}"
        )
    if int(checks["distinct_listens"]) != EXPECTED_LISTENS:
        raise SystemExit("restated table does not have one row per listen")
    if int(checks["unaffected_rows_that_moved"]) != 0:
        raise SystemExit(
            f"{checks['unaffected_rows_that_moved']} listens outside the cohort "
            f"changed; the restatement is not contained"
        )

    lost_check = q(
        f"""
        SELECT COUNTIF(v.match_status = 'MATCHED' AND r.match_status != 'MATCHED') AS lost
        FROM `{restated}` r
        JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')} AND r.recording_cohort
    """,
        "prove no v1 match was lost",
    )[0]
    if int(lost_check["lost"]) != 0:
        raise SystemExit(
            f"{lost_check['lost']} listens lost a v1 match; the cohort definition is "
            f"supposed to make that impossible"
        )

    transitions = q(
        f"""
        SELECT v.match_status AS prior_status, v.failure_reason AS prior_reason,
               r.match_status AS new_status, r.failure_reason AS new_reason,
               r.match_method AS new_method, COUNT(*) AS listens
        FROM `{restated}` r
        JOIN `{PROJECT}.splitsheet_silver.silver_listen_matches` v USING (listen_hash)
        WHERE {period('r')} AND {period('v')} AND r.recording_cohort
        GROUP BY 1, 2, 3, 4, 5 ORDER BY listens DESC
    """,
        "cohort transitions",
    )

    newly_matched = sum(
        int(t["listens"])
        for t in transitions
        if t["prior_status"] != "MATCHED" and t["new_status"] == "MATCHED"
    )
    newly_ambiguous = sum(
        int(t["listens"]) for t in transitions if t["new_reason"] == "AMBIGUOUS_TIE"
    )
    still_unmatched = sum(
        int(t["listens"])
        for t in transitions
        if t["prior_status"] != "MATCHED" and t["new_status"] != "MATCHED"
    )
    lost = sum(
        int(t["listens"])
        for t in transitions
        if t["prior_status"] == "MATCHED" and t["new_status"] != "MATCHED"
    )

    for table in (pair_table, index_table):
        q(f"DROP TABLE IF EXISTS `{table}`", f"drop staging {table.split('.')[-1]}")

    report = {
        "artifact": "restatement_reprocessing",
        "restatement_run_id": run_id,
        "identity": {
            "scheme": identity.canonical_payload()["identity_scheme"],
            "canonical_inputs": identity.canonical_payload(),
            "inputs_digest": identity.inputs_digest,
            "cohort_key": COHORT.cohort_key,
            "cohort_sha256": COHORT.digest,
            "superseded_run_id": superseded,
            "why_superseded": LEGACY_RUN_ID.why_insufficient,
        },
        "prior_normalization_version": v1_rules.version,
        "new_normalization_version": rules.version,
        "scoring_version_unchanged": scoring.version,
        "blocking_version": BLOCKING_VERSION,
        "cohort": {
            "definition": COHORT.canonical_predicate(),
            "definition_source": "config/restatement_cohorts.yml, hashed into the run identity",
            "generated_sql": COHORT_SQL,
            "first_implementation_deviation": (
                "the script test alone pulled in 395,540 listens that already had a key and cost "
                "29,954 previously matched ones; restricted to the frozen definition, no v1 match "
                "can be lost because every listen in scope was unmatched"
            ),
            "affected_listens": affected_listens,
            "affected_pairs": int(cohort["affected_pairs"]),
            "pct_of_corpus": round(100 * affected_listens / EXPECTED_LISTENS, 6),
            "renormalized_pairs": n_pairs,
            "renormalized_pairs_sha256": pair_digest,
            "transliterated_canonical_index_rows": n_index,
            "transliterated_index_sha256": index_digest,
        },
        "shape": {k: int(v) for k, v in checks.items()},
        "transitions": transitions,
        "summary": {
            "newly_matched": newly_matched,
            "still_unmatched": still_unmatched,
            "newly_ambiguous": newly_ambiguous,
            "previously_matched_now_unmatched": lost,
        },
        "cost": {
            "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
            "estimated_bytes_dry_run": sum(s.get("estimated_bytes") or 0 for s in stats),
            "list_price_equivalent_usd": round(
                sum(s.get("bytes_billed") or 0 for s in stats) / 1024**4 * ON_DEMAND_USD_PER_TIB, 4
            ),
            "caveat": "list-price equivalent; actual monetary cost UNKNOWN without billing evidence",
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    print(
        f"\n  cohort {affected_listens:,} listens "
        f"({report['cohort']['pct_of_corpus']}% of the corpus)"
    )
    print(f"  unaffected listens that moved: {int(checks['unaffected_rows_that_moved'])}")
    print(
        f"  newly matched {newly_matched:,}   still unmatched {still_unmatched:,}   "
        f"newly ambiguous {newly_ambiguous:,}   lost matches {lost}"
    )
    print("\n  transitions (top 10):")
    for t in transitions[:10]:
        print(
            f"    {t['prior_reason'] or t['prior_status']!s:<26} -> "
            f"{t['new_reason'] or t['new_status']!s:<22} {int(t['listens']):>10,}  "
            f"[{t['new_method']}]"
        )


if __name__ == "__main__":
    main()
