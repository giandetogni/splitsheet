"""Deterministic, algorithmic transliteration for Hangul and Japanese kana. No dictionary.

TRANSLITERATION IS NOT TRANSLATION. `해금` becomes `haegeum`, not "Haegeum (a Korean fiddle)". This
module maps writing systems to Latin letters and never touches meaning.

WHY ONLY THESE SCRIPTS, and this is the whole design decision:

  * HANGUL is algorithmic. A syllable block decomposes arithmetically into initial, medial and
    final jamo (S = 0xAC00 + (L*21 + V)*28 + T), and each jamo has a fixed Latin mapping. No lookup
    of a word is required, so the result is reproducible from the code point alone.
  * KANA is algorithmic. Hiragana and katakana are syllabaries: each character has one reading,
    with small deterministic rules for the sokuon (small tsu doubles the next consonant), the
    chouonpu (long vowel mark), yoon (small ya/yu/yo) and syllabic n.
  * KANJI IS NOT. A Han character has multiple readings chosen by context and by the word it
    appears in; 生 alone has more than ten. Romanising kanji requires a dictionary and a parser,
    both of which introduce a data dependency, a language model, and a class of silent errors that
    would be indistinguishable from a matching bug. So kanji is NOT transliterated here, and a
    title containing kanji is reported as NOT FULLY COVERED rather than half-converted.

THE COVERAGE RULE, and why it is strict: a partially transliterated title yields a lookup key built
from a fragment of the content, which is exactly the low-information-key failure the Phase 4
preflight measured (`cvver`: 5 ASCII characters surviving from 26 Unicode alphanumerics, one key
producing 434,700 candidate pairs). So a key is only emitted when EVERY non-Latin character in the
string was transliterated. Half a title is not a key.

The romanisations are simple, per-character mappings: Revised Romanization letter values for jamo
and Hepburn-style values for kana, WITHOUT the assimilation and long-vowel refinements a
linguistically complete romaniser would apply. That is a deliberate limitation, documented and
tested, because a deterministic approximation that both sides of a join apply identically is worth
more here than a linguistically better one that only one side applies.
"""

from __future__ import annotations

import unicodedata

# --- Hangul -------------------------------------------------------------------------------

HANGUL_BASE = 0xAC00
HANGUL_LAST = 0xD7A3

# Revised Romanization letter values, in Unicode jamo order.
INITIAL_JAMO = (
    "g",
    "kk",
    "n",
    "d",
    "tt",
    "r",
    "m",
    "b",
    "pp",
    "s",
    "ss",
    "",
    "j",
    "jj",
    "ch",
    "k",
    "t",
    "p",
    "h",
)
MEDIAL_JAMO = (
    "a",
    "ae",
    "ya",
    "yae",
    "eo",
    "e",
    "yeo",
    "ye",
    "o",
    "wa",
    "wae",
    "oe",
    "yo",
    "u",
    "wo",
    "we",
    "wi",
    "yu",
    "eu",
    "ui",
    "i",
)
FINAL_JAMO = (
    "",
    "k",
    "k",
    "ks",
    "n",
    "nj",
    "nh",
    "t",
    "l",
    "lk",
    "lm",
    "lb",
    "ls",
    "lt",
    "lp",
    "lh",
    "m",
    "p",
    "ps",
    "t",
    "t",
    "ng",
    "t",
    "t",
    "k",
    "t",
    "p",
    "t",
)

# Compatibility jamo that appear on their own (not inside a syllable block).
STANDALONE_JAMO = {
    "ㄱ": "g",
    "ㄴ": "n",
    "ㄷ": "d",
    "ㄹ": "r",
    "ㅁ": "m",
    "ㅂ": "b",
    "ㅅ": "s",
    "ㅇ": "",
    "ㅈ": "j",
    "ㅊ": "ch",
    "ㅋ": "k",
    "ㅌ": "t",
    "ㅍ": "p",
    "ㅎ": "h",
    "ㅏ": "a",
    "ㅑ": "ya",
    "ㅓ": "eo",
    "ㅕ": "yeo",
    "ㅗ": "o",
    "ㅛ": "yo",
    "ㅜ": "u",
    "ㅠ": "yu",
    "ㅡ": "eu",
    "ㅣ": "i",
}

