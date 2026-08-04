"""Unit tests for every scoring feature. Pure: no GCP, no config, no cost.

These pin the Python side of each feature. The SQL side is generated from the same
definitions, and an integration test recomputes published rows in Python to prove the two
agree on real data -- see tests/integration/test_match_results_contract.py.
"""

from __future__ import annotations

import pytest

from matching import features as F

# --- unicode_exact ----------------------------------------------------------------------

def test_unicode_exact_is_true_only_for_identical_non_empty_values():
    assert F.unicode_exact("radiohead", "radiohead")
    assert not F.unicode_exact("radiohead", "radio head")


def test_two_empty_values_are_not_a_match():
    """Absence of content is not evidence of agreement: '' == '' must not score as a hit."""
    assert not F.unicode_exact("", "")
    assert not F.unicode_exact("", "radiohead")


def test_unicode_exact_preserves_script():
    assert F.unicode_exact("けやき坂46", "けやき坂46")
    assert not F.unicode_exact("けやき坂46", "keyakizaka46")


# --- token_similarity -------------------------------------------------------------------

def test_token_similarity_is_order_insensitive():
    assert F.token_similarity("the beatles", "beatles the") == 1.0


def test_token_similarity_is_jaccard():
    # {a,b,c} vs {b,c,d}: intersection 2, union 4
    assert F.token_similarity("a b c", "b c d") == 0.5


def test_token_similarity_of_empty_side_is_zero_not_one():
    assert F.token_similarity("", "beatles") == 0.0
    assert F.token_similarity("", "") == 0.0


def test_token_similarity_handles_repeated_tokens_as_sets():
    assert F.token_similarity("na na na", "na") == 1.0


# --- edit distance and string_similarity ------------------------------------------------

@pytest.mark.parametrize(("a", "b", "d"), [
    ("", "", 0), ("abc", "abc", 0), ("abc", "abd", 1), ("abc", "ab", 1),
    ("kitten", "sitting", 3), ("", "abc", 3), ("flaw", "lawn", 2),
])
def test_edit_distance(a, b, d):
    assert F.edit_distance(a, b) == d
    assert F.edit_distance(b, a) == d, "edit distance must be symmetric"


def test_string_similarity_is_one_for_identical_and_zero_for_empty():
    assert F.string_similarity("abc", "abc") == 1.0
    assert F.string_similarity("", "abc") == 0.0
    assert F.string_similarity("abc", "") == 0.0


def test_string_similarity_scales_by_the_longer_side():
    # one substitution out of three characters
    assert F.string_similarity("abc", "abd") == pytest.approx(2 / 3)


def test_string_similarity_counts_code_points_not_bytes():
    """CHAR_LENGTH in BigQuery counts characters, so Python must not count UTF-8 bytes."""
    assert F.string_similarity("é", "é") == 1.0
    assert F.string_similarity("日本語", "日本") == pytest.approx(2 / 3)


# --- ascii retention and the information class -------------------------------------------

def test_ascii_retention_ratio_of_pure_latin_content_is_one():
    r = F.ascii_retention_ratio("radiohead", "creep", "radiohead creep")
    assert r == pytest.approx(1.0)


def test_ascii_retention_ratio_drops_when_the_script_is_discarded():
    # 5 ASCII survivors out of 8 alphanumerics: the CJK content left no ASCII behind.
    r = F.ascii_retention_ratio("チノ", "cv 水瀬", "cvver")
    assert r is not None
    assert r < F.LOW_INFORMATION_RATIO_CUT + 1.0


def test_ascii_retention_ratio_is_none_without_alphanumeric_content():
    """A ratio over zero content is undefined, not zero."""
    assert F.ascii_retention_ratio("", "", "") is None
    assert F.ascii_retention_ratio("!!!", "???", "") is None


def test_information_class_uses_the_measured_cut():
    assert F.information_class(0.19) == F.LOW_INFORMATION
    assert F.information_class(0.20) == F.NORMAL
    assert F.information_class(1.0) == F.NORMAL


def test_undefined_ratio_is_not_called_low_information():
    """No content cannot be low-information content; those listens have no key at all."""
    assert F.information_class(None) == F.NORMAL


# --- the bundle used by the builder ------------------------------------------------------

def test_features_for_pair_returns_every_scored_feature():
    got = F.features_for_pair("radiohead", "creep", "radiohead", "creep")
    assert set(got) == {
        "artist_unicode_exact", "recording_unicode_exact",
        "artist_token_similarity", "recording_token_similarity",
        "artist_string_similarity", "recording_string_similarity"}
    assert all(v == 1.0 or v is True for v in got.values())


def test_features_for_pair_on_a_complete_mismatch():
    got = F.features_for_pair("radiohead", "creep", "kino", "gruppa krovi")
    assert got["artist_unicode_exact"] is False
    assert got["recording_token_similarity"] == 0.0


# --- the generated SQL is at least well-formed and references the right columns ----------

def test_sql_generators_mention_both_sides():
    sql = F.sql_feature_columns("l", "k")
    for col in ("l.artist_normalized_unicode", "k.artist_normalized_unicode",
                "l.recording_normalized_unicode", "k.recording_normalized_unicode"):
        assert col in sql
    for name in ("artist_unicode_exact", "recording_token_similarity",
                 "artist_string_similarity"):
        assert f"AS {name}" in sql


def test_sql_information_class_uses_the_same_constant_as_python():
    assert str(F.LOW_INFORMATION_RATIO_CUT) in F.sql_information_class("r")
    assert F.LOW_INFORMATION in F.sql_information_class("r")
