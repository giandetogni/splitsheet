"""Load the preserved 2026-06 slice into bronze_listens and the eval label table.

Failure-safe publication. The previous design was not, and the three defects were:

  * it ran TRUNCATE before the replacement rows existed, so the empty table was a
    published state;
  * bronze and labels were four independent statements, so a crash between them left
    bronze at run N and labels empty;
  * there was no transaction, so the swap was never atomic.

This version materialises both targets into run-scoped staging tables, validates them
completely, and only then swaps both into place inside a single BigQuery multi-statement
transaction. A failure at any point before COMMIT leaves the previous published state
untouched and internally consistent -- bronze and labels can never be left on different
runs.

Idempotency strategy: DETERMINISTIC REPLACEMENT, guarded by a pre-check.

  Chosen: if the targets already hold exactly the expected state for this run_id, write
  nothing and report `skipped`. Otherwise stage, validate, and swap. Replacement covers
  exactly the period's partitions because the source is one frozen slice spanning
  precisely [2026-06-01, 2026-07-01).

  Rejected: MERGE on listen_hash. It would join 38.2 M against 38.2 M on every run and
  still need a partition predicate to satisfy require_partition_filter, buying nothing
  when the source is immutable and the key has zero measured collisions.

  Evidence that would change it: late arrivals into an already published partition,
  several slices writing the same table, or partial partition coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time

PERIOD_START = "2026-06-01 00:00:00+00"
PERIOD_END = "2026-07-01 00:00:00+00"
EXPECTED_IN_PERIOD = 38_199_641
EXPECTED_OUTSIDE = 1_000_359
EXPECTED_SOURCE_TOTAL = 39_200_000
DUMP_ID = "listenbrainz-dump-2593-20260712-000004-full"
SLICE_PREFIX = "raw/listenbrainz/fullexport-2593/period=2026-06"

HASH_EXPR = """TO_HEX(SHA256(CONCAT(CAST(user_id AS STRING), '|',
                       CAST(UNIX_MICROS(listened_at) AS STRING), '|',
                       recording_msid)))"""

PERIOD_PREDICATE = (f"listened_at >= TIMESTAMP '{PERIOD_START}' "
                    f"AND listened_at < TIMESTAMP '{PERIOD_END}'")

ALLOWED_BRONZE_COLUMNS = [
    "listen_hash", "listened_at", "submitted_at", "recording_msid", "artist_name",
    "recording_name", "release_name", "source_file", "dump_id", "ingestion_run_id",
    "ingested_at",
]
BANNED_COLUMNS = {"user_id", "recording_mbid", "release_mbid", "artist_credit_id",
                  "artist_credit_mbids", "mapper_recording_mbid"}


class ContractError(RuntimeError):
    """Raised when the source no longer matches the manifest it was measured against."""


def load_manifest(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def run_id_from_manifest(man: dict) -> str:
    """Deterministic per slice content, so re-running yields the same identifier."""
    digest = hashlib.sha256("|".join(e["sha256"] for e in man["members"]).encode()).hexdigest()
    return f"{man['slice_id']}:{digest[:16]}"


def verify_source_contract(man: dict, bucket_name: str) -> dict:
    """Abort before any query or publication if GCS diverges from the manifest.

    Checks object count, unexpected objects, per-object size, per-object SHA-256 (carried
    as custom metadata) and the total byte sum. A wildcard source would make most of this
    uncheckable, which is why the source is an explicit list.
    """
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    expected = {e["member"]: e for e in man["members"]}

    found = {}
    for blob in client.list_blobs(bucket, prefix=SLICE_PREFIX + "/"):
        name = blob.name.rsplit("/", 1)[-1]
        if name == "_manifest.json":
            continue
        found[name] = blob

    problems = []
    if len(found) != 23:
        problems.append(f"expected 23 objects, found {len(found)}")
    unexpected = sorted(set(found) - set(expected))
    missing = sorted(set(expected) - set(found))
    if unexpected:
        problems.append(f"unexpected objects: {unexpected}")
    if missing:
        problems.append(f"missing objects: {missing}")

    total = 0
    for name, entry in expected.items():
        blob = found.get(name)
        if blob is None:
            continue
        blob.reload()
        total += blob.size
        if blob.size != entry["size_bytes"]:
            problems.append(f"{name}: size {blob.size} != manifest {entry['size_bytes']}")
        sha = (blob.metadata or {}).get("sha256")
        if sha != entry["sha256"]:
            problems.append(f"{name}: sha256 metadata mismatch")

    if total != man["total_bytes"]:
        problems.append(f"total bytes {total} != manifest {man['total_bytes']}")
    if problems:
        raise ContractError("source contract violated: " + "; ".join(problems))

    return {"objects": len(found), "total_bytes": total, "checksums_verified": len(expected)}


def query(client, sql: str, label: str, stats: list) -> list:
    job = client.query(sql)
    rows = [dict(r) for r in job.result()]
    stats.append({
        "step": label,
        "job_id": job.job_id,
        "bytes_processed": job.total_bytes_processed,
        "bytes_billed": job.total_bytes_billed,
        "slot_ms": job.slot_millis,
        "duration_ms": int((job.ended - job.started).total_seconds() * 1000),
        "dml_affected_rows": job.num_dml_affected_rows,
    })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-before-publish", action="store_true",
                    help="inject a failure after staging is validated, to prove the "
                         "published state survives a crash mid-run")
    ap.add_argument("--force", action="store_true", help="reload even if already current")
    ap.add_argument("--cleanup-orphan-staging", action="store_true",
                    help="drop staging tables left behind by failed runs, listing each "
                         "one dropped so the cleanup is auditable rather than silent")
    args = ap.parse_args()

    from google.cloud import bigquery

    client = bigquery.Client(project=args.project)
    man = load_manifest(args.manifest)
    run_id = run_id_from_manifest(man)
    suffix = re.sub(r"[^0-9a-z]", "_", run_id.lower())[-24:]
    stg_bronze = f"{args.project}.splitsheet_bronze.stg_bronze_listens_{suffix}"
    stg_labels = f"{args.project}.splitsheet_eval.stg_labels_{suffix}"
    stats: list = []
    t0 = time.time()

    if args.cleanup_orphan_staging:
        dropped = []
        for ds, pattern in (("splitsheet_bronze", "stg_bronze_listens_"),
                            ("splitsheet_eval", "stg_labels_")):
            rows = query(client, f"""
                SELECT table_name FROM `{args.project}.{ds}`.INFORMATION_SCHEMA.TABLES
                WHERE table_name LIKE '{pattern}%'
            """, f"list_staging_{ds}", stats)
            for r in rows:
                name = r["table_name"]
                query(client, f"DROP TABLE `{args.project}.{ds}.{name}`",
                      "drop_orphan_staging", stats)
                dropped.append(f"{ds}.{name}")
                print(f"  dropped orphan staging: {ds}.{name}")
        if not dropped:
            print("  no orphan staging tables found")
        with open(args.out, "w") as fh:
            json.dump({"action": "cleanup_orphan_staging", "dropped": dropped,
                       "jobs": stats}, fh, indent=1)
        return

    contract = verify_source_contract(man, args.bucket)
    print(f"source contract OK: {contract['objects']} objects, "
          f"{contract['total_bytes']:,} bytes, {contract['checksums_verified']} checksums")

    recon = query(client, f"""
        SELECT COUNT(*) AS source_total_rows,
               COUNTIF({PERIOD_PREDICATE}) AS in_period_rows,
               COUNTIF(NOT ({PERIOD_PREDICATE})) AS outside_target_period_rows,
               COUNTIF(listened_at IS NULL) AS unclassified_rows
        FROM `{args.project}.splitsheet_bronze.ext_listens_2026_06`
    """, "reconcile_source", stats)[0]
    total, inside, outside = (int(recon["source_total_rows"]), int(recon["in_period_rows"]),
                              int(recon["outside_target_period_rows"]))
    if (inside + outside != total or int(recon["unclassified_rows"]) != 0
            or total != EXPECTED_SOURCE_TOTAL or inside != EXPECTED_IN_PERIOD
            or outside != EXPECTED_OUTSIDE):
        raise ContractError(f"reconciliation failed: {recon}")

    current = query(client, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT listen_hash) AS distinct_hashes,
               COUNT(DISTINCT ingestion_run_id) AS run_ids, MIN(ingestion_run_id) AS run_id
        FROM `{args.project}.splitsheet_bronze.bronze_listens`
        WHERE {PERIOD_PREDICATE}
    """, "inspect_target", stats)[0]
    already = (int(current["n"]) == EXPECTED_IN_PERIOD
               and int(current["distinct_hashes"]) == EXPECTED_IN_PERIOD
               and int(current["run_ids"]) == 1 and current["run_id"] == run_id)

    inserted = skipped = failed = 0
    published = False
    if already and not args.force:
        skipped = EXPECTED_IN_PERIOD
        print(f"target already holds run {run_id}: nothing written")
    else:
        # --- stage (targets untouched) ---
        query(client, f"""
            CREATE OR REPLACE TABLE `{stg_bronze}`
            PARTITION BY DATE(listened_at) AS
            SELECT {HASH_EXPR} AS listen_hash, listened_at, created AS submitted_at,
                   recording_msid, artist_name, recording_name, release_name,
                   _FILE_NAME AS source_file, '{DUMP_ID}' AS dump_id,
                   '{run_id}' AS ingestion_run_id, CURRENT_TIMESTAMP() AS ingested_at
            FROM `{args.project}.splitsheet_bronze.ext_listens_2026_06`
            WHERE {PERIOD_PREDICATE}
        """, "stage_bronze", stats)
        query(client, f"""
            CREATE OR REPLACE TABLE `{stg_labels}` AS
            SELECT {HASH_EXPR} AS listen_hash, recording_mbid AS mapper_recording_mbid,
                   recording_mbid IS NOT NULL AS label_available
            FROM `{args.project}.splitsheet_bronze.ext_listens_2026_06`
            WHERE {PERIOD_PREDICATE}
        """, "stage_labels", stats)

        # --- validate staging completely, before anything is published ---
        v = query(client, f"""
            SELECT
              (SELECT COUNT(*) FROM `{stg_bronze}`) AS bronze_rows,
              (SELECT COUNT(DISTINCT listen_hash) FROM `{stg_bronze}`) AS bronze_hashes,
              (SELECT COUNTIF(listen_hash IS NULL) FROM `{stg_bronze}`) AS bronze_null_hash,
              (SELECT COUNT(*) FROM `{stg_labels}`) AS label_rows,
              (SELECT COUNTIF(label_available) FROM `{stg_labels}`) AS labels_present,
              (SELECT COUNT(*) FROM `{stg_bronze}` b
                 JOIN `{stg_labels}` l USING (listen_hash)) AS joined_rows
        """, "validate_staging", stats)[0]
        stg_cols = [r["column_name"] for r in query(client, f"""
            SELECT column_name FROM `{args.project}.splitsheet_bronze`.INFORMATION_SCHEMA.COLUMNS
            WHERE table_name = 'stg_bronze_listens_{suffix}'
        """, "validate_staging_schema", stats)]
        checks = {
            "bronze_rows": int(v["bronze_rows"]) == EXPECTED_IN_PERIOD,
            "bronze_hashes_unique": int(v["bronze_hashes"]) == EXPECTED_IN_PERIOD,
            "no_null_hash": int(v["bronze_null_hash"]) == 0,
            "label_rows": int(v["label_rows"]) == EXPECTED_IN_PERIOD,
            "bronze_labels_aligned": int(v["joined_rows"]) == EXPECTED_IN_PERIOD,
            "schema_exact": sorted(stg_cols) == sorted(ALLOWED_BRONZE_COLUMNS),
            "no_banned_columns": not (set(stg_cols) & BANNED_COLUMNS),
        }
        print("staging validation:", json.dumps(checks))
        if not all(checks.values()):
            failed = 1
            raise SystemExit(f"staging validation failed, nothing published: {checks}")

        if args.fail_before_publish:
            raise SystemExit(
                "INJECTED FAILURE after staging validation and before publication. "
                "Published tables are untouched.")

        # --- atomic swap: both targets or neither ---
        query(client, f"""
            BEGIN TRANSACTION;
            DELETE FROM `{args.project}.splitsheet_bronze.bronze_listens`
              WHERE {PERIOD_PREDICATE};
            INSERT INTO `{args.project}.splitsheet_bronze.bronze_listens`
              ({', '.join(ALLOWED_BRONZE_COLUMNS)})
              SELECT {', '.join(ALLOWED_BRONZE_COLUMNS)} FROM `{stg_bronze}`;
            DELETE FROM `{args.project}.splitsheet_eval.mapper_reference_labels`
              WHERE TRUE;
            INSERT INTO `{args.project}.splitsheet_eval.mapper_reference_labels`
              (listen_hash, mapper_recording_mbid, label_available)
              SELECT listen_hash, mapper_recording_mbid, label_available FROM `{stg_labels}`;
            COMMIT TRANSACTION;
        """, "publish_atomic", stats)
        inserted = EXPECTED_IN_PERIOD
        published = True

        # staging is dropped only after a successful commit, so a failed run leaves it
        # behind on purpose, named by run_id, for inspection
        for tbl in (stg_bronze, stg_labels):
            query(client, f"DROP TABLE IF EXISTS `{tbl}`", "drop_staging", stats)

    final = query(client, f"""
        SELECT (SELECT COUNT(*) FROM `{args.project}.splitsheet_bronze.bronze_listens`
                  WHERE {PERIOD_PREDICATE}) AS bronze_rows,
               (SELECT COUNT(DISTINCT listen_hash)
                  FROM `{args.project}.splitsheet_bronze.bronze_listens`
                  WHERE {PERIOD_PREDICATE}) AS bronze_distinct_hashes,
               (SELECT COUNT(*) FROM `{args.project}.splitsheet_eval.mapper_reference_labels`)
                 AS label_rows,
               (SELECT COUNTIF(label_available)
                  FROM `{args.project}.splitsheet_eval.mapper_reference_labels`)
                 AS labels_present
    """, "verify_published", stats)[0]

    report = {
        "ingestion_run_id": run_id,
        "source_contract": contract,
        "reconciliation": {
            "source_total_rows": total, "in_period_rows": inside,
            "outside_target_period_rows": outside,
            "classified_sum_equals_total": inside + outside == total,
            "unclassified_rows": int(recon["unclassified_rows"]),
        },
        "outcome": {"inserted": inserted, "skipped": skipped, "failed": failed,
                    "published": published},
        "bronze_rows": int(final["bronze_rows"]),
        "bronze_distinct_hashes": int(final["bronze_distinct_hashes"]),
        "label_rows": int(final["label_rows"]),
        "labels_present": int(final["labels_present"]),
        "total_bytes_billed": sum(s["bytes_billed"] or 0 for s in stats),
        "total_slot_ms": sum(s["slot_ms"] or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in
                      ("ingestion_run_id", "source_contract", "reconciliation", "outcome",
                       "bronze_rows", "bronze_distinct_hashes", "label_rows",
                       "labels_present", "total_bytes_billed", "total_slot_ms")}, indent=1))


if __name__ == "__main__":
    main()
