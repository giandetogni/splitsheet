"""The restatement run registry: legacy identifiers resolved to canonical ones, additively.

WHY A REGISTRY INSTEAD OF A CORRECTION. The 5,925,913 published rows of pub:v2 carry
`restate:6b3923771883e860`, an identifier produced by a scheme that could not tell two cohorts
apart. The tempting fix -- UPDATE the published rows to carry the canonical id -- would destroy the
one property Phase 6 exists to demonstrate: that a published figure is immutable and is corrected by
a new statement rather than by an edit. A restatement of a restatement's identity is still a
restatement.

So the correction is a NEW table that maps one to the other, and it carries both the identity and
the reason the old one was insufficient. The published rows are not touched.

WHAT THE REGISTRY PROVES BY BEING QUERYABLE:

    * the run that produced pub:v2 has a canonical identity that includes its cohort;
    * the cohort that was REJECTED before publication has a DIFFERENT canonical identity;
    * both of those map back to the SAME legacy string, which is the collision, recorded rather
      than quietly erased.

The rows are generated from src/restatement/identity.py and config/restatement_cohorts.yml, and the
dbt model is generated from these rows, so the warehouse cannot drift from the identity module. A
unit test fails if the checked-in SQL stops matching the generator.
"""

from __future__ import annotations

import argparse
import json
import pathlib

from restatement.identity import (
    IDENTITY_SCHEME,
    LEGACY_INPUTS,
    LEGACY_RUN_ID,
    CohortDefinition,
    LegacyRun,
    RestatementInputs,
    inputs_for_published_restatement,
    legacy_run_id,
    load_cohorts,
    load_legacy_runs,
)

MODEL_PATH = pathlib.Path(__file__).parents[2] / "dbt/models/finance/restatement_run_registry.sql"

#: Which publication each run produced. A rejected cohort produced none, and saying so is the
#: point: the registry has to be able to describe a run that was built and thrown away.
PUBLICATION_OF_RUN = {
    "partial-empty-hangul-kana": "pub:v2",
    "script-only-no-failure-reason-restriction": "",
}

RUN_TYPE_PUBLISHED = "PUBLISHED_RESTATEMENT"
RUN_TYPE_REJECTED = "REJECTED_BEFORE_PUBLICATION"

#: The registry's schema, declared rather than inferred, because half of these columns are NULL on
#: some rows and a UNION ALL of untyped NULLs is how a table quietly acquires the wrong type.
#:
#: `mart_run_id` is the resolution key: the identifier as it appears in fct_restatements, and NULL
#: for a run that never wrote a row. That is what lets the invariant be stated without exceptions --
#: every id in the mart joins to exactly one registry entry.
SCHEMA: tuple[tuple[str, str], ...] = (
    ("registry_key", "string"),
    ("run_type", "string"),
    ("is_legacy", "bool"),
    ("is_financially_effective", "bool"),
    ("mart_run_id", "string"),
    ("canonical_run_id", "string"),
    ("identity_scheme", "string"),
    ("inputs_digest", "string"),
    ("canonical_inputs_available", "bool"),
    ("canonical_inputs", "string"),
    ("legacy_run_id", "string"),
    ("legacy_scheme", "string"),
    ("legacy_id_is_ambiguous", "bool"),
    ("cohort_key", "string"),
    ("cohort_sha256", "string"),
    ("cohort_predicate", "string"),
    ("cohort_status", "string"),
    ("new_publication_id", "string"),
    ("measured_listens", "int64"),
    ("measured_distinct_pairs", "int64"),
    ("recorded_delta", "numeric"),
    ("rows_in_delta_mart", "int64"),
    ("provenance", "string"),
    ("why_legacy_insufficient", "string"),
    ("legacy_missing_inputs", "string"),
)

