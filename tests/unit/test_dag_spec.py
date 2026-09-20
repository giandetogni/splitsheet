"""The DAG's shape, asserted without an Airflow installation.

The import test proper (DagBag) needs Airflow and is skipped when it is absent; these
checks do not, and they are the ones that catch the failure modes that matter here: a
task that invokes a CLI which does not exist, a run that could overwrite committed
evidence, and a publication that could happen without someone asking for it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pipeline_spec as spec
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_task_ids_are_unique_and_every_upstream_exists():
    ids = [t.task_id for t in spec.TASKS]
    assert len(ids) == len(set(ids))
    for task in spec.TASKS:
        for upstream in task.upstream:
            assert upstream in ids, f"{task.task_id} depends on unknown {upstream}"


def test_graph_is_acyclic_and_has_one_entry_point():
    seen: list[str] = []
    for task in spec.TASKS:
        for upstream in task.upstream:
            assert upstream in seen, f"{task.task_id} precedes its upstream {upstream}"
        seen.append(task.task_id)
    assert [t.task_id for t in spec.TASKS if not t.upstream] == ["verify_source_slice"]


def test_every_stage_is_declared_and_all_four_boundaries_are_present():
    stages = {t.stage for t in spec.TASKS}
    assert stages <= set(spec.STAGES)
    assert {"preflight", "compute", "validation", "publication"} <= stages


def test_every_python_cli_a_task_invokes_actually_exists():
    """No decorative wrappers: a task names a script or the test fails."""
    referenced = set()
    for task in spec.TASKS:
        referenced.update(re.findall(r"src/[\w/]+\.py", task.command))
    assert len(referenced) >= 8
    for path in sorted(referenced):
        assert (REPO_ROOT / path).is_file(), f"task references missing CLI {path}"


def test_no_task_writes_into_the_committed_evidence_directory():
    for task in spec.TASKS:
        for flag in ("--out", "--work-dir", "--raw-dir"):
            for match in re.findall(rf"{flag} (\S+)", task.command):
                if flag == "--out":
                    assert "evidence_dir" not in match, task.task_id


def test_publication_is_off_by_default_and_never_retried():
    assert spec.DEFAULT_PARAMS["allow_publication"] is False
    publish = next(t for t in spec.TASKS if t.stage == "publication")
    assert publish.retries == 0
    assert publish.command.startswith(spec.PUBLISH_GUARD)


@pytest.mark.parametrize(("allow_publication", "expected_exit"), [("False", 1), ("True", 0)])
def test_publish_guard_refuses_unless_explicitly_enabled(allow_publication, expected_exit):
    """The guard is shell, so it is proved by running the shell, not by reading it."""
    guard = spec.PUBLISH_GUARD.replace("{{ params.allow_publication }}", allow_publication)
    result = subprocess.run(["bash", "-c", guard], capture_output=True, text=True, check=False)
    assert result.returncode == expected_exit
    assert ("refusing to publish" in result.stdout) == (expected_exit == 1)


def test_importing_the_spec_reaches_neither_airflow_nor_gcp():
    """Importing the task graph must not drag in Airflow or a GCP client.

    Measured as a delta, not as an absolute: `google` and `google.cloud` are namespace
    packages that some dependency sets register at interpreter start, before any code of
    ours runs. What this test defends is that IMPORTING pipeline_spec adds none of them.
    """
    probe = (
        "import sys, json;"
        " before = {m for m in sys.modules if m.startswith(('airflow', 'google'))};"
        " sys.path.insert(0, 'dags'); import pipeline_spec;"
        " after = {m for m in sys.modules if m.startswith(('airflow', 'google'))};"
        " print(json.dumps(sorted(after - before)))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr
    introduced = json.loads(loaded.stdout)
    assert introduced == [], f"importing pipeline_spec pulled in {introduced}"


def test_dag_file_imports_under_airflow():
    """The real import test. Skipped, never silently passed, when Airflow is absent."""
    pytest.importorskip("airflow", reason="apache-airflow is not installed locally")
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(REPO_ROOT / "dags"), include_examples=False)
    assert bag.import_errors == {}
    dag = bag.get_dag(spec.DAG_ID)
    assert dag is not None
    assert {t.task_id for t in dag.tasks} == {t.task_id for t in spec.TASKS}
    for task in spec.TASKS:
        assert set(dag.get_task(task.task_id).upstream_task_ids) == set(task.upstream)


def _render(command: str, **overrides) -> str:
    """Render a task command the way Airflow will, so parameter plumbing is testable."""
    from jinja2 import Template

    params = dict(spec.DEFAULT_PARAMS) | overrides
    return Template(command).render(params=params, ts_nodash="20260705T030000")


def test_defaults_render_the_frozen_identities_and_a_run_scoped_output_directory():
    rendered = _render(next(t for t in spec.TASKS if t.task_id == "build_match_results").command)
    assert "--norm-version '1.0.0+0bc0dd643e06'" in rendered
    assert "--candidate-run-id 'blk:c005e9a56b1ec542'" in rendered
    assert "--out artifacts/airflow/20260705T030000/match_results.json" in rendered


# Every task whose output identity depends on the normalization rules. Resolving those
# rules from whatever file happens to be current is what rebuilt canonical_match_texts
# under 1.1.0 while this DAG was pinned to 1.0.0.
NORM_VERSION_TASKS = (
    "run_normalization_job",
    "run_blocking_job",
    "build_candidate_features",
    "build_match_results",
)


def test_every_task_that_depends_on_normalization_is_given_the_version():
    by_id = {t.task_id: t for t in spec.TASKS}
    for task_id in NORM_VERSION_TASKS:
        command = by_id[task_id].command
        assert (
            "--norm-version '{{ params.norm_version }}'" in command
        ), f"{task_id} must be told which normalization version to use"


def test_no_task_leaves_the_normalization_version_to_be_resolved_implicitly():
    """A task may name the version through params and nothing else: no literal, no flag
    that asks for the current or latest rules."""
    for task_id in NORM_VERSION_TASKS:
        command = {t.task_id: t for t in spec.TASKS}[task_id].command
        for hint in ("--latest", "latest", "--rules-version", "default"):
            assert hint not in command, f"{task_id} resolves normalization implicitly: {hint}"
        assert "--norm-version 1." not in command, f"{task_id} hardcodes a version"


def test_the_normalization_version_default_is_still_the_frozen_one():
    assert spec.DEFAULT_PARAMS["norm_version"] == "1.0.0+0bc0dd643e06"


def test_the_dbt_task_shells_out_to_the_venv_dbt_rather_than_uv_run():
    """N6: publish.py already runs `.venv/bin/dbt`, so the dbt task matches it and the two
    agree on one toolchain. Scoped to this task deliberately: the python tasks still use
    `uv run python`, and changing them is not what this asserts."""
    task = next(t for t in spec.TASKS if t.task_id == "run_dbt_build")
    assert "uv run dbt" not in task.command
    # The command cds into dbt/ first, so the venv is one level up from there.
    assert "../.venv/bin/dbt" in task.command


COMPOSE = REPO_ROOT / "docker/airflow-compose.yml"
CONTAINER_VENV = "/opt/splitsheet/.venv"


def _compose() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE.read_text())


def _mounts() -> dict[str, tuple[str, str]]:
    """target -> (source, mode), from the short syntax.

    Parsed rather than shelled out to `docker compose config`, so the assertions hold on a
    machine with no Docker. The source may itself contain a colon, inside `${VAR:-default}`,
    so the split anchors on the target being an absolute /opt path.
    """
    out: dict[str, tuple[str, str]] = {}
    for entry in _compose()["services"]["airflow"]["volumes"]:
        m = re.fullmatch(r"(?P<source>.*?):(?P<target>/[^:]*)(?::(?P<mode>[a-z,]+))?", entry)
        assert m, f"unparsed volume entry {entry}"
        out[m["target"]] = (m["source"], m["mode"] or "rw")
    return out


def test_the_container_venv_is_isolated_from_the_host_one():
    """The repo bind carries the host's macOS .venv in; a named volume has to mask it."""
    mounts = _mounts()
    assert mounts["/opt/splitsheet"][0] == "../", "the repo is expected to stay bind-mounted"
    source, _mode = mounts[CONTAINER_VENV]
    assert (
        "/" not in source and "$" not in source
    ), f"{CONTAINER_VENV} must be a named volume, not a host path: {source}"
    assert source in (_compose()["volumes"] or {}), f"named volume {source} is not declared"


