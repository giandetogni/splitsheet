"""Unit tests for the versioned payout policy. Pure: no GCP.

MATCHED != PAYABLE, tested as a property of the policy object rather than trusted as prose:

  * the gates fire in the documented order, and the first failure is the reported reason;
  * SCORED_FALLBACK_UNIQUE stays MATCHED and is never payable;
  * no rate is ever imputed and no state ever falls back to UNKNOWN;
  * the policy cannot change without its version changing (mutation-tested);
  * the dbt project's var still equals the digest-sealed policy version, so the warehouse cannot
    apply one policy while claiming another.
"""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

from payout.policy import (
    ATTRIBUTABLE,
    DEFAULT_CONFIG_PATH,
    DEFECTIVE_OWNERSHIP,
    MATCH_RISK_POLICY,
    RATE_CARD_AMBIGUOUS,
    RATE_CARD_GAP,
    UNMATCHED,
    attribution_run_id,
    compute_digest,
    load_payout_policy,
)

POLICY = load_payout_policy()

#: Pinned. Every published amount was produced under this exact policy version.
EXPECTED_POLICY_VERSION = "1.0.0+84b4b68a37c9"

DBT_PROJECT = pathlib.Path(__file__).parents[2] / "dbt/dbt_project.yml"


def _raw() -> dict:
    with open(DEFAULT_CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


def _write(tmp_path, raw: dict):
    p = tmp_path / "changed.yml"
    with open(p, "w") as fh:
        yaml.safe_dump(raw, fh)
    return p


# --- versioning, proven by mutation -------------------------------------------------------

def test_policy_version_is_pinned():
    assert POLICY.version == EXPECTED_POLICY_VERSION, (
        "the payout policy changed without updating EXPECTED_POLICY_VERSION. A new policy means a "
        f"new publication, not an edited one: set it to {POLICY.version!r} in the same commit.")


def test_the_dbt_project_applies_the_policy_it_claims_to(tmp_path):
    """The warehouse reads the version from a dbt var. If that drifts from the sealed config, every
    published row would carry a version that did not produce it."""
    project = yaml.safe_load(DBT_PROJECT.read_text())
    assert project["vars"]["payout_policy_version"] == POLICY.version


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: r["match_risk"].update({"hold_methods": []}), id="hold_methods"),
    pytest.param(lambda r: r["ownership"].update(
        {"payable_statuses": ["OWNERSHIP_RESOLVED", "OWNERSHIP_DEFECTIVE"]}), id="ownership_gate"),
    pytest.param(lambda r: r["money"]["rounding"].update({"tiebreak": "random"}), id="tiebreak"),
    pytest.param(lambda r: r["money"].update({"published_scale": 4}), id="published_scale"),
    pytest.param(lambda r: r["inputs"].update({"match_run_id": "match:something_else"}),
                 id="input_binding"),
    pytest.param(lambda r: r.update({"fact_grain": ["period", "recording_mbid"]}), id="grain"),
])
def test_changing_the_policy_without_the_digest_fails_the_load(tmp_path, mutate):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(ValueError, match="digest"):
        load_payout_policy(_write(tmp_path, raw))