COHORT_PROVENANCE = {
    RUN_TYPE_PUBLISHED: (
        "the restatement that produced pub:v2. Its rows carry the legacy identifier because that is "
        "what they were published with; the canonical identity is recorded here rather than written "
        "back over them."
    ),
    RUN_TYPE_REJECTED: (
        "built first and rejected before publication: dropping the failure-reason half of the frozen "
        "predicate pulled in listens that already had a lookup key. It wrote no rows to the delta "
        "mart and produced no publication. It is kept because a rejected cohort is the only proof "
        "that the run identity actually distinguishes cohorts -- under the superseded scheme this "
        "run and the published one shared a single identifier."
    ),
}


def _cohort_row(key: str, cohort: CohortDefinition, inputs: RestatementInputs, legacy: str) -> dict:
    run_type = RUN_TYPE_PUBLISHED if cohort.mart_run_id else RUN_TYPE_REJECTED
    return {
        "registry_key": key,
        "run_type": run_type,
        "is_legacy": False,
        "is_financially_effective": bool(cohort.mart_run_id),
        "mart_run_id": cohort.mart_run_id or None,
        "canonical_run_id": inputs.run_id,
        "identity_scheme": IDENTITY_SCHEME,
        "inputs_digest": inputs.inputs_digest,
        "canonical_inputs_available": True,
        "canonical_inputs": inputs.canonical_payload(),
        # Both cohort rows carry the SAME legacy string. That is not a bug in the registry, it is
        # the finding the registry exists to record.
        "legacy_run_id": legacy,
        "legacy_scheme": LEGACY_RUN_ID.scheme,
        "legacy_id_is_ambiguous": True,
        "cohort_key": cohort.cohort_key,
        "cohort_sha256": cohort.digest,
        "cohort_predicate": cohort.canonical_predicate(),
        "cohort_status": cohort.status,
        "new_publication_id": PUBLICATION_OF_RUN.get(key) or None,
        "measured_listens": cohort.measured_listens,
        "measured_distinct_pairs": cohort.measured_distinct_pairs,
        "recorded_delta": cohort.recorded_delta or None,
        "rows_in_delta_mart": cohort.rows_in_delta_mart or None,
        "provenance": COHORT_PROVENANCE[run_type] + " " + " ".join(cohort.notes.split()),
        "why_legacy_insufficient": LEGACY_RUN_ID.why_insufficient,
        "legacy_missing_inputs": list(LEGACY_RUN_ID.missing_inputs),
    }


def _legacy_row(run: LegacyRun) -> dict:
    """A run with no cohort and no canonical identity. Every unrecorded field stays NULL."""
    return {
        "registry_key": run.restatement_run_id,
        "run_type": run.run_type,
        "is_legacy": run.is_legacy,
        "is_financially_effective": run.is_financially_effective,
        "mart_run_id": run.restatement_run_id,
        "canonical_run_id": None,
        "identity_scheme": None,
        "inputs_digest": None,
        "canonical_inputs_available": run.canonical_inputs_available,
        "canonical_inputs": None,
        "legacy_run_id": run.restatement_run_id,
        "legacy_scheme": "placeholder-literal",
        # Not ambiguous -- worse. An ambiguous id names two runs; this one names none.
        "legacy_id_is_ambiguous": False,
        "cohort_key": None,
        "cohort_sha256": None,
        "cohort_predicate": None,
        "cohort_status": None,
        "new_publication_id": None,
        "measured_listens": None,
        "measured_distinct_pairs": None,
        "recorded_delta": run.recorded_delta,
        "rows_in_delta_mart": run.rows_in_delta_mart,
        "provenance": run.provenance,
        "why_legacy_insufficient": (
            "it is not an identity at all: a placeholder literal that names no cohort, no period "
            "and no publication, so nothing about the run can be recovered from it"
        ),
        "legacy_missing_inputs": list(LEGACY_RUN_ID.missing_inputs),
    }


