"""Unit tests for canonical restatement identity. Pure: no GCP.

The defect being fixed: Phase 6 hashed `restatement_run_id` from the versions alone, so the cohort
was not part of the identity and two runs over different cohorts collided. Every test below exists
to make that class of collision impossible to reintroduce.
"""

from __future__ import annotations

import copy
import dataclasses
import pathlib

import pytest
import yaml

from restatement.identity import (
    CANONICAL_INPUT_FIELDS,
    COHORT_FIELDS_EXCLUDED_FROM_HASH,
    IDENTITY_SCHEME,
    LEGACY_RUN_ID,
    PUBLISHED_COHORT_KEY,
    REJECTED_COHORT_KEY,
    CohortDefinition,
    RestatementInputs,
    canonical_id_for_published_run,
    inputs_for_published_restatement,
    load_cohorts,
)

CONFIG = pathlib.Path(__file__).parents[2] / "config/restatement_cohorts.yml"
COHORTS = load_cohorts()

#: Pinned. These are the identities of the two Phase 6 cohorts under the canonical scheme.
EXPECTED_PUBLISHED_RUN_ID = "restate:v1:2f08f786d0d3e552"
EXPECTED_REJECTED_RUN_ID = "restate:v1:67646757af1bf447"


def published() -> RestatementInputs:
    return inputs_for_published_restatement(COHORTS, PUBLISHED_COHORT_KEY)


def rejected() -> RestatementInputs:
    return inputs_for_published_restatement(COHORTS, REJECTED_COHORT_KEY)


# --- 1. same inputs => same id ------------------------------------------------------------

def test_the_same_inputs_always_produce_the_same_id():
    assert published().run_id == published().run_id == EXPECTED_PUBLISHED_RUN_ID
    # And across freshly loaded config, not just a cached object.
    assert canonical_id_for_published_run() == EXPECTED_PUBLISHED_RUN_ID


def test_the_id_carries_its_identity_scheme():
    """An identifier must say which scheme produced it, or a future scheme is indistinguishable."""
    assert published().run_id.startswith(f"restate:{IDENTITY_SCHEME}:")
    assert published().canonical_payload()["identity_scheme"] == IDENTITY_SCHEME


# --- 2. changing ONLY the cohort => different id ------------------------------------------

def test_changing_only_the_cohort_changes_the_id():
    """The exact defect being fixed: these two runs share every version and differ only in which
    records they touched, and under the legacy scheme they shared one identifier."""
    a, b = published(), rejected()
    differing = [f for f in CANONICAL_INPUT_FIELDS
                 if getattr(a, f) != getattr(b, f)]
    assert differing == ["cohort_digest"], differing
    assert a.run_id != b.run_id
    assert (a.run_id, b.run_id) == (EXPECTED_PUBLISHED_RUN_ID, EXPECTED_REJECTED_RUN_ID)


def test_the_two_phase_6_cohorts_are_the_ones_that_used_to_collide():
    assert COHORTS[PUBLISHED_COHORT_KEY].measured_listens == 982_322
    assert COHORTS[REJECTED_COHORT_KEY].measured_listens == 1_377_862
    assert COHORTS[PUBLISHED_COHORT_KEY].digest != COHORTS[REJECTED_COHORT_KEY].digest


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda p: p.update({"prior_failure_reasons": []}), id="drop_failure_reasons"),
    pytest.param(lambda p: p.update({"required_scripts_any": ["hangul"]}), id="drop_kana"),
    pytest.param(lambda p: p.update({"string_fields_examined": ["recording_name"]}),
                 id="drop_artist_field"),
    pytest.param(lambda p: p.update({"period_end": "2026-08-01"}), id="widen_period"),
])
def test_any_predicate_change_changes_the_cohort_digest(mutate):
    base = COHORTS[PUBLISHED_COHORT_KEY]
    predicate = base.canonical_predicate()
    mutate(predicate)
    changed = CohortDefinition(
        cohort_key="mutated",
        period_start=predicate["period_start"],
        period_end=predicate["period_end"],
        prior_failure_reasons=tuple(predicate["prior_failure_reasons"]),
        string_fields_examined=tuple(predicate["string_fields_examined"]),
        required_scripts_any=tuple(predicate["required_scripts_any"]),
    )
    assert changed.digest != base.digest


# --- 3. changing ONLY a version => different id -------------------------------------------

@pytest.mark.parametrize("field_name", [
    "prior_normalization_version", "new_normalization_version", "scoring_version",
    "payout_policy_version", "rights_version", "rule_version_id", "trigger_reason",
    "canonical_snapshot_date", "period_start", "period_end",
])
def test_changing_only_one_canonical_input_changes_the_id(field_name):
    base = published()
    changed = dataclasses.replace(base, **{field_name: getattr(base, field_name) + "-x"})
    assert changed.run_id != base.run_id, field_name


