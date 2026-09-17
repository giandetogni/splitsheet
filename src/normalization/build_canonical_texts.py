"""Normalize the canonical text of the recordings that scoring actually has to compare.

Scoring compares full Unicode text on both sides. The listen side already has it
(`silver_listens_normalized`); the canonical side does not, because Phase 3B only needed
ASCII lookup keys from the snapshot. This builder fills that gap using the SAME Python
library, so the normalization rules keep exactly one implementation.

Universe, and why it is not the whole snapshot: the scored universe is 3,045,208 candidate
pairs covering 368,795 distinct canonical recordings out of 31,554,198. Normalizing the
whole snapshot would be 85x the work for no consumer. `source_universe` records which
candidate run defined the set, so widening the candidate set requires re-running this
builder rather than silently scoring against stale coverage.

Runs as the human ADC identity, like every other ingestion step. The matcher identity gets
read access to the resulting table and nothing else.

Steps: query -> normalize locally -> gzipped TSV -> GCS -> staging load -> INSERT -> drop.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from normalization import load_rules
from normalization.normalizer import normalized_unicode

PROJECT = "ss-de-944054e7"
SNAPSHOT = "2026-07-17"
MAX_BYTES = 200 * 1024**3
GCS_PREFIX = "derived/canonical_texts"


def rules_for_version(requested: str):
    """Load the rules file that DECLARES `requested`, never whatever is current.

    Frozen copies live at config/normalization_rules_v<semver>.yml and the working file at
    config/normalization_rules.yml. Both are candidates, but the one that gets used has to
    declare the version that was asked for, so naming a version can never quietly resolve
    to a different one -- which is how this table was once rebuilt under 1.1.0 while the
    DAG was pinned to 1.0.0.
    """
    cfg = pathlib.Path(__file__).parents[2] / "config"
    semver = requested.split("+", 1)[0]
    candidates = [cfg / f"normalization_rules_v{semver}.yml", cfg / "normalization_rules.yml"]
    found = []
    for path in candidates:
        if not path.exists():
            continue
        rules = load_rules(path)
        if rules.version == requested:
            return rules, path
        found.append(f"{path.name} declares {rules.version}")
    raise SystemExit(
        f"no rules file declares normalization version {requested}. "
        f"expected {requested}, found: {found or 'no rules file at all'}")


def source_rows(client, candidate_run_id: str):
    """Distinct canonical recordings that are a candidate for a listen needing a score.

    EXACT-unique listens are excluded: their decision is structural, so their candidate's
    text is never compared. That is the difference between 368,795 recordings and millions.
    """
    from google.cloud import bigquery

    sql = f"""
    WITH universe AS (
      SELECT listen_hash
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates`
      WHERE candidate_run_id = @run
      GROUP BY listen_hash
      HAVING COUNT(*) > 1 OR MIN(block_method) = 'FALLBACK'
    ),
    mbids AS (
      SELECT DISTINCT c.candidate_recording_mbid AS recording_mbid
      FROM `{PROJECT}.splitsheet_silver.silver_match_candidates` c
      JOIN universe USING (listen_hash)
      WHERE c.candidate_run_id = @run
    )
    SELECT k.recording_mbid, k.artist_credit_name, k.recording_name, k.release_name
    FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings` k
    JOIN mbids USING (recording_mbid)
    WHERE k.snapshot_date = DATE '{SNAPSHOT}'
    ORDER BY k.recording_mbid
    """
    cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES,
        query_parameters=[bigquery.ScalarQueryParameter("run", "STRING", candidate_run_id)])
    job = client.query(sql, job_config=cfg)
    rows = job.result()
    print(f"  source query billed={job.total_bytes_billed or 0:,} "
          f"rows={rows.total_rows:,}", flush=True)
    return rows, job


def published_identity(client, rules, candidate_run_id: str):
    """The identity already published for exactly these frozen inputs, or None.

    `ingestion_run_id` is derived from the payload, so it cannot be known before building.
    What can be checked first is whether the published rows already carry the inputs being
    asked for: the candidate universe, the canonical snapshot and the normalization rules.
    Those three are frozen upstream, so together they determine the payload -- which makes
    a rerun on unchanged inputs a no-op instead of a new key for the same content.
    """
    row = next(iter(client.query(f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT ingestion_run_id) AS runs, MIN(ingestion_run_id) AS run_id,
               COUNT(DISTINCT normalization_version) AS versions,
               MIN(normalization_version) AS version,
               MIN(normalization_rules_sha256) AS rules_digest,
               COUNT(DISTINCT source_universe) AS universes,
               MIN(source_universe) AS universe,
               COUNT(DISTINCT snapshot_date) AS snapshots, MIN(snapshot_date) AS snapshot
        FROM `{PROJECT}.splitsheet_bronze.canonical_match_texts`
    """).result()))
    matches = (int(row["n"]) > 0 and int(row["runs"]) == 1 and int(row["versions"]) == 1
               and int(row["universes"]) == 1 and int(row["snapshots"]) == 1
               and row["version"] == rules.version
               and row["rules_digest"] == rules.rules_digest
               and row["universe"] == candidate_run_id
               and str(row["snapshot"]) == SNAPSHOT)
    return dict(row) if matches else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-run-id", required=True)
    ap.add_argument("--norm-version", required=True,
                    help="the frozen normalization version to build under, e.g. "
                         "1.0.0+0bc0dd643e06. The rules file that declares it is "
                         "the one that is loaded; there is no latest fallback.")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--work-dir", required=True, help="local scratch, outside the repo")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery, storage

    rules, rules_path = rules_for_version(args.norm_version)
    print(f"normalization {rules.version} from {rules_path.name}", flush=True)
    client = bigquery.Client(project=PROJECT)
    t0 = time.time()

    # Before the source query, before the local file, before GCS and before the target.
    published = published_identity(client, rules, args.candidate_run_id)
    if published:
        print(f"target already holds {published['run_id']} for these inputs: nothing written",
              flush=True)
        report = {
            "artifact": "canonical_match_texts", "skipped": True,
            "source_universe": args.candidate_run_id, "snapshot_date": SNAPSHOT,
            "recordings": int(published["n"]),
            "ingestion_run_id": published["run_id"],
            "normalization_version": rules.version,
            "normalization_rules_sha256": rules.rules_digest,
            "wall_seconds": round(time.time() - t0, 1),
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)
        print(json.dumps(report, indent=1))
        return

    local = pathlib.Path(args.work_dir) / "canonical_match_texts.tsv.gz"
    local.parent.mkdir(parents=True, exist_ok=True)

    rows, src_job = source_rows(client, args.candidate_run_id)
    n = 0
    empty_artist = empty_recording = no_release = 0
    # mtime=0 and an empty filename: the gzip header must not carry a timestamp, or the
    # digest below -- and so the run id -- would change for identical content.
    with open(local, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for r in rows:
            artist = normalized_unicode(r["artist_credit_name"] or "", rules=rules)
            recording = normalized_unicode(r["recording_name"] or "", rules=rules)
            release = (r["release_name"] or "").casefold().strip()
            empty_artist += not artist
            empty_recording += not recording
            no_release += not release
            w.writerow([r["recording_mbid"], artist, recording, release])
            n += 1
            if n % 100_000 == 0:
                print(f"  {n:,} recordings normalized", flush=True)

    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    run_id = "cantext:" + hashlib.sha256(
        f"{rules.version}|{rules.rules_digest}|{args.candidate_run_id}|{n}|{digest}".encode()
    ).hexdigest()[:16]

    object_name = (f"{GCS_PREFIX}/version={rules.version}/"
                   f"universe={args.candidate_run_id.replace(':', '_')}/"
                   "canonical_match_texts.tsv.gz")
    blob = storage.Client(project=PROJECT).bucket(args.bucket).blob(object_name)
    blob.metadata = {"sha256": digest, "normalization_version": rules.version}
    blob.upload_from_filename(str(local), content_type="application/gzip")
    uri = f"gs://{args.bucket}/{object_name}"
    print(f"  uploaded {local.stat().st_size:,} bytes -> {uri}", flush=True)

    # Staged load, then INSERT. Loading straight into the Terraform-managed table with
    # --replace semantics is what overwrote a 13-column schema with a 10-column one in
    # Phase 3A; the staging table absorbs that risk instead.
    stg = f"{PROJECT}.splitsheet_bronze.stg_canonical_texts"
    load_cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        field_delimiter="\t",
        quote_character='"',
        allow_quoted_newlines=True,
        write_disposition="WRITE_TRUNCATE",
        schema=[
            bigquery.SchemaField("recording_mbid", "STRING"),
            bigquery.SchemaField("artist_normalized_unicode", "STRING"),
            bigquery.SchemaField("recording_normalized_unicode", "STRING"),
            bigquery.SchemaField("release_lower", "STRING"),
        ])
    load = client.load_table_from_uri(uri, stg, job_config=load_cfg)
    load.result()
    print(f"  loaded {load.output_rows:,} rows into staging", flush=True)
    if load.output_rows != n:
        raise SystemExit(f"staging holds {load.output_rows} rows, expected {n}")

    ins = client.query(f"""
        BEGIN TRANSACTION;
        DELETE FROM `{PROJECT}.splitsheet_bronze.canonical_match_texts` WHERE TRUE;
        INSERT INTO `{PROJECT}.splitsheet_bronze.canonical_match_texts`
        SELECT DATE '{SNAPSHOT}', recording_mbid,
               IFNULL(artist_normalized_unicode, ''), IFNULL(recording_normalized_unicode, ''),
               NULLIF(IFNULL(release_lower, ''), ''),
               '{rules.version}', '{rules.rules_digest}',
               '{args.candidate_run_id}', '{run_id}', CURRENT_TIMESTAMP()
        FROM `{stg}`;
        COMMIT TRANSACTION;
    """, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
    ins.result()
    client.query(f"DROP TABLE IF EXISTS `{stg}`").result()

    check = next(iter(client.query(f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT recording_mbid) AS distinct_mbid,
               COUNTIF(artist_normalized_unicode = '') AS empty_artist,
               COUNTIF(recording_normalized_unicode = '') AS empty_recording,
               COUNTIF(release_lower IS NULL) AS no_release
        FROM `{PROJECT}.splitsheet_bronze.canonical_match_texts`
    """).result()))
    if check["n"] != n or check["distinct_mbid"] != n:
        raise SystemExit(f"published table disagrees with the build: {dict(check)} vs {n}")

    report = {
        "artifact": "canonical_match_texts",
        "source_universe": args.candidate_run_id,
        "snapshot_date": SNAPSHOT,
        "recordings": n,
        "object": uri,
        "bytes": local.stat().st_size,
        "sha256": digest,
        "ingestion_run_id": run_id,
        "normalization_version": rules.version,
        "normalization_rules_sha256": rules.rules_digest,
        "empty_artist_unicode": empty_artist,
        "empty_recording_unicode": empty_recording,
        "recordings_without_release": no_release,
        "published": {k: int(v) for k, v in dict(check).items()},
        "bytes_billed": (src_job.total_bytes_billed or 0) + (ins.total_bytes_billed or 0),
        "wall_seconds": round(time.time() - t0, 1),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
