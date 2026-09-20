"""Unit tests for the frozen scoring config and the decision policy. Pure: no GCP.

Two things are being defended here:

  * the numbers in config/scoring_rules.yml cannot change without the suite noticing, proven
    by mutation rather than asserted;
  * the decision policy refuses every case it is supposed to refuse, in particular that
    nothing is ever resolved by candidate order and that a single candidate is never called
    an ambiguous tie.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from matching.scoring import (
    ACCEPTED,
    AMBIGUOUS_TIE,
    BELOW_THRESHOLD,
    DEFAULT_CONFIG_PATH,
    NO_BLOCK_CANDIDATES,
    compute_digest,
    load_scoring_rules,
)

RULES = load_scoring_rules()

#: Pinned. Every metric in docs/schema_notes.md was measured under this exact version, and the
#: validation partition was opened once under it. A silent change invalidates all of that.
EXPECTED_SCORING_VERSION = "1.0.0+cb21f9704ff0"

ALL_FEATURES = {
    "artist_unicode_exact": True,
    "recording_unicode_exact": True,
    "artist_token_similarity": 1.0,
    "recording_token_similarity": 1.0,
    "artist_string_similarity": 1.0,
    "recording_string_similarity": 1.0,
    "release_lower_exact": True,
}


def _raw() -> dict:
    with open(DEFAULT_CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


def _write(tmp_path, raw: dict, name: str = "changed.yml"):
    p = tmp_path / name
    with open(p, "w") as fh:
        yaml.safe_dump(raw, fh)
    return p


# --- versioning, proven by mutation -----------------------------------------------------


def test_scoring_version_is_pinned():
    assert RULES.version == EXPECTED_SCORING_VERSION, (
        "scoring rules changed without updating EXPECTED_SCORING_VERSION. If the change was "
        f"intended, set EXPECTED_SCORING_VERSION to {RULES.version!r} in the same commit, "
        "bump scoring_version, and re-run validation under a NEW version."
    )


def test_recorded_digest_matches_the_file_contents():
    assert _raw()["rules_sha256"] == RULES.rules_digest


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda r: r["features"]["recording_token_similarity"].update({"weight": 0.9}),
            id="weight",
        ),
        pytest.param(lambda r: r["thresholds"].update({"fallback": 0.80}), id="fallback_threshold"),
        pytest.param(lambda r: r["thresholds"].update({"exact": 0.60}), id="exact_threshold"),
        pytest.param(lambda r: r.update({"minimum_score_margin": 0.05}), id="margin"),
        pytest.param(lambda r: r.update({"tie_epsilon": 0.005}), id="tie_epsilon"),
        pytest.param(
            lambda r: r["low_information_policy"].update({"suppress_candidates": True}),
            id="low_information_policy",
        ),
    ],
)
def test_editing_a_rule_without_updating_the_digest_fails_the_load(tmp_path, mutate):
    """THE MUTATION TEST. A changed rule with a stale digest must stop the pipeline."""
    raw = copy.deepcopy(_raw())
    mutate(raw)
    p = _write(tmp_path, raw)
    with pytest.raises(ValueError, match="digest"):
        load_scoring_rules(p)


def test_a_changed_rule_produces_a_different_version_string(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["thresholds"]["fallback"] = 0.80
    raw["rules_sha256"] = compute_digest(raw)
    changed = load_scoring_rules(_write(tmp_path, raw))
    assert changed.version != RULES.version
    assert changed.rules_digest != RULES.rules_digest


def test_prose_only_edits_do_not_change_the_digest(tmp_path):
    """The digest must track behaviour, not commentary, or nobody will trust it."""
    raw = copy.deepcopy(_raw())
    raw["features"]["recording_token_similarity"]["source"] = "reworded description"
    raw["calibrated_on"]["grid_cells_evaluated"] = 99
    assert compute_digest(raw) == RULES.rules_digest
    load_scoring_rules(_write(tmp_path, raw))  # still loads


def test_load_refuses_a_config_that_would_suppress_low_information_candidates(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["low_information_policy"]["suppress_candidates"] = True
    raw["rules_sha256"] = compute_digest(raw)
    with pytest.raises(ValueError, match="suppressed"):
        load_scoring_rules(_write(tmp_path, raw))


def test_load_refuses_tie_epsilon_above_the_margin(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["tie_epsilon"] = 0.5
    raw["rules_sha256"] = compute_digest(raw)
    with pytest.raises(ValueError, match="tie_epsilon"):
        load_scoring_rules(_write(tmp_path, raw))


def test_load_refuses_a_feature_the_feature_table_does_not_have(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["features"]["popularity"] = {"weight": 1.0, "kind": "unit_interval"}
    raw["rules_sha256"] = compute_digest(raw)
    with pytest.raises(ValueError, match="do not exist"):
        load_scoring_rules(_write(tmp_path, raw))


# --- score combination -------------------------------------------------------------------


def test_all_features_present_and_perfect_scores_one():
    assert RULES.score(ALL_FEATURES) == pytest.approx(1.0)


def test_all_features_absent_of_similarity_scores_zero():
    assert (
        RULES.score({k: (False if isinstance(v, bool) else 0.0) for k, v in ALL_FEATURES.items()})
        == 0.0
    )


def test_score_is_the_configured_weighted_mean():
    feats = dict.fromkeys(ALL_FEATURES, 0.0)
    feats["recording_token_similarity"] = 1.0
    expected = RULES.weights["recording_token_similarity"] / sum(RULES.weights.values())
    assert RULES.score(feats) == pytest.approx(expected)


def test_a_null_feature_leaves_both_numerator_and_denominator():
    """A listen with no release must not be penalised for a field it never sent."""
    with_release = dict(ALL_FEATURES, release_lower_exact=False)
    without_release = dict(ALL_FEATURES, release_lower_exact=None)
    assert RULES.score(without_release) == pytest.approx(1.0)
    assert RULES.score(with_release) < 1.0


def test_score_is_none_when_nothing_could_be_compared():
    assert RULES.score(dict.fromkeys(ALL_FEATURES)) is None


# --- decision policy ---------------------------------------------------------------------


def test_exact_unique_is_not_this_functions_business():
    """Structural acceptance happens before scoring; decide() only sees scored paths."""
    assert RULES.decide("EXACT", 0, None, None) == NO_BLOCK_CANDIDATES


def test_fallback_unique_above_threshold_is_accepted():
    assert RULES.decide("FALLBACK", 1, RULES.fallback_threshold, None) == ACCEPTED


def test_fallback_unique_below_threshold_is_below_threshold_not_a_tie():
    assert RULES.decide("FALLBACK", 1, RULES.fallback_threshold - 0.01, None) == BELOW_THRESHOLD


def test_a_single_candidate_can_never_be_an_ambiguous_tie():
    for top1 in (0.0, 0.5, 0.9, 1.0):
        assert RULES.decide("FALLBACK", 1, top1, None) in (ACCEPTED, BELOW_THRESHOLD)
        assert RULES.decide("EXACT", 1, top1, None) in (ACCEPTED, BELOW_THRESHOLD)


def test_exact_multiple_separable_is_accepted():
    assert RULES.decide("EXACT", 4, 0.99, 0.40) == ACCEPTED


def test_fallback_multiple_separable_is_accepted():
    assert RULES.decide("FALLBACK", 4, 0.99, 0.40) == ACCEPTED


def test_fallback_multiple_uses_the_fallback_threshold_not_the_exact_one():
    between = (RULES.exact_threshold + RULES.fallback_threshold) / 2
    assert RULES.decide("EXACT", 3, between, 0.1) == ACCEPTED
    assert RULES.decide("FALLBACK", 3, between, 0.1) == BELOW_THRESHOLD


def test_equal_top_scores_are_an_ambiguous_tie():
    assert RULES.decide("EXACT", 2, 0.95, 0.95) == AMBIGUOUS_TIE


def test_margin_inside_tie_epsilon_is_an_ambiguous_tie():
    assert RULES.decide("EXACT", 2, 0.95, 0.95 - RULES.tie_epsilon / 2) == AMBIGUOUS_TIE


def test_margin_below_the_minimum_is_an_ambiguous_tie():
    top2 = 0.95 - (RULES.minimum_score_margin * 0.9)
    assert RULES.decide("EXACT", 2, 0.95, top2) == AMBIGUOUS_TIE


def test_margin_at_the_minimum_is_accepted():
    top2 = 0.95 - RULES.minimum_score_margin
    assert RULES.decide("EXACT", 2, 0.95, top2) == ACCEPTED


def test_threshold_is_checked_before_the_margin():
    """A weak lone leader is refused for weakness, not mislabelled as a tie."""
    assert RULES.decide("FALLBACK", 3, 0.10, 0.0) == BELOW_THRESHOLD


def test_no_outcome_depends_on_candidate_order():
    """decide() cannot see order: it takes only the two scores. This test documents that the
    signature is the guarantee -- there is no argument through which order could enter."""
    import inspect

    params = set(inspect.signature(RULES.decide).parameters)
    assert params == {"block_method", "candidate_count", "top1", "top2"}
    for banned in ("row_number", "rn", "candidate_recording_mbid", "order", "index"):
        assert banned not in params


# --- generated SQL carries the same numbers ----------------------------------------------


def test_sql_decision_contains_the_configured_numbers():
    sql = RULES.sql_decision()
    assert str(RULES.exact_threshold) in sql
    assert str(RULES.fallback_threshold) in sql
    assert str(RULES.tie_epsilon) in sql
    assert str(RULES.minimum_score_margin) in sql
    assert AMBIGUOUS_TIE in sql and BELOW_THRESHOLD in sql


def test_sql_score_uses_every_weighted_feature_and_no_other_column():
    sql = RULES.sql_score("f")
    for name, weight in RULES.weights.items():
        if weight:
            assert f"f.{name}" in sql
    for banned in ("mapper", "popularity", "score_col", "artist_credit_id", "ROW_NUMBER"):
        assert banned not in sql


def test_sql_score_casts_booleans_and_leaves_floats_alone():
    sql = RULES.sql_score()
    assert "CAST(artist_unicode_exact AS INT64)" in sql
    assert "CAST(recording_token_similarity" not in sql