def test_changing_only_the_normalization_version_changes_the_id():
    base = published()
    changed = dataclasses.replace(base, new_normalization_version="1.2.0+deadbeefcafe")
    assert changed.run_id != base.run_id


def test_changing_only_the_prior_publication_id_changes_the_id():
    base = published()
    changed = dataclasses.replace(base, prior_publication_id="pub:v2")
    assert changed.run_id != base.run_id


# --- 4. field order is irrelevant ---------------------------------------------------------

def test_the_order_of_set_valued_predicate_fields_does_not_change_the_digest():
    """These lists are sets semantically -- failure reasons, scripts, fields examined -- so their
    order must not carry identity. Otherwise reordering a YAML list would invent a new run."""
    base = COHORTS[PUBLISHED_COHORT_KEY]
    reordered = CohortDefinition(
        cohort_key=base.cohort_key,
        period_start=base.period_start, period_end=base.period_end,
        prior_failure_reasons=tuple(reversed(base.prior_failure_reasons)),
        string_fields_examined=tuple(reversed(base.string_fields_examined)),
        required_scripts_any=tuple(reversed(base.required_scripts_any)),
    )
    assert reordered.digest == base.digest


def test_yaml_key_order_does_not_change_the_digest(tmp_path):
    raw = yaml.safe_load(CONFIG.read_text())
    shuffled = copy.deepcopy(raw)
    for cohort in shuffled["cohorts"]:
        cohort["predicate"] = dict(reversed(list(cohort["predicate"].items())))
    p = tmp_path / "reordered.yml"
    p.write_text(yaml.safe_dump(shuffled, sort_keys=False))
    assert {k: v.digest for k, v in load_cohorts(p).items()} == \
           {k: v.digest for k, v in COHORTS.items()}


def test_the_id_does_not_depend_on_dataclass_field_order():
    """Identity is computed from a declared field list and canonical JSON, so constructing the
    inputs with keyword arguments in any order gives the same hash."""
    base = published()
    payload = {f: getattr(base, f) for f in CANONICAL_INPUT_FIELDS}
    rebuilt = RestatementInputs(**dict(reversed(list(payload.items()))))
    assert rebuilt.run_id == base.run_id


# --- 5. comments and documentation are irrelevant -----------------------------------------

def test_notes_status_and_measured_counts_do_not_change_the_digest():
    base = COHORTS[PUBLISHED_COHORT_KEY]
    annotated = dataclasses.replace(
        base, status="SOMETHING_ELSE", measured_listens=42,
        notes="a completely different explanation of the same predicate")
    assert annotated.digest == base.digest
    assert set(COHORT_FIELDS_EXCLUDED_FROM_HASH) >= {"status", "measured_listens", "notes"}


def test_yaml_comments_and_prose_do_not_change_the_digest(tmp_path):
    raw_text = CONFIG.read_text()
    commented = "# an added comment that must not matter\n" + raw_text.replace(
        "measured_listens: 982322", "measured_listens: 982322  # inline comment")
    p = tmp_path / "commented.yml"
    p.write_text(commented)
    assert {k: v.digest for k, v in load_cohorts(p).items()} == \
           {k: v.digest for k, v in COHORTS.items()}


def test_editing_a_note_does_not_change_the_run_id(tmp_path):
    raw = yaml.safe_load(CONFIG.read_text())
    raw["cohorts"][0]["notes"] = "reworded entirely"
    p = tmp_path / "renoted.yml"
    p.write_text(yaml.safe_dump(raw))
    assert inputs_for_published_restatement(load_cohorts(p)).run_id == EXPECTED_PUBLISHED_RUN_ID


# --- the config cannot drift from its digest ----------------------------------------------

def test_a_predicate_edited_without_updating_its_digest_is_refused(tmp_path):
    raw = yaml.safe_load(CONFIG.read_text())
    raw["cohorts"][0]["predicate"]["required_scripts_any"] = ["hangul"]
    p = tmp_path / "stale.yml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="stale cohort id"):
        load_cohorts(p)


def test_a_predicate_using_an_unknown_field_is_refused(tmp_path):
    raw = yaml.safe_load(CONFIG.read_text())
    raw["cohorts"][0]["predicate"]["sql"] = "where 1=1"
    p = tmp_path / "sqlish.yml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="outside the predicate vocabulary"):
        load_cohorts(p)


def test_incomplete_canonical_inputs_are_refused():
    base = published()
    with pytest.raises(ValueError, match="incomplete"):
        dataclasses.replace(base, cohort_digest="").canonical_payload()


def test_every_documented_canonical_input_is_actually_hashed():
    payload = published().canonical_payload()
    for name in ("period_start", "period_end", "prior_publication_id",
                 "prior_normalization_version", "new_normalization_version", "scoring_version",
                 "payout_policy_version", "rights_version", "rule_version_id", "trigger_reason",
                 "canonical_snapshot_date", "cohort_digest"):
        assert name in payload, name


