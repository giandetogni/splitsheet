"""Deterministic, pure, staged normalization of the fields exposed by v_matcher_input.

Pure by construction: no I/O, no clock, no randomness, no network. The same input and the
same rules always give the same output, which is what lets a normalization version be
meaningful and a restatement be explainable.

Nothing here matches, blocks or scores. It produces keys and a status; deciding what to do
with them belongs to a later phase.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from .rules import NormalizationRules, load_rules

# Bracketed segments: "(Live)", "[Remastered]". Non-greedy so "a (b) c (d)" loses both.
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FOUR_DIGIT_YEAR = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_WHITESPACE = re.compile(r"\s+")
# Trailing junk left behind after a segment is removed: " - ", " – ", " / ".
_TRAILING_SEPARATORS = " -–—_/|,:;."

_ASCII_DELETE = str.maketrans("", "", "".join(
    c for c in map(chr, range(128)) if not c.isalnum()))


class Status(str, Enum):
    """Why a row has, or does not have, a usable key. Never a reason to discard a row."""

    OK = "OK"
    MISSING_FIELD = "MISSING_FIELD"
    UNSUPPORTED_SCRIPT = "UNSUPPORTED_SCRIPT"


@dataclass(frozen=True)
class NormalizedFields:
    """Result for one listen. Both stages are always reported, so a caller can try the
    exact key first and fall back only where it found nothing."""

    artist_exact: str
    recording_exact: str
    release_exact: str
    exact_key: str

    artist_fallback: str
    recording_fallback: str
    fallback_key: str

    status: Status
    status_detail: str
    normalization_version: str

    @property
    def has_exact_key(self) -> bool:
        return bool(self.exact_key)

    @property
    def has_fallback_key(self) -> bool:
        return bool(self.fallback_key)


def fold(text: str, rules: NormalizationRules) -> str:
    """Reduce a string to ASCII alphanumerics.

    This is the conservative stage, and it intentionally mirrors MusicBrainz's
    combined_lookup convention rather than being maximally clever: measured identical for
    90.84% of canonical rows. Punctuation and whitespace vanish as a consequence of keeping
    only alphanumerics, so they need no separate step.
    """
    if not text:
        return ""
    if not text.isascii():
        text = unicodedata.normalize(rules.unicode_form, text)
        if rules.strip_diacritics:
            text = "".join(c for c in text if not unicodedata.combining(c))
    if rules.lowercase:
        text = text.lower()
    if text.isascii():
        return text.translate(_ASCII_DELETE)
    return "".join(c for c in text if c.isascii() and c.isalnum())


def _strip_featuring(text: str, rules: NormalizationRules) -> str:
    """Drop everything from a featuring marker onward.

    Word-boundary anchored, so "Ftisha" and "Withered" survive; a naive substring match
    would silently truncate legitimate names.
    """
    lowered = text.lower()
    cut = len(text)
    for marker in rules.featuring_markers:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered):
            cut = min(cut, m.start())
    return text[:cut]


def _strip_version_suffixes(text: str, rules: NormalizationRules) -> str:
    out = text
    for suffix in rules.version_suffixes:
        out = re.sub(rf"(?<![a-z0-9]){re.escape(suffix)}(?![a-z0-9])", " ", out,
                     flags=re.IGNORECASE)
    return out


def _strip_leading_article(text: str, rules: NormalizationRules) -> str:
    stripped = text.lstrip()
    lowered = stripped.lower()
    for article in rules.leading_articles:
        if lowered.startswith(article + " "):
            return stripped[len(article) + 1:]
    return text


def aggressive(text: str, rules: NormalizationRules) -> str:
    """The fallback stage: remove the version information the exact stage preserved.

    Applied only to listens that found no candidate at the exact stage. Order matters --
    bracketed segments go first so their contents cannot be partially matched afterwards.
    """
    if not text:
        return ""
    out = text
    if rules.strip_bracketed_segments:
        out = _BRACKETED.sub(" ", out)
    out = _strip_featuring(out, rules)
    out = _strip_version_suffixes(out, rules)
    if rules.strip_four_digit_years:
        out = _FOUR_DIGIT_YEAR.sub(" ", out)
    out = _WHITESPACE.sub(" ", out).strip().strip(_TRAILING_SEPARATORS)
    out = _strip_leading_article(out, rules)
    return fold(out, rules)


@lru_cache(maxsize=1)
def _default_rules() -> NormalizationRules:
    return load_rules()


def normalize(artist_name: str | None,
              recording_name: str | None,
              release_name: str | None = None,
              rules: NormalizationRules | None = None) -> NormalizedFields:
    """Normalize one listen's strings into an exact key and a fallback key.

    A row that yields no key is classified, not rejected: `MISSING_FIELD` when the input is
    absent or blank, `UNSUPPORTED_SCRIPT` when non-empty input folds away because we cannot
    romanise it. Both keep the row visible to downstream accounting.
    """
    r = rules or _default_rules()

    artist_raw = artist_name or ""
    recording_raw = recording_name or ""
    release_raw = release_name or ""

    artist_exact = fold(artist_raw, r)
    recording_exact = fold(recording_raw, r)
    release_exact = fold(release_raw, r)
    artist_fb = aggressive(artist_raw, r)
    recording_fb = aggressive(recording_raw, r)

    if not artist_raw.strip() or not recording_raw.strip():
        status, detail = Status.MISSING_FIELD, "artist_name or recording_name is empty"
    elif not artist_exact or not recording_exact:
        status = Status.UNSUPPORTED_SCRIPT
        which = "artist" if not artist_exact else "recording"
        detail = f"{which} folds to empty; needs transliteration we do not have"
    else:
        status, detail = Status.OK, ""

    # A key is only formed when both halves survive. Concatenating a present artist with an
    # empty recording would produce a key equal to the artist alone, which would collide
    # every unromanisable track by that artist into one bucket.
    exact_key = artist_exact + recording_exact if artist_exact and recording_exact else ""
    fallback_key = artist_fb + recording_fb if artist_fb and recording_fb else ""

    return NormalizedFields(
        artist_exact=artist_exact,
        recording_exact=recording_exact,
        release_exact=release_exact,
        exact_key=exact_key,
        artist_fallback=artist_fb,
        recording_fallback=recording_fb,
        fallback_key=fallback_key,
        status=status,
        status_detail=detail,
        normalization_version=r.version,
    )
