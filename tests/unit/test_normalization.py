"""Unit tests for staged normalization. Pure: no GCP, no credentials, no cost.

The tests are organised around the risks that matter rather than around coverage: that the
two stages behave differently in the specific way the Phase 0A measurement relies on, that
word-boundary handling cannot silently truncate real names, that unusable input is
classified instead of dropped, and that the version tracks the rules.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
import yaml

from normalization import Status, aggressive, fold, load_rules, normalize

RULES = load_rules()
# Anchored to the repo root, not the working directory, so the suite does not depend
# on where pytest was invoked from.
RULES_PATH = pathlib.Path(__file__).parents[2] / "config/normalization_rules.yml"


def _raw_rules() -> dict:
    with open(RULES_PATH) as fh:
        return yaml.safe_load(fh)


# --- stage 1: exact ---------------------------------------------------------------------

@pytest.mark.parametrize(("artist", "recording", "expected"), [
    # Values measured in Phase 0A; the library must reproduce them.
    ("Radiohead", "Paranoid Android", "radioheadparanoidandroid"),
    ("Radiohead", "Paranoid Android - Remastered 2017",
     "radioheadparanoidandroidremastered2017"),
    ("Radiohead", "Paranoid Android (Live)", "radioheadparanoidandroidlive"),
    # NFKD + diacritic stripping.
    ("Beyoncé", "Déjà Vu", "beyoncedejavu"),
    ("Sigur Rós", "Untitled #3", "sigurrosuntitled3"),
    # Punctuation and whitespace disappear as a consequence of keeping only alphanumerics.
    ("A  Tribe   Called Quest", "Can I Kick It?", "atribecalledquestcanikickit"),
    ("AC/DC", "T.N.T.", "acdctnt"),
    # The exact stage preserves articles and featuring credits on purpose.
    ("The Beatles", "Let It Be", "thebeatlesletitbe"),
])
def test_exact_key_is_conservative(artist, recording, expected):
    assert normalize(artist, recording).exact_key == expected


# --- stage 2: fallback ------------------------------------------------------------------

@pytest.mark.parametrize(("recording", "expected"), [
    ("Paranoid Android", "paranoidandroid"),
    ("Paranoid Android - Remastered 2017", "paranoidandroid"),
    ("Paranoid Android (Live)", "paranoidandroid"),
    ("Paranoid Android - 2017 Remaster", "paranoidandroid"),
    ("Paranoid Android [Deluxe Edition]", "paranoidandroid"),
    ("Paranoid Android - Radio Edit", "paranoidandroid"),
    ("Paranoid Android (Mono)", "paranoidandroid"),
    ("Paranoid Android - Bonus Track", "paranoidandroid"),
    ("Paranoid Android (feat. Someone)", "paranoidandroid"),
])
def test_fallback_collapses_version_markers(recording, expected):
    assert aggressive(recording, RULES) == expected


def test_fallback_strips_leading_article_but_exact_does_not():
    n = normalize("The Beatles", "Let It Be")
    assert n.exact_key == "thebeatlesletitbe"
    assert n.fallback_key == "beatlesletitbe"


# --- the property the staged design depends on ------------------------------------------

VERSION_VARIANTS = [
    "Paranoid Android",
    "Paranoid Android - Remastered 2017",
    "Paranoid Android (Live)",
    "Paranoid Android - 2017 Remaster",
    "Paranoid Android [Deluxe Edition]",
]


def test_variants_stay_distinct_at_exact_and_merge_at_fallback():
    """This is the whole reason the stages are split.

    Phase 0A measured that merging these costs more than it gains: listens with exactly one
    candidate fall from 73.64% to 51.93%. So the exact stage must keep them apart, and only
    the fallback stage may merge them.
    """
    exact = {normalize("Radiohead", v).exact_key for v in VERSION_VARIANTS}
    fallback = {normalize("Radiohead", v).fallback_key for v in VERSION_VARIANTS}
    assert len(exact) == len(VERSION_VARIANTS), "exact stage merged variants it must keep apart"
    assert len(fallback) == 1, "fallback stage failed to merge equivalent variants"
    assert fallback == {"radioheadparanoidandroid"}


# --- word boundaries: a wrong match silently truncates a real name ----------------------

@pytest.mark.parametrize(("text", "expected"), [
    # 'ft' inside a word must not trigger the featuring rule.
    ("Ftisha", "ftisha"),
    ("Aftermath", "aftermath"),
    ("Drift", "drift"),
    # 'with' inside a word.
    ("Withered Hand", "witheredhand"),
    ("Within Temptation", "withintemptation"),
    # 'live' inside a word must not be treated as a version marker.
    ("Deliverance", "deliverance"),
    ("Oliver", "oliver"),
    ("Alive", "alive"),
    # 'mix' and 'mono' inside words.
    ("Monolith", "monolith"),
    ("Mixtape Vol 1", "mixtapevol1"),
])
def test_markers_are_word_boundary_anchored(text, expected):
    assert aggressive(text, RULES) == expected


def test_featuring_marker_at_a_boundary_is_stripped():
    assert aggressive("Song feat. Someone", RULES) == "song"
    assert aggressive("Song ft Someone", RULES) == "song"
    assert aggressive("Song with Someone", RULES) == "song"


# --- unusable input is classified, never dropped ----------------------------------------

@pytest.mark.parametrize(("artist", "recording", "status"), [
    ("Various Artists", "乡愁四韵", Status.UNSUPPORTED_SCRIPT),
    ("椎名林檎", "丸ノ内サディスティック", Status.UNSUPPORTED_SCRIPT),
    ("", "Some Track", Status.MISSING_FIELD),
    ("Some Artist", "", Status.MISSING_FIELD),
    ("   ", "Some Track", Status.MISSING_FIELD),
    (None, "Some Track", Status.MISSING_FIELD),
    ("Some Artist", None, Status.MISSING_FIELD),
    ("Radiohead", "Paranoid Android", Status.OK),
])
def test_unusable_input_is_classified(artist, recording, status):
    n = normalize(artist, recording)
    assert n.status is status
    if status is not Status.OK:
        assert not n.has_exact_key
        assert n.status_detail, "a non-OK status must carry a reason"


def test_no_key_is_built_from_only_one_half():
    """A romanisable artist with an unromanisable title must not produce a key equal to the
    artist alone; that would collapse every such track by that artist into one bucket."""
    n = normalize("Various Artists", "乡愁四韵")
    assert n.artist_exact == "variousartists"
    assert n.recording_exact == ""
    assert n.exact_key == ""
    assert n.fallback_key == ""


def test_a_title_that_is_only_version_markers_does_not_vanish_into_a_partial_key():
    n = normalize("Radiohead", "(Live)")
    assert n.exact_key == "radioheadlive"
    assert n.fallback_key == "", "fallback emptied the title, so no key may be formed"


# --- determinism, purity, idempotency ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "Paranoid Android - Remastered 2017", "Déjà Vu", "AC/DC", "乡愁四韵", "",
])
def test_fold_is_idempotent(text):
    once = fold(text, RULES)
    assert fold(once, RULES) == once


def test_repeated_calls_are_identical():
    a = normalize("Björk", "Jóga (Remastered 2017)")
    b = normalize("Björk", "Jóga (Remastered 2017)")
    assert a == b


def test_every_result_carries_the_normalization_version():
    for artist, recording in [("Radiohead", "Karma Police"), ("", ""), ("A", "乡愁")]:
        assert normalize(artist, recording).normalization_version == RULES.version


# --- versioning tracks the rules --------------------------------------------------------

def test_version_is_semantic_plus_rules_digest():
    assert RULES.version.startswith(RULES.semantic_version + "+")
    assert len(RULES.rules_digest) == 12


def test_version_changes_when_a_rule_changes(tmp_path):
    """A rule edit must change the version even if nobody bumps it by hand, because a
    payout computed under different rules has to be distinguishable."""
    raw = _raw_rules()
    raw["fallback"]["version_suffixes"].append("acoustic")
    p = tmp_path / "changed.yml"
    p.write_text(yaml.safe_dump(raw))
    assert load_rules(p).version != RULES.version


def test_version_is_stable_under_reordering_and_reformatting(tmp_path):
    """Reordering a YAML list is not a behavioural change and must not look like one."""
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
