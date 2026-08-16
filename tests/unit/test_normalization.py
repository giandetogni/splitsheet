"""Unit tests for staged normalization. Pure: no GCP, no credentials, no cost.

Organised around risks, not coverage: that the Unicode value and the ASCII lookup key are
genuinely separate concepts, that non-Latin content stays valid, that no partial key can
escape, that legitimate numeric titles survive, and that the version cannot drift from the
rules.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
import yaml

from normalization import (
    KeyStatus,
    NormalizationStatus,
    aggressive,
    fold,
    load_rules,
    normalize,
    normalized_unicode,
)

RULES = load_rules()
# Anchored to the repo root, not the working directory.
RULES_PATH = pathlib.Path(__file__).parents[2] / "config/normalization_rules.yml"

#: Golden pin. Editing config/normalization_rules.yml changes the digest and fails this
#: test, which is the mechanism that forces a rule change to be acknowledged rather than
#: slipped in. Update it deliberately, in the same commit as the rule change.
EXPECTED_VERSION = "1.1.0+b3253b155934"

#: The FROZEN v1 rule set, kept so the v1 financial publication stays reproducible after the
#: Phase 6 restatement moved the live rules to 1.1.0.
V1_RULES_PATH = pathlib.Path(__file__).parents[2] / "config/normalization_rules_v1.0.0.yml"
EXPECTED_V1_VERSION = "1.0.0+0bc0dd643e06"


def _raw_rules() -> dict:
    with open(RULES_PATH) as fh:
        return yaml.safe_load(fh)


# --- the separation this module exists to enforce ---------------------------------------

#: Scripts with NO algorithmic romanisation. These still produce no ASCII key under 1.1.0, and that
#: is the rule working rather than a gap: romanising them would need a dictionary.
NON_LATIN = [
    pytest.param("Кино", "Группа крови", id="cyrillic"),
    pytest.param("Ελευθερία Αρβανιτάκη", "Δυναμίτης", id="greek"),
    pytest.param("فيروز", "زهرة المدائن", id="arabic"),
    pytest.param("椎名林檎", "丸ノ内サディスティック", id="cjk-japanese-with-kanji"),
    pytest.param("周杰倫", "七里香", id="cjk-chinese"),
]

#: Scripts the 1.1.0 transliteration rule DOES cover. Under the frozen 1.0.0 rules they produced no
#: key; that behaviour is asserted against the frozen copy in tests/unit/test_transliteration.py.
TRANSLITERABLE = [
    pytest.param("아이유", "좋은 날", id="hangul"),
    pytest.param("YOASOBI", "アイドル", id="kana"),
]


@pytest.mark.parametrize(("artist", "recording"), NON_LATIN)
def test_non_latin_content_is_valid_and_keeps_its_script(artist, recording):
    """A missing ASCII key is a missing KEY, never an invalid record."""
    n = normalize(artist, recording)
    assert n.normalization_status is NormalizationStatus.VALID
    assert n.artist_normalized_unicode, "unicode normalization must not empty valid content"
    assert n.recording_normalized_unicode
    assert not n.artist_normalized_unicode.isascii(), "script was not preserved"
    assert n.lookup_exact == ""
    assert n.exact_key_status is KeyStatus.EMPTY


@pytest.mark.parametrize(("artist", "recording"), TRANSLITERABLE)
def test_transliterable_scripts_now_produce_a_key_and_keep_their_script(artist, recording):
    """The 1.1.0 change, stated as a property: a key appears, and the stored value stays non-Latin.

    This test replaced an assertion that hangul yields no key. That assertion was true of 1.0.0 and
    is exactly what the Phase 6 restatement changed, so it was updated deliberately rather than
    relaxed -- and the old behaviour is still asserted, against the frozen v1 rule set.
    """
    n = normalize(artist, recording)
    assert n.normalization_status is NormalizationStatus.VALID
    assert not n.recording_normalized_unicode.isascii(), "script must still be preserved"
    assert n.lookup_exact, "a transliterable title must produce a key under 1.1.0"
    assert n.lookup_exact.isascii()
    assert n.exact_key_status is KeyStatus.AVAILABLE


@pytest.mark.parametrize(("artist", "recording", "expected"), [
    pytest.param("Radiohead", "丸ノ内サディスティック", KeyStatus.PARTIAL, id="latin-artist-cjk-track"),
    pytest.param("椎名林檎", "Tokyo", KeyStatus.PARTIAL, id="cjk-artist-latin-track"),
    pytest.param("椎名林檎", "丸ノ内サディスティック", KeyStatus.EMPTY, id="both-cjk"),
    pytest.param("Radiohead", "Creep", KeyStatus.AVAILABLE, id="both-latin"),
])
def test_key_status_distinguishes_partial_from_empty(artist, recording, expected):
    n = normalize(artist, recording)
    assert n.exact_key_status is expected
    assert n.normalization_status is NormalizationStatus.VALID


@pytest.mark.parametrize(("artist", "recording"), [
    ("Radiohead", "丸ノ内サディスティック"),
    ("椎名林檎", "Tokyo"),
    ("Various Artists", "乡愁四韵"),
])
def test_no_partial_key_is_ever_emitted(artist, recording):
    """Half a key would collapse every such recording by that artist into one bucket."""
    n = normalize(artist, recording)
    assert n.lookup_exact == ""
    assert n.lookup_fallback == ""
    assert not n.exact_key_usable
    assert not n.fallback_key_usable


def test_punctuation_only_is_a_content_error():
    n = normalize("!!!", "???")
    assert n.normalization_status is NormalizationStatus.NO_ALPHANUMERIC_CONTENT
    assert n.artist_normalized_unicode == ""
    assert n.status_detail


@pytest.mark.parametrize(("artist", "recording", "status"), [
    ("", "Some Track", NormalizationStatus.MISSING_ARTIST),
    (None, "Some Track", NormalizationStatus.MISSING_ARTIST),
    ("   ", "Some Track", NormalizationStatus.MISSING_ARTIST),
    ("Some Artist", "", NormalizationStatus.MISSING_RECORDING),
    ("Some Artist", None, NormalizationStatus.MISSING_RECORDING),
])
def test_absent_fields_are_classified_not_dropped(artist, recording, status):
    n = normalize(artist, recording)
    assert n.normalization_status is status
    assert n.status_detail


def test_composed_and_decomposed_unicode_converge():
    """U+00E9 and 'e' + U+0301 must normalize identically, or one title indexes twice."""
    composed, decomposed = "Beyoncé", "Beyoncé"
    assert composed != decomposed
    a, b = normalize(composed, "Halo"), normalize(decomposed, "Halo")
    assert a.artist_normalized_unicode == b.artist_normalized_unicode
    assert a.lookup_exact == b.lookup_exact == "beyoncehalo"


def test_casefold_handles_sharp_s_and_final_sigma():
    assert normalized_unicode("STRASSE", RULES) == normalized_unicode("Straße", RULES)
    assert normalized_unicode("ΟΔΟΣ", RULES) == \
        normalized_unicode("οδός", RULES)


def test_unicode_punctuation_becomes_a_space_not_a_join():
    assert normalized_unicode("Rock—Roll", RULES) == "rock roll"
    assert normalized_unicode("Don’t Stop", RULES) == "don t stop"


# --- stage 1: exact ---------------------------------------------------------------------

@pytest.mark.parametrize(("artist", "recording", "expected"), [
    ("Radiohead", "Paranoid Android", "radioheadparanoidandroid"),
    ("Radiohead", "Paranoid Android - Remastered 2017",
     "radioheadparanoidandroidremastered2017"),
    ("Radiohead", "Paranoid Android (Live)", "radioheadparanoidandroidlive"),
    ("Beyoncé", "Déjà Vu", "beyoncedejavu"),
    ("Sigur Rós", "Untitled #3", "sigurrosuntitled3"),
    ("A  Tribe   Called Quest", "Can I Kick It?", "atribecalledquestcanikickit"),
    ("AC/DC", "T.N.T.", "acdctnt"),
    ("The Beatles", "Let It Be", "thebeatlesletitbe"),
])
def test_exact_lookup_is_conservative(artist, recording, expected):
    assert normalize(artist, recording).lookup_exact == expected


def test_exact_never_applies_fallback_rules():
    """If the exact stage starts stripping, the 73.64% measurement stops describing it."""
    for recording, marker in [("Song - Live", "live"),
                              ("Song (Remastered 2017)", "remastered"),
                              ("Song feat. X", "feat")]:
        n = normalize("The Artist", recording)
        assert marker in n.lookup_exact, f"exact stage stripped {marker!r}"
        assert n.lookup_exact.startswith("theartist"), "exact stripped a leading article"


# --- stage 2: fallback ------------------------------------------------------------------

@pytest.mark.parametrize(("recording", "expected"), [
    ("Paranoid Android", "paranoidandroid"),
    ("Paranoid Android - Remastered 2017", "paranoidandroid"),
    ("Paranoid Android - 2017 Remaster", "paranoidandroid"),
    ("Paranoid Android (Live)", "paranoidandroid"),
    ("Paranoid Android [Deluxe Edition]", "paranoidandroid"),
    ("Paranoid Android - Radio Edit", "paranoidandroid"),
    ("Paranoid Android (feat. Someone)", "paranoidandroid"),
    ("Paranoid Android - Anniversary Edition 2017", "paranoidandroid"),
])
def test_fallback_collapses_version_markers(recording, expected):
    assert aggressive(recording, RULES) == expected


def test_variants_stay_distinct_at_exact_and_merge_at_fallback():
    variants = ["Paranoid Android", "Paranoid Android - Remastered 2017",
                "Paranoid Android (Live)", "Paranoid Android - 2017 Remaster",
                "Paranoid Android [Deluxe Edition]"]
    exact = {normalize("Radiohead", v).lookup_exact for v in variants}
    fallback = {normalize("Radiohead", v).lookup_fallback for v in variants}
    assert len(exact) == len(variants), "exact stage merged variants it must keep apart"
    assert fallback == {"radioheadparanoidandroid"}


# --- years: only inside recognised structures -------------------------------------------

@pytest.mark.parametrize("title", [
    "1999", "1984", "2001", "Class of 1984", "Live 2000", "Summer of 69",
    "1969", "2112", "Nineteen 1985",
])
def test_legitimate_numeric_titles_are_preserved(title):
    """A bare four-digit strip would destroy every one of these."""
    digits = "".join(c for c in title if c.isdigit())
    assert digits and digits in aggressive(title, RULES), \
        f"fallback removed the year from the legitimate title {title!r}"


@pytest.mark.parametrize(("title", "must_not_contain"), [
    ("Song - Remastered 2017", "2017"),
    ("Song - 2017 Remaster", "2017"),
    ("Song - Remastered in 1998", "1998"),
    ("Song - Anniversary Edition 2017", "2017"),
    ("Song - 2011 Remastered Version", "2011"),
])
def test_year_is_removed_only_inside_a_recognised_structure(title, must_not_contain):
    assert must_not_contain not in aggressive(title, RULES)


def test_leading_version_word_is_not_treated_as_a_suffix():
    """'Live 2000' is a title; 'Song - Live' is a version. Position separates them."""
    assert aggressive("Live 2000", RULES) == "live2000"
    assert aggressive("Live at Leeds", RULES) == "liveatleeds"
    assert aggressive("Mono", RULES) == "mono"
    assert aggressive("Song - Live", RULES) == "song"


def test_transformations_applied_is_recorded():
    n = normalize("The Beatles", "Let It Be - Remastered 2009 (Live)")
    assert n.transformations_applied, "no audit trail of what was applied"
    joined = " ".join(n.transformations_applied)
    assert "strip_bracketed" in joined
    assert "year_structure" in joined
    assert "leading_article:the" in joined


def test_fallback_never_destroys_a_title_entirely():
    n = normalize("Radiohead", "(Live)")
    assert n.lookup_fallback, "fallback emptied the title instead of reverting"
    assert "reverted:would_empty_title" in n.transformations_applied


# --- word boundaries --------------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("Ftisha", "ftisha"), ("Aftermath", "aftermath"), ("Drift", "drift"),
    ("Withered Hand", "witheredhand"), ("Within Temptation", "withintemptation"),
    ("Deliverance", "deliverance"), ("Oliver", "oliver"), ("Alive", "alive"),
    ("Monolith", "monolith"), ("Mixtape Vol 1", "mixtapevol1"),
])
def test_markers_are_word_boundary_anchored(text, expected):
    assert aggressive(text, RULES) == expected


def test_featuring_marker_at_a_boundary_is_stripped():
    for title in ["Song feat. Someone", "Song ft Someone", "Song with Someone"]:
        assert aggressive(title, RULES) == "song"


# --- invariants over a deterministic corpus ---------------------------------------------

CORPUS = [
    ("Radiohead", "Paranoid Android"), ("Radiohead", "Paranoid Android - Remastered 2017"),
    ("Beyoncé", "Déjà Vu"), ("The Beatles", "Let It Be"),
    ("Кино", "Группа крови"),
    ("椎名林檎", "丸ノ内"), ("فيروز", "زهرة"),
    ("Ελευθερία", "Δυναμίτης"),
    ("AC/DC", "T.N.T."), ("Prince", "1999"), ("Various", "Live 2000"),
    ("!!!", "???"), ("", "Orphan"), ("Orphan", ""),
    ("Sigur Rós", "Untitled #3"), ("Withered Hand", "Drift"),
    ("아이유", "좋은 날"),
]


@pytest.mark.parametrize(("artist", "recording"), CORPUS)
def test_invariant_normalization_is_idempotent(artist, recording):
    n = normalize(artist, recording)
    again = normalize(n.artist_normalized_unicode, n.recording_normalized_unicode)
    assert again.artist_normalized_unicode == n.artist_normalized_unicode
    assert again.recording_normalized_unicode == n.recording_normalized_unicode
    assert fold(n.lookup_exact, RULES) == n.lookup_exact


@pytest.mark.parametrize(("artist", "recording"), CORPUS)
def test_invariant_valid_content_never_normalizes_to_empty(artist, recording):
    n = normalize(artist, recording)
    if n.normalization_status is NormalizationStatus.VALID:
        assert n.artist_normalized_unicode and n.recording_normalized_unicode


@pytest.mark.parametrize(("artist", "recording"), CORPUS)
def test_invariant_combined_key_is_never_partial(artist, recording):
    n = normalize(artist, recording)
    for key, status in ((n.lookup_exact, n.exact_key_status),
                        (n.lookup_fallback, n.fallback_key_status)):
        assert bool(key) == (status is KeyStatus.AVAILABLE)


@pytest.mark.parametrize(("artist", "recording"), CORPUS)
def test_invariant_repeated_calls_are_identical(artist, recording):
    assert normalize(artist, recording) == normalize(artist, recording)


@pytest.mark.parametrize(("artist", "recording"), CORPUS)
def test_invariant_every_result_carries_the_version(artist, recording):
    assert normalize(artist, recording).normalization_version == RULES.version


# --- versioning cannot drift from the rules ---------------------------------------------

def test_version_matches_the_pinned_value():
    """Fails if the rules changed without updating EXPECTED_VERSION.

    This is the demonstrated link between rules and version: proven by mutation, not
    asserted. See docs/schema_notes.md.
    """
    assert RULES.version == EXPECTED_VERSION, (
        "normalization rules changed without updating EXPECTED_VERSION. If the change was "
        f"intended, set EXPECTED_VERSION to {RULES.version!r} in the same commit."
    )


def test_version_is_semantic_plus_rules_digest():
    assert RULES.version.startswith(RULES.semantic_version + "+")
    assert len(RULES.rules_digest) == 12


def test_version_changes_when_any_rule_changes(tmp_path):
    raw = _raw_rules()
    raw["fallback"]["version_suffixes"].append("acoustic")
    p = tmp_path / "changed.yml"
    p.write_text(yaml.safe_dump(raw))
    assert load_rules(p).version != RULES.version


def test_version_is_stable_under_reordering_and_reformatting(tmp_path):
    raw = _raw_rules()
    raw["fallback"]["version_suffixes"].reverse()
    raw["fallback"]["featuring_markers"].reverse()
    p = tmp_path / "reordered.yml"
    p.write_text(yaml.safe_dump(raw, default_flow_style=True))
    assert load_rules(p).version == RULES.version


def test_rules_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RULES.semantic_version = "9.9.9"  # type: ignore[misc]


def test_unsupported_rule_values_are_rejected(tmp_path):
    raw = _raw_rules()
    raw["common"]["keep"] = "everything"
    p = tmp_path / "bad.yml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="keep"):
        load_rules(p)