# --- Kana ---------------------------------------------------------------------------------

KATAKANA_TO_ROMAJI = {
    "ア": "a",
    "イ": "i",
    "ウ": "u",
    "エ": "e",
    "オ": "o",
    "カ": "ka",
    "キ": "ki",
    "ク": "ku",
    "ケ": "ke",
    "コ": "ko",
    "サ": "sa",
    "シ": "shi",
    "ス": "su",
    "セ": "se",
    "ソ": "so",
    "タ": "ta",
    "チ": "chi",
    "ツ": "tsu",
    "テ": "te",
    "ト": "to",
    "ナ": "na",
    "ニ": "ni",
    "ヌ": "nu",
    "ネ": "ne",
    "ノ": "no",
    "ハ": "ha",
    "ヒ": "hi",
    "フ": "fu",
    "ヘ": "he",
    "ホ": "ho",
    "マ": "ma",
    "ミ": "mi",
    "ム": "mu",
    "メ": "me",
    "モ": "mo",
    "ヤ": "ya",
    "ユ": "yu",
    "ヨ": "yo",
    "ラ": "ra",
    "リ": "ri",
    "ル": "ru",
    "レ": "re",
    "ロ": "ro",
    "ワ": "wa",
    "ヰ": "i",
    "ヱ": "e",
    "ヲ": "o",
    "ン": "n",
    "ガ": "ga",
    "ギ": "gi",
    "グ": "gu",
    "ゲ": "ge",
    "ゴ": "go",
    "ザ": "za",
    "ジ": "ji",
    "ズ": "zu",
    "ゼ": "ze",
    "ゾ": "zo",
    "ダ": "da",
    "ヂ": "ji",
    "ヅ": "zu",
    "デ": "de",
    "ド": "do",
    "バ": "ba",
    "ビ": "bi",
    "ブ": "bu",
    "ベ": "be",
    "ボ": "bo",
    "パ": "pa",
    "ピ": "pi",
    "プ": "pu",
    "ペ": "pe",
    "ポ": "po",
    "ヴ": "vu",
}
# Small kana that modify the PREVIOUS syllable rather than standing alone.
YOON = {"ャ": "ya", "ュ": "yu", "ョ": "yo", "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o"}
SOKUON = "ッ"  # doubles the following consonant
CHOUONPU = "ー"  # long vowel mark: dropped, per the simple mapping
KATAKANA_MIDDLE_DOT = "・"

HIRAGANA_START, HIRAGANA_END = 0x3041, 0x3096
KATAKANA_OFFSET = 0x30A1 - 0x3041


def _hiragana_to_katakana(ch: str) -> str:
    code = ord(ch)
    if HIRAGANA_START <= code <= HIRAGANA_END:
        return chr(code + KATAKANA_OFFSET)
    return ch


def is_hangul(ch: str) -> bool:
    return HANGUL_BASE <= ord(ch) <= HANGUL_LAST or ch in STANDALONE_JAMO


def is_kana(ch: str) -> bool:
    code = ord(_hiragana_to_katakana(ch))
    return 0x30A1 <= code <= 0x30FF


def is_han(ch: str) -> bool:
    """Kanji / Chinese characters. Deliberately NOT transliterated: no dictionary here."""
    return "CJK UNIFIED IDEOGRAPH" in unicodedata.name(ch, "")


def is_latin_or_neutral(ch: str) -> bool:
    """Characters that need no transliteration: Latin letters, digits, and anything that is not a
    letter or a number at all (punctuation, spaces, symbols)."""
    if ch.isascii():
        return True
    category = unicodedata.category(ch)
    if category[0] not in ("L", "N"):
        return True
    name = unicodedata.name(ch, "")
    return name.startswith("LATIN")