def test_prose_edits_do_not_change_the_digest(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["grain_adjustment_reason"] = "reworded"
    raw["match_risk"]["expected_held_listens"] = 35007
    assert compute_digest(raw) == POLICY.rules_digest
    load_payout_policy(_write(tmp_path, raw))


@pytest.mark.parametrize(("mutate", "match"), [
    (lambda r: r["rate"].update({"imputation": "previous_rate"}), "imputation"),
    (lambda r: r["money"].update({"negative_payout": "ALLOWED"}), "negative"),
    (lambda r: r["money"]["rounding"].update({"single_rounding_point": False}), "close in cents"),
    (lambda r: r["money"]["rounding"].update({"method": "round_half_up_per_holder"}),
     "unsupported rounding"),
    (lambda r: r["publication"].update({"destructive_update": "ALLOWED"}), "destructively"),
    (lambda r: r.update({"attribution_states": [*r["attribution_states"], "UNKNOWN"]}),
     "UNKNOWN"),
])
def test_a_policy_that_would_invent_money_is_refused(tmp_path, mutate, match):
    """These are not style preferences. Each one, if permitted, changes a published amount or hides
    why it was not published."""
    raw = copy.deepcopy(_raw())
    mutate(raw)
    raw["rules_sha256"] = compute_digest(raw)
    with pytest.raises(ValueError, match=match):
        load_payout_policy(_write(tmp_path, raw))


# --- the gates ----------------------------------------------------------------------------

def test_unmatched_is_never_payable():
    for method in ("NO_CANDIDATES", "SCORED_EXACT_MULTIPLE", "SCORED_FALLBACK_MULTIPLE"):
        got = POLICY.disposition("UNRESOLVED", method, None, None)
        assert got == UNMATCHED
        assert not POLICY.is_payable(got)


def test_fallback_unique_stays_matched_and_is_held_not_downgraded():
    got = POLICY.disposition("MATCHED", "SCORED_FALLBACK_UNIQUE", "RESOLVED", 1)
    assert got == MATCH_RISK_POLICY
    assert not POLICY.is_payable(got)
    assert "SCORED_FALLBACK_UNIQUE" in POLICY.hold_methods
    assert POLICY.expected_held_listens == 35_007


def test_a_held_method_is_held_even_when_ownership_and_rate_are_perfect():
    assert POLICY.disposition("MATCHED", "SCORED_FALLBACK_UNIQUE", "RESOLVED", 1) \
        == MATCH_RISK_POLICY


@pytest.mark.parametrize("resolution", [
    "DEFECTIVE_OWNERSHIP", "COVERED_ONLY_BY_INVALID_SET", "NO_OWNERSHIP_RECORD",
    "OWNERSHIP_GAP_FOR_DATE", "MULTIPLE_VALID_SETS",
])
def test_every_ownership_problem_blocks_payment(resolution):
    got = POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE", resolution, 1)
    assert got == DEFECTIVE_OWNERSHIP
    assert not POLICY.is_payable(got)


def test_ownership_is_checked_before_rate():
    """A listen failing both gates is reported as the ownership problem: an unknown owner cannot be
    paid at any rate, so reporting it as a pricing issue would misdirect the fix."""
    assert POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE",
                              "DEFECTIVE_OWNERSHIP", 0) == DEFECTIVE_OWNERSHIP


def test_a_missing_rate_is_a_gap_and_never_an_imputed_value():
    got = POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE", "RATE_CARD_GAP", 0)
    assert got == RATE_CARD_GAP
    assert not POLICY.is_payable(got)


def test_two_covering_rates_are_ambiguous_not_averaged():
    got = POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE", "RESOLVED", 2)
    assert got == RATE_CARD_AMBIGUOUS
    assert not POLICY.is_payable(got)


def test_all_gates_passed_is_the_only_payable_state():
    got = POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE", "RESOLVED", 1)
    assert got == ATTRIBUTABLE
    assert POLICY.is_payable(got)
    assert sum(POLICY.is_payable(s) for s in POLICY.attribution_states) == 1


def test_an_unmapped_resolution_status_fails_loudly_instead_of_defaulting():
    """No UNKNOWN bucket: a status the policy has never seen is a policy gap, and a gap that
    quietly resolves to 'unpayable' would hide a broken upstream contract."""
    with pytest.raises(ValueError, match="no ownership mapping"):
        POLICY.disposition("MATCHED", "STRUCTURAL_EXACT_UNIQUE", "SOMETHING_NEW", 1)


def test_terminal_states_contain_no_unknown():
    assert "UNKNOWN" not in POLICY.attribution_states
    assert len(set(POLICY.attribution_states)) == len(POLICY.attribution_states)


# --- run ids ------------------------------------------------------------------------------

def test_attribution_run_id_is_deterministic_and_label_specific():
    a = attribution_run_id(POLICY)
    assert a == attribution_run_id(POLICY)
    assert a.startswith("attr:")
    assert attribution_run_id(POLICY, "REHEARSAL") != a


def test_the_fact_grain_includes_rate_card_id_and_says_why():
    """The grain adjustment is documented in the config, not just in a commit message."""
    assert "rate_card_id" in POLICY.fact_grain
    assert "rate card changes value inside the period" in _raw()["grain_adjustment_reason"]


# --- the generated SQL matches the Python -------------------------------------------------

def test_sql_disposition_encodes_the_same_gates_in_the_same_order():
    sql = POLICY.sql_disposition()
    order = [sql.index(state) for state in
             (UNMATCHED, MATCH_RISK_POLICY, DEFECTIVE_OWNERSHIP, RATE_CARD_GAP, ATTRIBUTABLE)]
    assert order == sorted(order), "the SQL gates are not in the policy order"
    assert "SCORED_FALLBACK_UNIQUE" in sql
    assert "UNKNOWN" not in sql


def test_sql_ownership_status_maps_every_known_resolution_status():
    sql = POLICY.sql_ownership_status()
    for status in POLICY.ownership_status_map:
        assert f"'{status}'" in sql
    assert "ELSE NULL END" in sql, "an unmapped status must become NULL, not a default"