def registry_rows(
    cohorts: dict[str, CohortDefinition] | None = None, legacy_runs: list[LegacyRun] | None = None
) -> list[dict]:
    """One row per identifier the warehouse can contain. Pure: no warehouse, no clock."""
    registry = cohorts if cohorts is not None else load_cohorts()
    runs = legacy_runs if legacy_runs is not None else load_legacy_runs()
    legacy = legacy_run_id(**LEGACY_INPUTS)
    rows = [
        _cohort_row(key, cohort, inputs_for_published_restatement(registry, key), legacy)
        for key, cohort in registry.items()
    ]
    rows += [_legacy_row(run) for run in runs]
    return sorted(rows, key=lambda r: r["registry_key"])


def _sql_literal(value, sql_type: str) -> str:
    if value is None:
        return f"cast(null as {sql_type})"
    if sql_type == "bool":
        return "true" if value else "false"
    if sql_type == "int64":
        return str(int(value))
    if sql_type == "numeric":
        return f"numeric '{value}'"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def registry_model_sql(
    cohorts: dict[str, CohortDefinition] | None = None, legacy_runs: list[LegacyRun] | None = None
) -> str:
    """Render the dbt model. GENERATED -- edit the config or identity.py, then regenerate."""
    rows = registry_rows(cohorts, legacy_runs)
    header = "\n".join(
        [
            "{{ config(materialized = 'table') }}",
            "",
            "-- GENERATED by src/restatement/registry.py. Do not edit by hand:",
            "-- tests/unit/test_restatement_identity.py fails if this file stops matching the",
            "-- generator, so a cohort or identity change cannot leave the warehouse behind.",
            "--",
            "-- THE RESTATEMENT RUN REGISTRY. Every identifier the warehouse contains, explained once.",
            "--",
            f"-- The published rows of pub:v2 carry `{LEGACY_RUN_ID.run_id}`, produced by a scheme that",
            "-- hashed the versions alone: the cohort was not part of the hash, so the rejected",
            "-- 1,377,862-listen cohort and the published 982,322-listen cohort produced the SAME",
            "-- identifier under the same versions.",
            "--",
            "-- They are NOT rewritten. A published figure is corrected by a new statement, never by an",
            "-- edit, and that applies to its identity as much as to its amount. The two cohort rows",
            "-- carry the same legacy_run_id and different canonical_run_ids: the collision as data.",
            "--",
            f"-- canonical_run_id = restate:{IDENTITY_SCHEME}:<first 16 hex of sha256(canonical inputs)>,",
            "-- whose inputs include the cohort digest -- the term the legacy scheme was missing.",
            "--",
            "-- mart_run_id is the RESOLUTION KEY: the identifier as it appears in fct_restatements, and",
            "-- NULL for a run that never wrote a row. It is what lets the invariant be stated with no",
            "-- exceptions -- every id in the delta mart joins to exactly one row here, including the",
            "-- LEGACY_REHEARSAL placeholder, whose entry says plainly that it is not an identity.",
            "",
            "",
        ]
    )
    columns = [name for name, _ in SCHEMA]
    types = dict(SCHEMA)
    selects = []
    for row in rows:
        cols = ",\n".join(f"        {_sql_literal(row[c], types[c])} as {c}" for c in columns)
        selects.append(f"    select\n{cols}")
    body = "with runs as (\n" + "\n\n    union all\n\n".join(selects) + "\n)\n\n"
    body += "select\n    " + ",\n    ".join(columns) + ",\n    current_timestamp() as measured_at\n"
    body += "from runs\norder by registry_key\n"
    return header + body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="regenerate the dbt model")
    args = ap.parse_args()
    sql = registry_model_sql()
    if args.write:
        MODEL_PATH.write_text(sql)
        print(f"wrote {MODEL_PATH.relative_to(pathlib.Path(__file__).parents[2])}")
    for row in registry_rows():
        print(
            f"  {row['registry_key']:<42} {row['run_type']:<28} "
            f"canonical={row['canonical_run_id'] or '-':<26} "
            f"mart={row['mart_run_id'] or '-':<26} "
            f"delta={row['recorded_delta'] or '-':>5} "
            f"rows={row['rows_in_delta_mart'] or 0:>9,}"
        )


if __name__ == "__main__":
    main()
