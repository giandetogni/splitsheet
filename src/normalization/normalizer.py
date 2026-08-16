"""Deterministic, pure, staged normalization of the fields exposed by v_matcher_input.

Pure by construction: no I/O, no clock, no randomness, no network.

THREE OUTPUTS PER FIELD, and conflating them was the bug this module was rewritten to fix:

  *_normalized_unicode  the real normalized value. Script-preserving: Cyrillic stays
                        Cyrillic, CJK stays CJK. This is what a human would call
                        "the normalized title".
  *_lookup_exact        an ASCII-only blocking key. ASCII purely because that is what
                        matches the observed MusicBrainz combined_lookup convention.
  *_lookup_fallback     the aggressive key, usable only after the exact key has failed.

A Cyrillic title normalizes perfectly well and is VALID. It simply has no ASCII lookup
key. Treating "no ASCII key" as "invalid record" would discard real listens for the sole
crime of not being written in Latin script.

Nothing here matches, blocks or scores.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

from .rules import NormalizationRules, load_rules

_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_SEPARATORS = " -–—_/|,:;."
_ASCII_DELETE = str.maketrans("", "", "".join(
    c for c in map(chr, range(128)) if not c.isalnum()))


class NormalizationStatus(str, Enum):
    """Whether the CONTENT is usable. Independent of whether an ASCII key can be built."""

    VALID = "VALID"
    MISSING_ARTIST = "MISSING_ARTIST"
    MISSING_RECORDING = "MISSING_RECORDING"
    NO_ALPHANUMERIC_CONTENT = "NO_ALPHANUMERIC_CONTENT"


class KeyStatus(str, Enum):
    """Whether a combined lookup key could be built, and if not, why."""

    AVAILABLE = "AVAILABLE"
    #: Exactly one half produced a key. A combined key is NOT emitted, because
    #: artist-only or title-only would collide every such recording into one bucket.
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"


@dataclass(frozen=True)
class NormalizedFields:
    artist_normalized_unicode: str
    recording_normalized_unicode: str
    release_normalized_unicode: str

    artist_lookup_exact: str
    recording_lookup_exact: str
    release_lookup_exact: str
    lookup_exact: str

    artist_lookup_fallback: str
    recording_lookup_fallback: str
    lookup_fallback: str

    normalization_status: NormalizationStatus
    exact_key_status: KeyStatus
    fallback_key_status: KeyStatus
    status_detail: str
    transformations_applied: tuple[str, ...] = field(default=())
    normalization_version: str = ""

    @property
    def is_valid(self) -> bool:
        return self.normalization_status is NormalizationStatus.VALID

    @property
    def exact_key_usable(self) -> bool:
        return self.exact_key_status is KeyStatus.AVAILABLE

    @property
    def fallback_key_usable(self) -> bool:
        return self.fallback_key_status is KeyStatus.AVAILABLE


def normalized_unicode(text: str, rules: NormalizationRules) -> str:
    """Script-preserving normalization. Never empties valid non-Latin content.

    NFKD, drop combining marks, casefold, turn Unicode punctuation and symbols into
    spaces, collapse whitespace. Letters and digits of every script survive.
    """
    if not text:
        return ""
    out = unicodedata.normalize(rules.unicode_form, text)
    if rules.strip_diacritics:
        out = "".join(c for c in out if not unicodedata.combining(c))
    if rules.lowercase:
        # casefold, not lower: it handles ß -> ss and Greek final sigma correctly.
        out = out.casefold()
    # Unicode categories P* (punctuation), S* (symbols), C* (control) become spaces, so
    # word boundaries survive instead of words being glued together.
    out = "".join(" " if unicodedata.category(c)[0] in ("P", "S", "C") else c for c in out)
    return _WHITESPACE.sub(" ", out).strip()


def transliterated_for_key(text: str, rules: NormalizationRules) -> str:
    """Apply the configured transliteration, but ONLY on the lookup-key path.

    Added in normalization 1.1.0. Two properties matter:

      * `*_normalized_unicode` is untouched, so the stored representation of a Korean title stays
        Korean. Transliteration exists to build a KEY, not to rewrite content.
      * a string that is not FULLY covered is returned unchanged, so it folds to nothing exactly as
        it did under 1.0.0. A half-transliterated title would key on a fragment, which is the
        low-information key the Phase 4 preflight measured.

    With transliteration disabled (the frozen 1.0.0 rule set) this is the identity function, which
    is what keeps v1 reproducible.
    """
    if not rules.transliteration_enabled or not text:
        return text
    from normalization.transliterate import transliterate

    converted, covered = transliterate(text, rules.transliteration_scripts)
    if rules.transliteration_require_full_coverage and not covered:
        return text
    return converted


def fold(text: str, rules: NormalizationRules) -> str:
    """Reduce to ASCII alphanumerics: the lookup key, not the normalized value.

    Matches the observed MusicBrainz combined_lookup key on 90.84% of the measured
    canonical rows. Under 1.0.0 non-Latin scripts folded to empty because we had no
    transliteration table; under 1.1.0 hangul and kana are converted first (see
    `transliterated_for_key`), while kanji and every other script still fold to empty -- a missing
    KEY, not invalid content.
    """
    if not text:
        return ""
    text = transliterated_for_key(text, rules)
    if not text.isascii():
        text = unicodedata.normalize(rules.unicode_form, text)
        if rules.strip_diacritics:
            text = "".join(c for c in text if not unicodedata.combining(c))
    if rules.lowercase:
        text = text.lower()
    if text.isascii():
        return text.translate(_ASCII_DELETE)
    return "".join(c for c in text if c.isascii() and c.isalnum())


def _strip_featuring(text: str, rules: NormalizationRules, applied: list[str]) -> str:
    lowered = text.lower()
    cut = len(text)
    hit = None
    for marker in rules.featuring_markers:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered):
            if m.start() < cut:
                cut, hit = m.start(), marker
    if hit is not None:
        applied.append(f"featuring:{hit}")
    return text[:cut]


def _strip_year_structures(text: str, rules: NormalizationRules, applied: list[str]) -> str:
    """Remove a year only inside a recognised reissue structure, never on its own.

    A bare four-digit strip would destroy "1999", "1984", "2001" and "Class of 1984".
    """
    out = text
    for pattern in rules.year_structures:
        new = pattern.sub(" ", out)
        if new != out:
            applied.append(f"year_structure:{pattern.pattern}")
            out = new
    return out


def _strip_version_suffixes(text: str, rules: NormalizationRules,
                            applied: list[str]) -> str:
    """Remove version markers, but only where something precedes them.

    A version marker is by definition a SUFFIX. Anchoring at position 0 is what preserves
    the album title "Live 2000", the track "Mono", and "Live at Leeds".
    """
    out = text
    for suffix in rules.version_suffixes:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(suffix)}(?![a-z0-9])", re.IGNORECASE)
        pieces, last, changed = [], 0, False
        for m in pattern.finditer(out):
            if m.start() == 0 or not out[:m.start()].strip(_TRAILING_SEPARATORS).strip():
                continue  # leading token: part of the title, not a suffix
            pieces.append(out[last:m.start()])
            last, changed = m.end(), True
        if changed:
            pieces.append(out[last:])
            out = " ".join(pieces)
            applied.append(f"version_suffix:{suffix}")
    return out


def _strip_leading_article(text: str, rules: NormalizationRules, applied: list[str]) -> str:
    stripped = text.lstrip()
    lowered = stripped.lower()
    for article in rules.leading_articles:
        if lowered.startswith(article + " "):
            applied.append(f"leading_article:{article}")
            return stripped[len(article) + 1:]
    return text


def aggressive(text: str, rules: NormalizationRules,
               applied: list[str] | None = None) -> str:
    """The fallback stage. Order matters and is not arbitrary.

    Bracketed segments first, then featuring, then year STRUCTURES (before bare suffix
    removal, or the suffix would be consumed and the year orphaned), then bare suffixes,
    then the leading article.

    If the transformations would empty a non-empty title, they are reverted: a fallback
    that destroys the title is worse than no fallback at all.
    """
    acc: list[str] = [] if applied is None else applied
    if not text:
        return ""
    out = text
    if rules.strip_bracketed_segments:
        stripped = _BRACKETED.sub(" ", out)
        if stripped != out:
            acc.append("strip_bracketed")
            out = stripped
    out = _strip_featuring(out, rules, acc)
    out = _strip_year_structures(out, rules, acc)
    out = _strip_version_suffixes(out, rules, acc)
    out = _WHITESPACE.sub(" ", out).strip().strip(_TRAILING_SEPARATORS)
    out = _strip_leading_article(out, rules, acc)

    folded = fold(out, rules)
    if not folded and fold(text, rules):
        acc.append("reverted:would_empty_title")
        return fold(text, rules)
    return folded


@lru_cache(maxsize=1)
def _default_rules() -> NormalizationRules:
    return load_rules()


def _key_status(a: str, b: str) -> KeyStatus:
    if a and b:
        return KeyStatus.AVAILABLE
    if a or b:
        return KeyStatus.PARTIAL
    return KeyStatus.EMPTY


def normalize(artist_name: str | None,
              recording_name: str | None,
              release_name: str | None = None,
              rules: NormalizationRules | None = None) -> NormalizedFields:
    """Normalize one listen. Content validity and key availability are reported separately."""
    r = rules or _default_rules()
    artist_raw, recording_raw = artist_name or "", recording_name or ""
    release_raw = release_name or ""

    artist_uni = normalized_unicode(artist_raw, r)
    recording_uni = normalized_unicode(recording_raw, r)
    release_uni = normalized_unicode(release_raw, r)

    artist_exact = fold(artist_raw, r)
    recording_exact = fold(recording_raw, r)
    release_exact = fold(release_raw, r)

    applied: list[str] = []
    artist_fb = aggressive(artist_raw, r, applied)
    recording_fb = aggressive(recording_raw, r, applied)

    # Content validity is judged on the UNICODE value, never on the ASCII key.
    if not artist_raw.strip():
        status, detail = NormalizationStatus.MISSING_ARTIST, "artist_name is absent or blank"
    elif not recording_raw.strip():
        status, detail = (NormalizationStatus.MISSING_RECORDING,
                          "recording_name is absent or blank")
    elif not artist_uni or not recording_uni:
        which = "artist" if not artist_uni else "recording"
        status = NormalizationStatus.NO_ALPHANUMERIC_CONTENT
        detail = f"{which} contains no letters or digits in any script"
    else:
        status, detail = NormalizationStatus.VALID, ""

    exact_status = _key_status(artist_exact, recording_exact)
    fallback_status = _key_status(artist_fb, recording_fb)
    if status is not NormalizationStatus.VALID:
        detail = detail or ""
    elif exact_status is not KeyStatus.AVAILABLE:
        detail = ("content is valid but has no ASCII lookup key; "
                  "MusicBrainz romanises these and we have no transliteration table")

    return NormalizedFields(
        artist_normalized_unicode=artist_uni,
        recording_normalized_unicode=recording_uni,
        release_normalized_unicode=release_uni,
        artist_lookup_exact=artist_exact,
        recording_lookup_exact=recording_exact,
        release_lookup_exact=release_exact,
        # Only ever emitted when BOTH halves exist.
        lookup_exact=(artist_exact + recording_exact
                      if exact_status is KeyStatus.AVAILABLE else ""),
        artist_lookup_fallback=artist_fb,
        recording_lookup_fallback=recording_fb,
        lookup_fallback=(artist_fb + recording_fb
                         if fallback_status is KeyStatus.AVAILABLE else ""),
        normalization_status=status,
        exact_key_status=exact_status,
        fallback_key_status=fallback_status,
        status_detail=detail,
        transformations_applied=tuple(applied),
        normalization_version=r.version,
    )
