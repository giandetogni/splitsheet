"""Unit tests for transliteration and for the 1.1.0 normalization rule. Pure: no GCP.

Four things are defended here, and the last two are the ones that make a restatement honest:

  1. the conversions are correct and deterministic;
  2. kanji is NOT converted, and a title containing it produces NO key rather than a fragment;
  3. normalization 1.0.0 is still REPRODUCIBLE from its frozen copy, byte for byte, digest and all;
  4. the new rule cannot appear under the old version -- proven by mutation, not asserted.
"""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

from normalization import load_rules, normalize
from normalization.normalizer import transliterated_for_key
from normalization.transliterate import (
    SCRIPT_HANGUL,
    SCRIPT_KANA,
    is_han,
    is_hangul,
    is_kana,
    script_inventory,
    transliterate,
    transliterate_hangul,
    transliterate_kana,
    would_transliterate,
)

REPO = pathlib.Path(__file__).parents[2]
V1_PATH = REPO / "config/normalization_rules_v1.0.0.yml"
LIVE_PATH = REPO / "config/normalization_rules.yml"

V1 = load_rules(V1_PATH)
V2 = load_rules()

EXPECTED_V1_VERSION = "1.0.0+0bc0dd643e06"
EXPECTED_V2_VERSION = "1.1.0+b3253b155934"


# --- Hangul --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hangul", "romanised"),
    [
        ("해금", "haegeum"),  # the restatement's headline case
        ("쩔어", "jjeoleo"),
        ("팔도강산", "paldogangsan"),
        ("다음", "daeum"),
        ("한국", "hanguk"),  # final consonant romanises as k, not g
        ("아", "a"),  # initial ieung is silent
    ],
)
def test_hangul_syllables_romanise_by_the_arithmetic_decomposition(hangul, romanised):
    assert transliterate_hangul(hangul) == romanised


def test_hangul_transliteration_is_deterministic():
    assert transliterate_hangul("해금") == transliterate_hangul("해금")


def test_hangul_is_detected_and_kana_is_not():
    assert is_hangul("해") and not is_kana("해") and not is_han("해")


# --- Kana ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kana", "romaji"),
    [
        ("アイドル", "aidoru"),
        ("キャラメル", "kyarameru"),  # yoon merges into the previous syllable
        ("ヨルシカ", "yorushika"),
        ("ん", "n"),
        ("ドラゴン", "doragon"),
        ("サッカー", "sakkaa".replace("aa", "a")),  # sokuon doubles, chouonpu drops
    ],
)
def test_kana_romanises_by_syllable(kana, romaji):
    assert transliterate_kana(kana) == romaji


def test_hiragana_and_katakana_give_the_same_result():
    assert transliterate_kana("あいどる") == transliterate_kana("アイドル")


def test_sokuon_doubles_the_following_consonant():
    assert transliterate_kana("ホッケ") == "hokke"


def test_kana_is_detected():
    assert is_kana("ア") and is_kana("あ") and not is_hangul("ア")


# --- kanji is deliberately not converted ---------------------------------------------------


def test_kanji_is_recognised_and_never_transliterated():
    assert is_han("夜") and is_han("駆")
    converted, covered = transliterate("夜に駆ける")
    assert not covered, "a title with kanji must not be reported as covered"
    assert "夜" in converted, "kanji must be left alone, not guessed at"


def test_a_partially_convertible_title_yields_no_key_rather_than_a_fragment():
    """The low-information key failure the Phase 4 preflight measured, prevented by construction."""
    fields = normalize("YOASOBI", "夜に駆ける", rules=V2)
    assert fields.lookup_exact == ""
    assert fields.exact_key_status.value != "AVAILABLE"
    # And the Unicode representation is still intact: no content was destroyed.
    assert fields.recording_normalized_unicode


def test_transliterated_for_key_returns_the_input_unchanged_when_not_covered():
    assert transliterated_for_key("夜に駆ける", V2) == "夜に駆ける"


# --- coverage and inventory ----------------------------------------------------------------


def test_full_coverage_is_required_for_a_key():
    for text in ("해금", "アイドル", "Creep", "해금 remix"):
        _, covered = transliterate(text)
        assert covered, text
    for text in ("夜に駆ける", "Кино", "海金"):
        _, covered = transliterate(text)
        assert not covered, text


def test_latin_and_neutral_characters_are_left_alone():
    assert transliterate("Creep (Live 2000)") == ("Creep (Live 2000)", True)


def test_script_inventory_counts_what_is_actually_there():
    inventory = script_inventory("Agust D 해금")
    assert inventory["hangul"] == 2
    assert inventory["han"] == 0
    assert inventory["latin_or_neutral"] > 0


