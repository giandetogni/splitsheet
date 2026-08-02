"""Load and version the normalization rules.

The version is not a hand-maintained string. It is the declared semantic version plus a
hash of the effective rules, so editing config/normalization_rules.yml changes
`normalization_version` whether or not anyone remembers to bump it. That property is what
makes a rule change traceable, and it is what a restatement would key off: a payout
computed under one normalization version can be told apart from one computed under another.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field

DEFAULT_RULES_PATH = pathlib.Path(__file__).parents[2] / "config/normalization_rules.yml"


@dataclass(frozen=True)
class NormalizationRules:
    """Immutable, fully resolved rule set. Frozen so no caller can mutate shared rules."""

    semantic_version: str
    unicode_form: str
    strip_diacritics: bool
    lowercase: bool
    strip_bracketed_segments: bool
    year_structures: tuple[re.Pattern, ...]
    featuring_markers: tuple[str, ...]
    leading_articles: tuple[str, ...]
    version_suffixes: tuple[str, ...]
    rules_digest: str = field(compare=False)

    @property
    def version(self) -> str:
        """e.g. '1.0.0+a1b2c3d4e5f6'. Changes if any rule changes."""
        return f"{self.semantic_version}+{self.rules_digest}"


def _canonical(payload: dict) -> str:
    """Stable serialisation, so the digest depends on rule content and not on key order,
    indentation or comments."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def load_rules(path: str | pathlib.Path | None = None) -> NormalizationRules:
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_RULES_PATH
    with open(p) as fh:
        raw = yaml.safe_load(fh)

    common = raw["common"]
    fallback = raw["fallback"]

    if common["keep"] != "ascii_alphanumeric":
        raise ValueError(f"unsupported keep policy: {common['keep']!r}")
    if common["unicode_form"] not in {"NFKD", "NFKC", "NFD", "NFC"}:
        raise ValueError(f"unsupported unicode_form: {common['unicode_form']!r}")

    effective = {
        "version": raw["version"],
        "common": {k: common[k] for k in
                   ("unicode_form", "strip_diacritics", "lowercase", "keep")},
        "fallback": {
            "strip_bracketed_segments": fallback["strip_bracketed_segments"],
            "year_structures": sorted(fallback["year_structures"]),
            # Sorted so a reordering of the YAML lists is not treated as a rule change,
            # while adding or removing an entry is.
            "featuring_markers": sorted(fallback["featuring_markers"]),
            "leading_articles": sorted(fallback["leading_articles"]),
            "version_suffixes": sorted(fallback["version_suffixes"]),
        },
    }
    digest = hashlib.sha256(_canonical(effective).encode()).hexdigest()[:12]


    return NormalizationRules(
        semantic_version=str(raw["version"]),
        unicode_form=common["unicode_form"],
        strip_diacritics=bool(common["strip_diacritics"]),
        lowercase=bool(common["lowercase"]),
        strip_bracketed_segments=bool(fallback["strip_bracketed_segments"]),
        # Compiled once at load. Longest pattern first so the most specific structure
        # ("2017 digital remastered version") is consumed before a shorter prefix of it.
        year_structures=tuple(
            re.compile(pat, re.IGNORECASE)
            for pat in sorted(fallback["year_structures"], key=len, reverse=True)
        ),
        # Longest first, so "feat." is consumed before "feat" and "deluxe edition" before
        # "deluxe". Matching the shorter form first would leave debris behind.
        featuring_markers=tuple(sorted(fallback["featuring_markers"], key=len, reverse=True)),
        leading_articles=tuple(fallback["leading_articles"]),
        version_suffixes=tuple(sorted(fallback["version_suffixes"], key=len, reverse=True)),
        rules_digest=digest,
    )
