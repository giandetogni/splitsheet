"""The monthly task graph, declared without importing Airflow.

Keeping the graph in a plain module is what lets its shape -- task ids, edges, retry
policy and the exact shell command each task runs -- be asserted by the unit suite on a
machine with no Airflow installed, and it keeps the DAG file to the adapter it should
be. Nothing here imports Airflow, touches GCP or reads the environment: importing this
module must stay free and side-effect-free, because the import test is the cheapest
check in the project and it should never need credentials to run.

Every command below is a CLI that already exists and is already the documented way to
run that stage by hand. The DAG owns ordering, parameters, retries and run identity. It
owns no matching, payout or restatement logic.
"""

from __future__ import annotations

from dataclasses import dataclass

DAG_ID = "splitsheet_monthly_pipeline"

# Reads and writes are separated by parameter, not by convention. `evidence_dir` holds
# committed Phase 0-6 artifacts and is only ever read; every task writes under
# `out_dir/<ts_nodash>/`, so a DAG run cannot overwrite published evidence even if it
# is re-run with the same period. test_dag_spec.py enforces both halves of that.
DEFAULT_PARAMS: dict[str, object] = {
    "period": "2026-06",
    "project": "ss-de-944054e7",
    "gcs_bucket": "splitsheet-raw-944054e7",
    # Paths inside the worker. The compose file mounts the host's preserved slice at
    # /opt/splitsheet-data read-only, so a container cannot write to the raw copy.
    "raw_dir": "/opt/splitsheet-data/raw/fullexport-2593-2026-06",
    "derived_dir": "/opt/splitsheet-data/derived",
    "evidence_dir": "docs/phase0",
    "out_dir": "artifacts/airflow",
    # The frozen identities Phase 4B-6 published under. A run that wants different
    # inputs overrides them in the UI; they are never read from the environment, so an
    # import cannot pick up an accidental value.
    "norm_version": "1.0.0+0bc0dd643e06",
    "blocking_version": "staged-1.0.0",
    "candidate_run_id": "blk:c005e9a56b1ec542",
    # Publication is the only irreversible step in the graph, and it is off by default.
    "allow_publication": False,
}

RUN_OUT = "{{ params.out_dir }}/{{ ts_nodash }}"
MANIFEST = "{{ params.evidence_dir }}/period_{{ params.period | replace('-', '_') }}_manifest.json"

# A failed guard is the safe outcome: the task goes red and nothing is published. This
# is deliberately not a skip -- a run that silently declined to publish would look the
# same as a run that published, which is exactly the ambiguity publication cannot have.
PUBLISH_GUARD = (
    'if [ "{{ params.allow_publication }}" != "True" ]; then'
    ' echo "refusing to publish: params.allow_publication is false"; exit 1; fi\n'
)


@dataclass(frozen=True)
class Task:
    """One BashOperator's worth of intent, independent of Airflow."""

    task_id: str
    stage: str
    command: str
    retries: int
    upstream: tuple[str, ...] = ()


# retries=2 is for stages that are safe to retry: they are read-only, or they refuse to
# re-insert a run they already hold. retries=1 marks a stage where a retry is safe but
# expensive in bytes. retries=0 marks a stage no scheduler may repeat on its own.
TASKS: tuple[Task, ...] = (
    Task(
        "verify_source_slice", "preflight",
        "mkdir -p " + RUN_OUT + " && uv run python src/recon/verify_slice.py"
        " --manifest " + MANIFEST + " --raw-dir {{ params.raw_dir }}"
        " --out " + RUN_OUT + "/slice_verification.json",
        retries=2,
    ),
    Task(
        "verify_gcs_slice", "preflight",
        "uv run python src/preservation/verify_gcs_slice.py"
        " --manifest " + MANIFEST + " --raw-dir {{ params.raw_dir }}"
        " --bucket {{ params.gcs_bucket }}"
        " --out " + RUN_OUT + "/gcs_verification.json",
        retries=2, upstream=("verify_source_slice",),
    ),
    Task(
        "load_bronze_tables", "compute",
        "uv run python src/ingestion/load_period.py --project {{ params.project }}"
        " --manifest " + MANIFEST + " --bucket {{ params.gcs_bucket }}"
        " --out " + RUN_OUT + "/bronze_load.json",
        retries=2, upstream=("verify_gcs_slice",),
    ),
    Task(
        "run_normalization_job", "compute",
        "uv run python src/normalization/build_canonical_texts.py"
        " --candidate-run-id '{{ params.candidate_run_id }}' --bucket {{ params.gcs_bucket }}"
        " --norm-version '{{ params.norm_version }}'"
        " --work-dir {{ params.derived_dir }}"
        " --out " + RUN_OUT + "/canonical_texts.json",
        retries=2, upstream=("load_bronze_tables",),
    ),
    Task(
        "run_blocking_job", "compute",
        "uv run python src/matching/build_blocking.py --project {{ params.project }}"
        " --norm-version '{{ params.norm_version }}'"
        " --blocking-version '{{ params.blocking_version }}'"
        " --out " + RUN_OUT + "/blocking.json",
        retries=2, upstream=("run_normalization_job",),
    ),
    Task(
        "build_candidate_features", "compute",
        "uv run python src/matching/build_candidate_features.py"
        " --norm-version '{{ params.norm_version }}'"
        " --candidate-run-id '{{ params.candidate_run_id }}'"
        " --out " + RUN_OUT + "/candidate_features.json",
        retries=2, upstream=("run_blocking_job",),
    ),
    Task(
        "build_match_results", "compute",
        "uv run python src/matching/build_match_results.py"
        " --norm-version '{{ params.norm_version }}'"
        " --blocking-version '{{ params.blocking_version }}'"
        " --candidate-run-id '{{ params.candidate_run_id }}'"
        " --out " + RUN_OUT + "/match_results.json",
        retries=1, upstream=("build_candidate_features",),
    ),
    Task(
        "report_top_unmatched", "validation",
        "uv run python src/evaluation/top_unmatched.py"
        " --out " + RUN_OUT + "/top_unmatched.json",
        retries=2, upstream=("build_match_results",),
    ),
    Task(
        "run_dbt_build", "dbt",
        "cd dbt && DBT_PROFILES_DIR=$(pwd) ../.venv/bin/dbt build",
        retries=1, upstream=("report_top_unmatched",),
    ),
    Task(
        "verify_rights_layer", "validation",
        "uv run python src/rights/verify_rights.py"
        " --out " + RUN_OUT + "/rights_verification.json",
        retries=2, upstream=("run_dbt_build",),
    ),
    Task(
        "publish_period_results", "publication",
        PUBLISH_GUARD + "uv run python src/payout/publish.py"
        " --out " + RUN_OUT + "/payout_publication.json",
        retries=0, upstream=("verify_rights_layer",),
    ),
)

STAGES: tuple[str, ...] = ("preflight", "compute", "validation", "dbt", "publication")