# --- the legacy identifier is preserved, not rewritten ------------------------------------

def test_the_legacy_identifier_is_recorded_as_insufficient():
    assert LEGACY_RUN_ID.run_id == "restate:6b3923771883e860"
    assert LEGACY_RUN_ID.published_rows_carry_it is True
    assert "cohort" in LEGACY_RUN_ID.why_insufficient
    assert "cohort_definition" in LEGACY_RUN_ID.missing_inputs
    # The legacy scheme hashed only versions, which is exactly why it could not separate cohorts.
    assert "cohort_definition" not in LEGACY_RUN_ID.hashed_inputs


def test_the_legacy_and_canonical_identifiers_are_different_strings():
    """The legacy id stays on the published rows; the canonical id identifies the same run in the
    registry. They must never be confused for one another."""
    assert LEGACY_RUN_ID.run_id != canonical_id_for_published_run()
    assert not LEGACY_RUN_ID.run_id.startswith(f"restate:{IDENTITY_SCHEME}:")


def test_sql_is_generated_from_the_predicate_and_is_not_its_identity():
    """Reformatting generated SQL cannot change the run id, because the SQL is downstream of the
    predicate rather than the source of it."""
    cohort = COHORTS[PUBLISHED_COHORT_KEY]
    sql = cohort.sql_predicate()
    assert "failure_reason IN ('NO_LOOKUP_KEY_EMPTY', 'NO_LOOKUP_KEY_PARTIAL')" in sql
    assert "artist_name" in sql and "recording_name" in sql
    spaced = cohort.sql_predicate(normalized_alias="listens", matches_alias="matches")
    assert spaced != sql
    assert cohort.digest == COHORTS[PUBLISHED_COHORT_KEY].digest


# --- the legacy scheme's defect, reproduced rather than asserted ---------------------------

def test_the_legacy_formula_reproduces_the_published_identifier():
    from restatement.identity import LEGACY_INPUTS, legacy_run_id
    assert legacy_run_id(**LEGACY_INPUTS) == LEGACY_RUN_ID.run_id


def test_the_legacy_formula_cannot_see_the_cohort_at_all():
    """The collision, demonstrated: the legacy signature has no cohort parameter, so feeding it the
    published cohort's run and the rejected cohort's run is literally the same call."""
    import inspect

    from restatement.identity import LEGACY_INPUTS, legacy_run_id
    params = set(inspect.signature(legacy_run_id).parameters)
    assert "cohort" not in " ".join(params)
    assert legacy_run_id(**LEGACY_INPUTS) == legacy_run_id(**LEGACY_INPUTS)
    # ...while the canonical scheme separates exactly those two runs.
    assert published().run_id != rejected().run_id


def test_the_legacy_id_resolves_to_the_canonical_id_of_the_published_run():
    assert LEGACY_RUN_ID.canonical_equivalent == EXPECTED_PUBLISHED_RUN_ID


# --- the registry: additive, and consistent with the identity module -----------------------

def test_the_registry_maps_one_legacy_id_to_two_canonical_ids():
    from restatement.registry import registry_rows
    rows = registry_rows(COHORTS)
    assert len({r["legacy_run_id"] for r in rows}) == 1
    assert len({r["canonical_run_id"] for r in rows}) == len(rows) == 2
    assert len({r["cohort_sha256"] for r in rows}) == 2
    assert all(r["legacy_id_is_ambiguous"] for r in rows)


def test_the_registry_records_which_run_was_published_and_which_was_rejected():
    from restatement.registry import registry_rows
    by_key = {r["cohort_key"]: r for r in registry_rows(COHORTS)}
    assert by_key[PUBLISHED_COHORT_KEY]["new_publication_id"] == "pub:v2"
    assert by_key[PUBLISHED_COHORT_KEY]["published_rows_carry_legacy_id"] is True
    rejected_row = by_key[REJECTED_COHORT_KEY]
    assert rejected_row["cohort_status"] == "REJECTED_BEFORE_PUBLICATION"
    assert rejected_row["new_publication_id"] == ""
    assert rejected_row["published_rows_carry_legacy_id"] is False


def test_the_generated_dbt_model_is_current():
    """The warehouse cannot drift from the identity module: change a cohort or an input and this
    fails until the model is regenerated."""
    from restatement.registry import MODEL_PATH, registry_model_sql
    assert MODEL_PATH.read_text() == registry_model_sql(), (
        f"{MODEL_PATH} is stale; run `python -m restatement.registry --write`")


def test_the_generated_model_contains_both_identities_and_neither_is_the_legacy_one():
    from restatement.registry import MODEL_PATH
    sql = MODEL_PATH.read_text()
    assert EXPECTED_PUBLISHED_RUN_ID in sql and EXPECTED_REJECTED_RUN_ID in sql
    assert sql.count(f"'{LEGACY_RUN_ID.run_id}' as legacy_run_id") == 2