def transliterate_hangul(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if HANGUL_BASE <= code <= HANGUL_LAST:
            index = code - HANGUL_BASE
            initial, medial, final = index // 588, (index % 588) // 28, index % 28
            out.append(INITIAL_JAMO[initial] + MEDIAL_JAMO[medial] + FINAL_JAMO[final])
        elif ch in STANDALONE_JAMO:
            out.append(STANDALONE_JAMO[ch])
        else:
            out.append(ch)
    return "".join(out)


def transliterate_kana(text: str) -> str:
    """Katakana and hiragana to romaji. Sokuon doubles, chouonpu drops, yoon merges."""
    chars = [_hiragana_to_katakana(c) for c in text]
    out: list[str] = []
    pending_double = False
    for i, ch in enumerate(chars):
        if ch == SOKUON:
            pending_double = True
            continue
        if ch == CHOUONPU:
            continue  # long vowel mark carries no extra Latin letter here
        if ch == KATAKANA_MIDDLE_DOT:
            out.append(" ")
            continue
        if ch in YOON:
            # Merge into the previous syllable: キ + ャ -> kya, not ki + ya.
            if out and out[-1] and out[-1][-1] in "aiueo":
                base = out[-1]
                stem = base[:-1]
                if stem.endswith(("sh", "ch", "j")):
                    out[-1] = stem + YOON[ch][-1]
                else:
                    out[-1] = stem + YOON[ch]
            else:
                out.append(YOON[ch])
            continue
        romaji = KATAKANA_TO_ROMAJI.get(ch)
        if romaji is None:
            out.append(ch)
            continue
        if pending_double and romaji:
            romaji = romaji[0] + romaji
            pending_double = False
        # Syllabic n before a vowel would be ambiguous; the simple mapping accepts that.
        out.append(romaji)
    if pending_double:
        pending_double = False
    return "".join(out)


# --- the rule applied to a whole string ---------------------------------------------------

#: Script sets a rule may cover. Each name is what appears in config/normalization_rules.yml.
SCRIPT_HANGUL = "hangul"
SCRIPT_KANA = "kana"
KNOWN_SCRIPTS = (SCRIPT_HANGUL, SCRIPT_KANA)


def script_inventory(text: str) -> dict[str, int]:
    """Which scripts a string actually contains, for evidence rather than for guessing."""
    counts = {"hangul": 0, "kana": 0, "han": 0, "latin_or_neutral": 0, "other": 0}
    for ch in text:
        if is_hangul(ch):
            counts["hangul"] += 1
        elif is_kana(ch):
            counts["kana"] += 1
        elif is_han(ch):
            counts["han"] += 1
        elif is_latin_or_neutral(ch):
            counts["latin_or_neutral"] += 1
        else:
            counts["other"] += 1
    return counts


def transliterate(text: str, scripts: tuple[str, ...] = KNOWN_SCRIPTS) -> tuple[str, bool]:
    """Transliterate the enabled scripts and report whether the result is FULLY covered.

    Returns (transliterated_text, fully_covered). `fully_covered` is False when any character
    remains that is neither Latin nor neutral -- kanji, an unenabled script, or anything else. The
    caller must not build a lookup key from a string that is not fully covered: a key made from
    part of a title is the low-information key failure the preflight measured.
    """
    if not text:
        return "", False
    out = text
    if SCRIPT_HANGUL in scripts:
        out = transliterate_hangul(out)
    if SCRIPT_KANA in scripts:
        out = transliterate_kana(out)
    covered = all(is_latin_or_neutral(ch) for ch in out)
    return out, covered


def would_transliterate(text: str, scripts: tuple[str, ...] = KNOWN_SCRIPTS) -> bool:
    """True when the string contains at least one character the enabled scripts would change."""
    for ch in text:
        if SCRIPT_HANGUL in scripts and is_hangul(ch):
            return True
        if SCRIPT_KANA in scripts and is_kana(ch):
            return True
    return False
