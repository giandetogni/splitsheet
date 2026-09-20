"""Generate the MODELED rights data and land it in BigQuery. Reproducible from the seed.

    ListenBrainz listens   REAL       MusicBrainz recordings   REAL
    rights holders         MODELED    ownership splits         MODELED
    rate cards             MODELED    royalty amounts          ILLUSTRATIVE, not observed

Steps: read the recording universe from the frozen match result -> generate locally with
src/rights/generator -> gzipped TSV -> GCS -> staged load -> INSERT -> drop staging.

The universe is checked against the frozen `match_run_id` before anything is generated. Rights
generated against a different catalogue would be a different dataset wearing the same version.

DELIBERATE DEFECTS ARE RECORDED HERE, NOT IN THE DATA. The report lists the exact number of
each defect class that was injected, so the quality layer's independent counts can be compared
against an expected figure. No column in the landed tables labels a row as defective: a source
that announces its own defects turns detection into a lookup.

--revise-payee-status makes a controlled change to N holders' payee_status and re-lands ONLY
rights_holders. That is what gives the dbt snapshot a second state to detect, which is where
SCD Type 2 is actually demonstrated. Ownership splits are untouched by it -- their temporal
model comes from the generator's validity intervals and is not SCD Type 2.
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
from rights.generator import (
    DEFECT_ORDER,
    RightsModel,
    holder_row,
    load_rights_model,
    orphan_rows,
    ownership_rows,
    rate_card_rows,
    select_defects,
)

PROJECT = "ss-de-944054e7"
MAX_BYTES = 100 * 1024**3
PERIOD = (
    "listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
    "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'"
)
GCS_PREFIX = "derived/rights"

HOLDER_COLUMNS = ["holder_id", "display_name", "holder_type", "payee_status", "model_scope"]
SPLIT_COLUMNS = [
    "recording_mbid",
    "rights_holder_id",
    "share_pct",
    "valid_from",
    "valid_to",
    "split_version_id",
]
RATE_COLUMNS = [
    "rate_card_id",
    "model_scope",
    "valid_from",
    "valid_to",
    "rate_per_stream",
    "currency",
    "rule_version_id",
]


def recording_universe(client, model: RightsModel) -> list[str]:
    """Every distinctly matched recording, eligible or risk-held.

    Risk-held recordings get rights too: a recording held under MATCH_RISK_POLICY today becomes
    eligible under a future policy with no regeneration, and some recordings are in both groups
    already.
    """
    from google.cloud import bigquery

    job = client.query(
        f"""
        SELECT DISTINCT matched_recording_mbid AS recording_mbid
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches`
        WHERE {PERIOD} AND match_status = 'MATCHED'
          AND match_run_id = '{model.universe_match_run_id}'
        ORDER BY recording_mbid
    """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES),
    )
    mbids = [r["recording_mbid"] for r in job.result()]
    print(
        f"  universe query billed={job.total_bytes_billed or 0:,} " f"recordings={len(mbids):,}",
        flush=True,
    )
    if len(mbids) != model.expected_recordings:
        raise SystemExit(
            f"universe holds {len(mbids)} recordings but config/rights_model.yml expects "
            f"{model.expected_recordings} for match run {model.universe_match_run_id}: the "
            f"catalogue changed, so this would be a different dataset under the same version"
        )
    return mbids, job


def write_tsv(path: pathlib.Path, columns: list[str], rows) -> tuple[int, str]:
    """Gzipped TSV. Returns row count and sha256 so reproducibility is checkable by digest."""
    n = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for row in rows:
            w.writerow([row[c] for c in columns])
            n += 1
    return n, hashlib.sha256(path.read_bytes()).hexdigest()


def generate_ownership(model: RightsModel, mbids: list[str], defects: dict[str, str]):
    for mbid in mbids:
        yield from ownership_rows(model, mbid, defects.get(mbid))
    # Orphans reference MBIDs that are deliberately absent from the catalogue.
    yield from orphan_rows(model)


def revise_holders(model: RightsModel, count: int) -> dict[str, str]:
    """Flip payee_status for the first `count` holders, deterministically.

    A controlled change, not a random one: the dbt snapshot has to be shown detecting a KNOWN
    difference, and the same revision must be reproducible.
    """
    flipped = {}
    for i in range(count):
        current = holder_row(model, i)["payee_status"]
        flipped[f"{model.holder_id_prefix}{i:0{model.holder_id_digits}d}"] = (
            "PENDING_VERIFICATION" if current == "ACTIVE" else "ACTIVE"
        )
    return flipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--revise-payee-status",
        type=int,
        default=0,
        help="re-land ONLY rights_holders with N holders' payee_status flipped",
    )
    ap.add_argument("--fail-before-publish", action="store_true")
    args = ap.parse_args()

    from google.cloud import bigquery, storage

    model = load_rights_model()
    client = bigquery.Client(project=PROJECT)
    bucket = storage.Client(project=PROJECT).bucket(args.bucket)
    work = pathlib.Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    stats: list = []
    print(
        f"rights model {model.version}  universe {model.universe_match_run_id}"
        f"{'  REVISION of ' + str(args.revise_payee_status) + ' holders' if args.revise_payee_status else ''}",
        flush=True,
    )

    mbids, universe_job = recording_universe(client, model)
    stats.append(
        {
            "step": "universe",
            "job_id": universe_job.job_id,
            "bytes_billed": universe_job.total_bytes_billed,
        }
    )

    # Deterministic from the model and the universe. No wall-clock: a re-run must produce the
    # same id, which is how a re-run is distinguishable from a new dataset.
    revision_tag = f"|rev{args.revise_payee_status}" if args.revise_payee_status else ""
    run_id = (
        "rights:"
        + hashlib.sha256(
            f"{model.version}|{model.universe_match_run_id}|{len(mbids)}{revision_tag}".encode()
        ).hexdigest()[:16]
    )
    print(f"  generation_run_id {run_id}", flush=True)

    defects = select_defects(model, mbids)
    injected = {k: sum(1 for v in defects.values() if v == k) for k in DEFECT_ORDER}
    injected["orphan_recording_mbid"] = model.defect_counts["orphan_recording_mbid"]
    injected["holder_without_split_reserved"] = model.reserved_without_split

    flipped = revise_holders(model, args.revise_payee_status)

    def holders():
        for i in range(model.holder_count):
            row = holder_row(model, i)
            if row["holder_id"] in flipped:
                row["payee_status"] = flipped[row["holder_id"]]
            yield row

    files: dict[str, dict] = {}
    targets = (
        ["rights_holders"]
        if args.revise_payee_status
        else ["rights_holders", "ownership_splits", "rate_card"]
    )

    for table in targets:
        path = work / f"{table}.tsv.gz"
        if table == "rights_holders":
            n, digest = write_tsv(path, HOLDER_COLUMNS, holders())
        elif table == "ownership_splits":
            n, digest = write_tsv(path, SPLIT_COLUMNS, generate_ownership(model, mbids, defects))
        else:
            n, digest = write_tsv(path, RATE_COLUMNS, rate_card_rows(model))
        object_name = (
            f"{GCS_PREFIX}/version={model.version}/run={run_id.replace(':', '_')}/"
            f"{table}.tsv.gz"
        )
        blob = bucket.blob(object_name)
        blob.metadata = {"sha256": digest, "rights_version": model.version}
        blob.upload_from_filename(str(path), content_type="application/gzip")
        files[table] = {
            "rows": n,
            "bytes": path.stat().st_size,
            "sha256": digest,
            "object": f"gs://{args.bucket}/{object_name}",
        }
        print(
            f"  {table:<18} rows={n:>10,}  bytes={path.stat().st_size:>12,}  "
            f"sha256={digest[:12]}",
            flush=True,
        )

    if args.fail_before_publish:
        raise SystemExit("INJECTED FAILURE after generation and upload, before publication.")

    schemas = {
        "rights_holders": [bigquery.SchemaField(c, "STRING") for c in HOLDER_COLUMNS],
        "ownership_splits": [
            bigquery.SchemaField("recording_mbid", "STRING"),
            bigquery.SchemaField("rights_holder_id", "STRING"),
            bigquery.SchemaField("share_pct", "NUMERIC"),
            bigquery.SchemaField("valid_from", "DATE"),
            bigquery.SchemaField("valid_to", "DATE"),
            bigquery.SchemaField("split_version_id", "STRING"),
        ],
        "rate_card": [
            bigquery.SchemaField("rate_card_id", "STRING"),
            bigquery.SchemaField("model_scope", "STRING"),
            bigquery.SchemaField("valid_from", "DATE"),
            bigquery.SchemaField("valid_to", "DATE"),
            bigquery.SchemaField("rate_per_stream", "NUMERIC"),
            bigquery.SchemaField("currency", "STRING"),
            bigquery.SchemaField("rule_version_id", "STRING"),
        ],
    }
    tails = {
        "rights_holders": "TRUE, '{v}', '{r}', CURRENT_TIMESTAMP()",
        "ownership_splits": "TRUE, '{v}', '{r}', CURRENT_TIMESTAMP()",
        "rate_card": "TRUE, '{v}', '{r}', CURRENT_TIMESTAMP()",
    }
    for table in targets:
        stg = f"{PROJECT}.splitsheet_rights.stg_{table}"
        # Staged load then INSERT, never a load straight into the Terraform-managed table: that
        # is what silently replaced a 13-column schema with a 10-column one in Phase 3A.
        load = client.load_table_from_uri(
            files[table]["object"],
            stg,
            job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.CSV,
                field_delimiter="\t",
                quote_character='"',
                allow_quoted_newlines=True,
                write_disposition="WRITE_TRUNCATE",
                schema=schemas[table],
            ),
        )
        load.result()
        if load.output_rows != files[table]["rows"]:
            raise SystemExit(
                f"{table}: staging holds {load.output_rows}, generated " f"{files[table]['rows']}"
            )
        cols = {
            "rights_holders": HOLDER_COLUMNS,
            "ownership_splits": SPLIT_COLUMNS,
            "rate_card": RATE_COLUMNS,
        }[table]
        tail = tails[table].format(v=model.version, r=run_id)
        job = client.query(
            f"""
            BEGIN TRANSACTION;
            DELETE FROM `{PROJECT}.splitsheet_rights.{table}` WHERE TRUE;
            INSERT INTO `{PROJECT}.splitsheet_rights.{table}`
            SELECT {', '.join(cols)}, {tail} FROM `{stg}`;
            COMMIT TRANSACTION;
        """,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES),
        )
        job.result()
        stats.append(
            {
                "step": f"publish_{table}",
                "job_id": job.job_id,
                "bytes_billed": job.total_bytes_billed,
                "load_job_id": load.job_id,
            }
        )
        client.query(f"DROP TABLE IF EXISTS `{stg}`").result()
        print(f"  published {table}", flush=True)

    check_job = client.query(
        f"""
        SELECT
          (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_rights.rights_holders`) AS holders,
          (SELECT COUNT(DISTINCT holder_id)
             FROM `{PROJECT}.splitsheet_rights.rights_holders`) AS distinct_holders,
          (SELECT COUNTIF(payee_status = 'PENDING_VERIFICATION')
             FROM `{PROJECT}.splitsheet_rights.rights_holders`) AS pending_holders,
          (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_rights.ownership_splits`) AS splits,
          (SELECT COUNT(DISTINCT recording_mbid)
             FROM `{PROJECT}.splitsheet_rights.ownership_splits`) AS split_recordings,
          (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_rights.rate_card`) AS rate_rows,
          (SELECT COUNTIF(NOT is_modeled) FROM `{PROJECT}.splitsheet_rights.ownership_splits`)
            AS splits_not_declared_modeled
    """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES),
    )
    landed = {k: int(v) for k, v in dict(next(iter(check_job.result()))).items()}
    stats.append(
        {"step": "verify", "job_id": check_job.job_id, "bytes_billed": check_job.total_bytes_billed}
    )
    if landed["splits_not_declared_modeled"]:
        raise SystemExit("a landed row does not declare itself modeled")

    report = {
        "artifact": "modeled_rights",
        "declaration": {
            "listenbrainz_listens": "REAL",
            "musicbrainz_recordings": "REAL",
            "rights_holders": "MODELED",
            "ownership_splits": "MODELED",
            "rate_cards": "MODELED",
            "royalty_amounts": "illustrative modeled amounts, not observed industry payouts",
            "real_entities_used": "none",
        },
        "rights_version": model.version,
        "rule_version_id": model.rule_version_id,
        "seed": model.seed,
        "generation_run_id": run_id,
        "universe_match_run_id": model.universe_match_run_id,
        "recordings_in_universe": len(mbids),
        "revision": (
            {"payee_status_flipped": args.revise_payee_status, "holder_ids": sorted(flipped)}
            if args.revise_payee_status
            else None
        ),
        "tables_published": targets,
        "files": files,
        "defects_injected": injected,
        "defects_are_labelled_in_the_data": False,
        "landed": landed,
        "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print("\n  landed:", json.dumps(landed))
    print("  defects injected:", json.dumps(injected))


if __name__ == "__main__":
    main()
