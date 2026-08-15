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


def test_normalization_version_is_frozen(frozen):
    assert load_rules().version == frozen["pipeline"]["normalization_version"], (
        "normalization rules changed while Phase 4B is frozen. That is a restatement: see "
        "docs/restatement_candidates.md, not an edit to this file.")


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
