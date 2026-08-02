"""INTEGRATION tests. These are not unit tests.

They require a live GCP project, Application Default Credentials, and permission to
impersonate the matcher service account. They read real tables and cost real bytes. They
are kept in tests/integration/ and marked `integration` so a plain `pytest tests/unit`
never touches the cloud.

Run with:
    make test-integration
"""

from __future__ import annotations

import json
import pathlib

import pytest

# Every test here needs GCP. The second marker splits them by blast radius:
#   integration_readonly    -- only reads; safe to run from CI
#   integration_destructive -- invokes the loader, which can create staging tables and
#                              republish; must NEVER run in CI against real bronze/eval
pytestmark = pytest.mark.integration

# Ceiling on any single query, so a mistake cannot turn into a large bill. Sized well
# above the largest read-only query measured (~6.7 GB) and far below anything alarming.
MAX_BYTES_BILLED = 50 * 1024**3

PROJECT = "ss-de-944054e7"
BUCKET = "splitsheet-raw-944054e7"
MATCHER_SA = "splitsheet-matcher@ss-de-944054e7.iam.gserviceaccount.com"
SLICE_PREFIX = "raw/listenbrainz/fullexport-2593/period=2026-06"
MANIFEST = pathlib.Path(__file__).parents[2] / "docs/phase0/period_2026_06_manifest.json"

SOURCE_TOTAL = 39_200_000
IN_PERIOD = 38_199_641
OUTSIDE_PERIOD = 1_000_359
LABELS_PRESENT = 32_799_203

PERIOD = ("listened_at >= TIMESTAMP '2026-06-01 00:00:00+00' "
          "AND listened_at < TIMESTAMP '2026-07-01 00:00:00+00'")

ALLOWED_VIEW_COLUMNS = {
    "listen_hash", "listened_at", "artist_name", "recording_name", "release_name",
    "recording_msid", "source_file", "dump_id", "ingestion_run_id",
}
BANNED_COLUMNS = {"user_id", "recording_mbid", "release_mbid", "artist_credit_id",
                  "artist_credit_mbids", "mapper_recording_mbid"}


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


@pytest.fixture(scope="module")
def bq_as_matcher():
    """A client running as the matcher service account, via impersonation. No JSON key."""
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    src, _ = google.auth.default(scopes=scopes)
    creds = impersonated_credentials.Credentials(
        source_credentials=src, target_principal=MATCHER_SA, target_scopes=scopes)
    return bigquery.Client(project=PROJECT, credentials=creds)


@pytest.fixture(scope="module")
def manifest():
    with open(MANIFEST) as fh:
        return json.load(fh)


def _cfg():
    from google.cloud import bigquery
    return bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)


def run(client, sql: str):
    """Every query in this suite is capped by maximum_bytes_billed."""
    return client.query(sql, job_config=_cfg())


def one(client, sql: str) -> dict:
    return next(iter(dict(r) for r in run(client, sql).result()))


# --- source contract ------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_source_uris_match_manifest_exactly(bq, manifest):
    """The external table must be bound to the manifest, with no wildcard."""
    table = bq.get_table(f"{PROJECT}.splitsheet_bronze.ext_listens_2026_06")
    uris = table.external_data_configuration.source_uris
    expected = [f"gs://{BUCKET}/{SLICE_PREFIX}/{m['member']}" for m in manifest["members"]]
    assert len(uris) == 23
    assert not any("*" in u for u in uris), "wildcard source reintroduced"
    assert sorted(uris) == sorted(expected)


@pytest.mark.integration_readonly
@pytest.mark.requires_gcs
def test_gcs_objects_match_manifest(manifest):
    """Object count, sizes, checksums and total bytes still match what was measured."""
    from google.cloud import storage

    client = storage.Client(project=PROJECT)
    blobs = {b.name.rsplit("/", 1)[-1]: b
             for b in client.list_blobs(BUCKET, prefix=SLICE_PREFIX + "/")
             if not b.name.endswith("_manifest.json")}
    expected = {m["member"]: m for m in manifest["members"]}
    assert set(blobs) == set(expected), "GCS objects diverge from the manifest"
    assert len(blobs) == 23
    total = 0
    for name, m in expected.items():
        blob = blobs[name]
        blob.reload()
        total += blob.size
        assert blob.size == m["size_bytes"], f"{name} size drift"
        assert (blob.metadata or {}).get("sha256") == m["sha256"], f"{name} sha256 drift"
    assert total == manifest["total_bytes"]