def test_would_transliterate_only_fires_for_enabled_scripts():
    assert would_transliterate("해금", (SCRIPT_HANGUL,))
    assert not would_transliterate("アイドル", (SCRIPT_HANGUL,))
    assert would_transliterate("アイドル", (SCRIPT_HANGUL, SCRIPT_KANA))
    assert not would_transliterate("Creep", (SCRIPT_HANGUL, SCRIPT_KANA))


# --- v1 stays reproducible -----------------------------------------------------------------


def test_the_frozen_v1_rule_set_still_produces_its_exact_version():
    """If this fails, the v1 financial publication is no longer reproducible from its rules and the
    baseline's provenance is broken."""
    assert V1.version == EXPECTED_V1_VERSION
    assert not V1.transliteration_enabled
    assert V1.transliteration_scripts == ()


def test_adding_transliteration_did_not_rehash_the_old_rule_set():
    """The digest payload must OMIT the transliteration key when the block is absent, not include it
    as null. Including it as null rehashed v1 on the first attempt and this caught it."""
    with open(V1_PATH) as fh:
        raw_v1 = yaml.safe_load(fh)
    assert "transliteration" not in raw_v1
    assert load_rules(V1_PATH).version == EXPECTED_V1_VERSION


def test_v1_still_refuses_the_key_that_v2_now_produces():
    before = normalize("Agust D", "해금", rules=V1)
    after = normalize("Agust D", "해금", rules=V2)
    assert before.lookup_exact == ""
    assert after.lookup_exact == "agustdhaegeum"
    assert before.normalization_version == EXPECTED_V1_VERSION
    assert after.normalization_version == EXPECTED_V2_VERSION


def test_transliteration_never_changes_the_preserved_unicode_value():
    """The stored representation of a Korean title stays Korean under both versions."""
    for artist, title in [("Agust D", "해금"), ("YOASOBI", "アイドル")]:
        before = normalize(artist, title, rules=V1)
        after = normalize(artist, title, rules=V2)
        assert before.recording_normalized_unicode == after.recording_normalized_unicode
        assert before.artist_normalized_unicode == after.artist_normalized_unicode


def test_latin_content_is_byte_identical_between_the_two_versions():
    """The unaffected cohort must not move. Anything else would make the restatement unbounded."""
    for artist, title in [
        ("Radiohead", "Creep"),
        ("The Beatles", "Let It Be (Remastered 2009)"),
        ("Kino", "Gruppa krovi"),
        ("Sigur Ros", "Hoppipolla"),
    ]:
        before = normalize(artist, title, rules=V1)
        after = normalize(artist, title, rules=V2)
        assert before.lookup_exact == after.lookup_exact
        assert before.lookup_fallback == after.lookup_fallback
        assert before.artist_normalized_unicode == after.artist_normalized_unicode
        assert before.recording_normalized_unicode == after.recording_normalized_unicode


# --- versioning, proven by mutation --------------------------------------------------------


def test_the_live_version_is_pinned():
    assert V2.version == EXPECTED_V2_VERSION


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r["transliteration"].update({"scripts": ["hangul"]}), id="scripts"),
        pytest.param(lambda r: r["transliteration"].update({"enabled": False}), id="enabled"),
        pytest.param(lambda r: r["transliteration"].update({"stage": "everywhere"}), id="stage"),
    ],
)
def test_changing_the_transliteration_rule_changes_the_version(tmp_path, mutate):
    """The new rule cannot appear silently under any existing version string."""
    with open(LIVE_PATH) as fh:
        raw = yaml.safe_load(fh)
    mutated = copy.deepcopy(raw)
    mutate(mutated)
    p = tmp_path / "mutated.yml"
    with open(p, "w") as fh:
        yaml.safe_dump(mutated, fh)
    changed = load_rules(p)
    assert changed.version != V2.version
    assert changed.version != EXPECTED_V1_VERSION


def test_a_rule_set_naming_an_unsupported_script_is_refused(tmp_path):
    with open(LIVE_PATH) as fh:
        raw = yaml.safe_load(fh)
    raw["transliteration"]["scripts"] = ["hangul", "han"]
    p = tmp_path / "han.yml"
    with open(p, "w") as fh:
        yaml.safe_dump(raw, fh)
    with pytest.raises(ValueError, match="algorithmic romanisation"):
        load_rules(p)


def test_partial_coverage_cannot_be_switched_on(tmp_path):
    with open(LIVE_PATH) as fh:
        raw = yaml.safe_load(fh)
    raw["transliteration"]["require_full_coverage"] = False
    p = tmp_path / "partial.yml"
    with open(p, "w") as fh:
        yaml.safe_dump(raw, fh)
    with pytest.raises(ValueError, match="fragment"):
        load_rules(p)