def test_uv_is_told_to_build_its_environment_in_that_volume():
    env = _compose()["services"]["airflow"]["environment"]
    assert env["UV_PROJECT_ENVIRONMENT"] == CONTAINER_VENV


# Airflow validates the executor against the metadata backend and exits 1 at bootstrap on
# a mismatch. These are the executors it refuses on SQLite.
EXECUTORS_SQLITE_REFUSES = ("LocalExecutor", "CeleryExecutor", "KubernetesExecutor")


INIT_SERVICE = "venv-init"
AIRFLOW_UID = "50000"


def test_only_the_init_service_is_root_and_it_reaches_only_the_venv_volume():
    """Docker creates the venv volume root-owned and Airflow runs as uid 50000, so
    something has to fix the owner. That something is root exactly once, and it is mounted
    on one directory -- not the repo, not the preserved slice, not the metadata DB."""
    services = _compose()["services"]
    init = services[INIT_SERVICE]
    assert init["user"] in ("0:0", "0", "root")
    assert "user" not in services["airflow"], "the Airflow service must not be given root"
    assert init["volumes"] == [
        f"venv-linux:{CONTAINER_VENV}"
    ], "the init service mounts the venv volume and nothing else"


ADC_TARGET = "/home/airflow/.config/gcloud/application_default_credentials.json"


