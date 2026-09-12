"""splitsheet_monthly_pipeline -- coordination only.

The graph itself lives in pipeline_spec.py, which imports nothing from Airflow. This
file is the adapter: it turns each declared task into a BashOperator and wires the
edges. Keeping the split means the task ids, dependencies and commands are testable
without an Airflow installation, and it makes it obvious when analytical logic is being
smuggled into the orchestrator -- there is nowhere here to put it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.models.dag import DAG
from airflow.models.param import Param
from airflow.operators.bash import BashOperator
from pipeline_spec import DAG_ID, DEFAULT_PARAMS, TASKS

REPO_ROOT = Path(__file__).resolve().parents[1]

with DAG(
    dag_id=DAG_ID,
    description="Monthly royalty attribution pipeline. Coordinates existing CLIs.",
    # The period's dumps are published early in the following month; the 5th is late
    # enough to be safe and early enough to leave room to re-run before month end.
    schedule="0 3 5 * *",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    # No backfill. Every historical period was published under a frozen run identity;
    # catchup would mint new runs over statements that are already in force.
    catchup=False,
    # Two concurrent runs would contend for the same BigQuery staging tables.
    max_active_runs=1,
    default_args={"retry_delay": timedelta(minutes=5), "depends_on_past": False},
    params={k: Param(v) for k, v in DEFAULT_PARAMS.items()},
    tags=["splitsheet", "monthly"],
    doc_md=__doc__,
) as dag:
    operators = {
        task.task_id: BashOperator(
            task_id=task.task_id,
            bash_command=task.command,
            cwd=str(REPO_ROOT),
            retries=task.retries,
            doc_md=f"stage: `{task.stage}`",
        )
        for task in TASKS
    }

    for task in TASKS:
        for upstream in task.upstream:
            operators[upstream] >> operators[task.task_id]