# --- reconciliation -------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_reconciliation_sums_and_classifies_every_row(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS total,
               COUNTIF({PERIOD}) AS inside,
               COUNTIF(NOT ({PERIOD})) AS outside,
               COUNTIF(listened_at IS NULL) AS unclassified
        FROM `{PROJECT}.splitsheet_bronze.ext_listens_2026_06`
    """)
    assert r["total"] == SOURCE_TOTAL
    assert r["inside"] == IN_PERIOD
    assert r["outside"] == OUTSIDE_PERIOD
    assert r["inside"] + r["outside"] == r["total"]
    assert r["unclassified"] == 0


@pytest.mark.integration_readonly
def test_bronze_row_count_and_hash_uniqueness(bq):
    r = one(bq, f"""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT listen_hash) AS distinct_hashes,
               COUNTIF(listen_hash IS NULL) AS null_hashes,
               COUNT(DISTINCT ingestion_run_id) AS run_ids
        FROM `{PROJECT}.splitsheet_bronze.bronze_listens`
        WHERE {PERIOD}
    """)
    assert r["n"] == IN_PERIOD
    assert r["distinct_hashes"] == IN_PERIOD, "listen_hash is not unique"
    assert r["null_hashes"] == 0
    assert r["run_ids"] == 1


@pytest.mark.integration_readonly
def test_bronze_and_labels_are_never_on_different_runs(bq):
    """Atomic publication means every bronze row has exactly one matching label row."""
    r = one(bq, f"""
        SELECT (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_bronze.bronze_listens`
                  WHERE {PERIOD}) AS bronze_rows,
               (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`)
                 AS label_rows,
               (SELECT COUNTIF(label_available)
                  FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`) AS labels_present,
               (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_bronze.bronze_listens` b
                  JOIN `{PROJECT}.splitsheet_eval.mapper_reference_labels` l
                    USING (listen_hash)
                  WHERE b.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
                    AND b.listened_at < TIMESTAMP '2026-07-01 00:00:00+00') AS joined
    """)
    assert r["bronze_rows"] == IN_PERIOD
    assert r["label_rows"] == IN_PERIOD
    assert r["labels_present"] == LABELS_PRESENT
    assert r["joined"] == IN_PERIOD, "bronze and labels are not aligned"


# --- column firewall ------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_bronze_has_no_pii_or_derived_columns(bq):
    cols = {r["column_name"] for r in run(bq, f"""
        SELECT column_name FROM `{PROJECT}.splitsheet_bronze`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'bronze_listens'
    """).result()}
    assert not (cols & BANNED_COLUMNS), f"banned columns present: {cols & BANNED_COLUMNS}"


@pytest.mark.integration_readonly
def test_matcher_view_exposes_exactly_the_allowed_columns(bq):
    cols = {r["column_name"] for r in run(bq, f"""
        SELECT column_name FROM `{PROJECT}.splitsheet_bronze`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'v_matcher_input'
    """).result()}
    assert cols == ALLOWED_VIEW_COLUMNS, f"view schema drift: {cols ^ ALLOWED_VIEW_COLUMNS}"
    assert not (cols & BANNED_COLUMNS)


# --- cost guard -----------------------------------------------------------------------

@pytest.mark.integration_readonly
def test_query_without_partition_filter_is_rejected(bq):
    from google.api_core.exceptions import BadRequest

    with pytest.raises(BadRequest, match="(?i)partition"):
        run(bq, f"SELECT COUNT(*) FROM `{PROJECT}.splitsheet_bronze.bronze_listens`").result()


# --- evaluation firewall --------------------------------------------------------------

@pytest.mark.integration_readonly
def test_matcher_can_read_its_input_contract(bq_as_matcher):
    r = one(bq_as_matcher,
            f"SELECT COUNT(*) AS n FROM `{PROJECT}.splitsheet_bronze.v_matcher_input`")
    assert r["n"] == IN_PERIOD


@pytest.mark.integration_readonly
@pytest.mark.parametrize("table", [
    "splitsheet_eval.mapper_reference_labels",
    "splitsheet_bronze.bronze_listens",
    "splitsheet_bronze.ext_listens_2026_06",
])
@pytest.mark.integration_readonly
def test_matcher_is_denied_everything_except_the_view(bq_as_matcher, table):
    """The label must be unreachable, and so must any table carrying derived identifiers."""
    from google.api_core.exceptions import Forbidden

    with pytest.raises(Forbidden, match="(?i)access denied"):
        run(bq_as_matcher, f"SELECT COUNT(*) FROM `{PROJECT}.{table}`").result()


# --- loader behaviour (slow: these invoke the real loader) -----------------------------

def _published_state(client) -> dict:
    return one(client, f"""
        SELECT (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_bronze.bronze_listens`
                  WHERE {PERIOD}) AS bronze_rows,
               (SELECT COUNT(DISTINCT listen_hash)
                  FROM `{PROJECT}.splitsheet_bronze.bronze_listens`
                  WHERE {PERIOD}) AS bronze_hashes,
               (SELECT COUNT(*) FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`)
                 AS label_rows,
               (SELECT COUNTIF(label_available)
                  FROM `{PROJECT}.splitsheet_eval.mapper_reference_labels`) AS labels_present
    """)


def _run_loader(tmp_path, *extra: str):
    import subprocess
    import sys

    repo = pathlib.Path(__file__).parents[2]
    return subprocess.run(
        [sys.executable, str(repo / "src/ingestion/load_period.py"),
         "--project", PROJECT, "--manifest", str(MANIFEST), "--bucket", BUCKET,
         "--out", str(tmp_path / "report.json"), *extra],
        capture_output=True, text=True, cwd=repo, check=False)


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_rerun_preserves_row_counts_and_labels(bq, tmp_path):
    before = _published_state(bq)
    proc = _run_loader(tmp_path)
    assert proc.returncode == 0, proc.stderr[-800:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["outcome"]["inserted"] == 0
    assert report["outcome"]["skipped"] == IN_PERIOD
    assert report["outcome"]["published"] is False
    assert _published_state(bq) == before


@pytest.mark.integration_destructive
@pytest.mark.slow
def test_failure_before_publication_leaves_previous_state_intact(bq, tmp_path):
    """The whole point of staging: a crash mid-run must not degrade what is published."""
    before = _published_state(bq)
    proc = _run_loader(tmp_path, "--force", "--fail-before-publish")
    assert proc.returncode != 0, "injected failure did not abort the run"
    assert "INJECTED FAILURE" in (proc.stdout + proc.stderr)

    after = _published_state(bq)
    assert after == before, "published state changed despite the run failing"
    assert after["bronze_rows"] == IN_PERIOD
    assert after["label_rows"] == IN_PERIOD
    assert after["labels_present"] == LABELS_PRESENT

    # staging is deliberately left behind for audit; clean it up so the suite is repeatable
    cleanup = _run_loader(tmp_path, "--cleanup-orphan-staging")
    assert cleanup.returncode == 0, cleanup.stderr[-800:]