def test_only_the_adc_file_is_mounted_and_it_is_read_only():
    """The GCS preflight tasks need Application Default Credentials. Mounting the whole
    ~/.config/gcloud would hand the container every other credential stored beside it, and
    a writable mount would let it rewrite the host's copy."""
    source, mode = _mounts()[ADC_TARGET]
    assert source.endswith(
        "/application_default_credentials.json"
    ), f"the ADC mount must name the file, not a directory: {source}"
    assert not source.rstrip("/").endswith(".config/gcloud")
    assert mode == "ro"
    assert source.startswith("${HOME}/"), "use ${HOME}; YAML does not expand ~"


def test_the_credentials_variable_points_at_that_exact_file():
    env = _compose()["services"]["airflow"]["environment"]
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == ADC_TARGET


def test_the_project_is_named_because_the_credential_file_does_not_carry_one():
    """`google.auth.default()` resolves a project from the gcloud config directory, which
    is deliberately not mounted. Without this the client raises `Project was not passed and
    could not be determined from the environment`."""
    env = _compose()["services"]["airflow"]["environment"]
    assert env["GOOGLE_CLOUD_PROJECT"] == "ss-de-944054e7"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == ADC_TARGET
    source, mode = _mounts()[ADC_TARGET]
    assert source.endswith("/application_default_credentials.json")
    assert not source.rstrip("/").endswith(".config/gcloud")
    assert mode == "ro"


def test_no_credential_material_is_committed_to_the_repo():
    """The credential is mounted from the host at run time and never copied in."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    offenders = [
        f
        for f in tracked.stdout.split()
        if "application_default_credentials" in f or f.endswith(".p12")
    ]
    assert offenders == [], offenders
    assert "refresh_token" not in COMPOSE.read_text()


def test_airflow_waits_for_the_init_service_to_finish_successfully():
    depends = _compose()["services"]["airflow"]["depends_on"]
    assert depends[INIT_SERVICE]["condition"] == "service_completed_successfully"


def test_the_volume_is_fixed_by_ownership_not_by_widening_permissions():
    """`chmod 777` would also make the sync work, and would hand every process in the
    container write access to the environment it runs from."""
    assert "chmod" not in COMPOSE.read_text()
    command = " ".join(_compose()["services"][INIT_SERVICE]["command"])
    assert f"chown -R {AIRFLOW_UID}:0" in command


def test_the_executor_is_one_sqlite_can_actually_run():
    """`LocalExecutor` on SQLite is accepted by compose and rejected by Airflow, so the
    container exits 1 before the scheduler starts. A file read catches it; a pull does not."""
    env = _compose()["services"]["airflow"]["environment"]
    assert (
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN" not in env
    ), "this compose is the SQLite one; naming a real backend changes which executors are legal"
    executor = env["AIRFLOW__CORE__EXECUTOR"]
    assert executor not in EXECUTORS_SQLITE_REFUSES, f"{executor} cannot run against SQLite"
    assert executor == "SequentialExecutor"


def test_the_preserved_slice_stays_read_only_while_derived_can_be_written():
    """B1: the two halves of the data directory are separate mounts with separate modes."""
    mounts = _mounts()
    assert "/opt/splitsheet-data" not in mounts, "raw and derived must not share one mount"
    assert mounts["/opt/splitsheet-data/raw"][1] == "ro"
    assert mounts["/opt/splitsheet-data/derived"][1] == "rw"


def test_overrides_reach_the_command_including_the_period_derived_manifest_path():
    task = next(t for t in spec.TASKS if t.task_id == "verify_source_slice")
    rendered = _render(task.command, period="2026-07", raw_dir="/tmp/slice")
    assert "--manifest docs/phase0/period_2026_07_manifest.json" in rendered
    assert "--raw-dir /tmp/slice" in rendered
    assert "2026_06" not in rendered
