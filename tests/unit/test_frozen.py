"""The Phase 4B freeze, enforced by the suite rather than by good intentions.

`config/frozen_versions.yml` records which versions produced the published result. Every metric
in docs/schema_notes.md sections 17-21 and every number in docs/restatement_candidates.md was
measured under them, and the validation partition was opened once under them and is consumed.

So a change to a normalization rule, a scoring weight or a threshold is not an edit -- it is a
restatement, and it must fail here first. Pure: no GCP.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from evaluation import load_split_config
from matching.scoring import load_scoring_rules
from normalization import load_rules

MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config/frozen_versions.yml"


@pytest.fixture(scope="module")
def frozen() -> dict:
    with open(MANIFEST_PATH) as fh:
        return yaml.safe_load(fh)


def test_the_frozen_normalization_copy_still_reproduces_the_baseline_version(frozen):
    """The v1 financial publication must stay reproducible after the restatement moved the live
    rules. Its rule set lives in a frozen copy, and this is the guard that it still loads to the
    exact version that produced pub:v1."""
    frozen_copy = MANIFEST_PATH.parents[1] / frozen["restatement"]["prior_normalization_frozen_copy"]
    assert frozen_copy.exists(), "the frozen v1 rule set is gone; v1 is no longer reproducible"
    assert load_rules(frozen_copy).version == frozen["pipeline"]["normalization_version"]


def test_the_live_normalization_version_is_the_declared_restatement_version(frozen):
    """The live rules may move only to a version this manifest declares. An undeclared change --
    including a well-meaning tweak to the transliteration table -- fails here rather than silently
    producing a third normalization version nobody recorded."""
    assert load_rules().version == frozen["restatement"]["new_normalization_version"], (
        "the live normalization version is neither the frozen baseline nor the declared "
        "restatement version. Declare it in config/frozen_versions.yml in the same commit.")


def test_scoring_version_is_frozen(frozen):
    assert load_scoring_rules().version == frozen["pipeline"]["scoring_version"], (
        "scoring rules changed while Phase 4B is frozen. Weights, thresholds, the margin and "
        "tie_epsilon are all frozen; changing one requires a new scoring_version AND a new "
        "validation strategy, because the validation partition is consumed.")


def test_weights_and_thresholds_are_the_calibrated_ones(frozen):
    rules = load_scoring_rules()
    assert rules.weights == {
        "artist_unicode_exact": 0.1, "recording_unicode_exact": 1.0,
        "artist_token_similarity": 0.1, "recording_token_similarity": 1.0,
        "artist_string_similarity": 0.1, "recording_string_similarity": 1.0,
        "release_lower_exact": 0.5,
    }
    assert (rules.exact_threshold, rules.fallback_threshold) == (0.55, 0.85)
    assert (rules.minimum_score_margin, rules.tie_epsilon) == (0.02, 0.01)


def test_the_restatement_left_scoring_rights_and_payout_frozen(frozen):
    """A restatement is allowed to change ONE thing. Scoring, rights and payout policy must be
    identical to the baseline, or the delta could not be attributed to the trigger."""
    r = frozen["restatement"]
    assert r["scoring_version"] == frozen["pipeline"]["scoring_version"]
    assert r["rights_version"] == "1.0.0+47f801102e17"
    assert r["payout_policy_version"] == "1.0.0+84b4b68a37c9"
    assert load_scoring_rules().version == r["scoring_version"]
    assert r["prior_normalization_version"] != r["new_normalization_version"]


def test_the_baseline_publication_identity_is_recorded(frozen):
    r = frozen["restatement"]
    for key in ("baseline_publication_id", "baseline_attribution_run_id",
                "baseline_content_digest", "baseline_portfolio_paid", "baseline_row_count"):
        assert r[key], key
    assert r["baseline_content_digest"] == "51193c1f2c4e8fcd8b8fa78ff197350e"
    assert r["baseline_portfolio_paid"] == "98284.22"


def test_evaluation_split_versions_are_frozen(frozen):
    cfg = load_split_config()
    assert cfg.split_version == frozen["evaluation"]["split_version"]
    assert cfg.calibration_split_version == frozen["evaluation"]["calibration_split_version"]


def test_validation_partition_is_recorded_as_consumed(frozen):
    """No tuning may be justified by it, and the manifest must keep saying so."""
    assert frozen["evaluation"]["validation_partition"] == "CONSUMED"
    assert frozen["evaluation"]["reuse_for_tuning"] == "FORBIDDEN"
    assert frozen["evaluation"]["validation_consumed_under"] == \
        frozen["pipeline"]["scoring_version"]


def test_published_counts_are_internally_consistent(frozen):
    """The frozen numbers must add up, or the freeze is recording a typo."""
    p = frozen["published_result"]
    assert (p["structural_exact_unique"] + p["scored_exact_multiple"]
            + p["scored_fallback_unique"] + p["scored_fallback_multiple"]) == p["matched"]
    unresolved = (p["below_threshold"] + p["ambiguous_tie"] + p["no_block_candidates"]
                  + p["no_lookup_key_partial"] + p["no_lookup_key_empty"]
                  + p["no_alphanumeric_content"])
    assert p["matched"] + unresolved == p["rows"] == 38_199_641


def test_restatement_register_keeps_the_transliteration_evidence():
    """The 해금 concentration is the evidence a future restatement is measured against."""
    text = (pathlib.Path(__file__).parents[2] / "docs/restatement_candidates.md").read_text()
    for required in ("해금", "384,926", "1,164,629", "NO_LOOKUP_KEY_PARTIAL",
                     "1.0.0+0bc0dd643e06", "1.0.0+cb21f9704ff0"):
        assert required in text, f"restatement evidence lost: {required}"
