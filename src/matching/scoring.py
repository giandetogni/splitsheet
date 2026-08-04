"""Load config/scoring_rules.yml and apply it, in Python and as generated SQL.

Two properties this module exists to guarantee:

1. THE CONFIG CANNOT DRIFT FROM ITS VERSION. `rules_sha256` is recomputed from the semantic
   content on every load and compared with the recorded value. Editing a weight or a threshold
   without updating that line is a hard failure, not a silent behaviour change. The unit suite
   also pins the resulting version string, so an edit that DOES update the digest still has to
   be acknowledged in a test.

2. PYTHON AND SQL CANNOT DRIFT FROM EACH OTHER. The score expression and the decision policy
   are emitted from the same loaded config, so BigQuery runs the arithmetic the unit tests
   pinned. An integration test recomputes published rows in Python and asserts they agree.

The decision policy, in the order the branches are evaluated:

    top1 < applied threshold                  -> BELOW_THRESHOLD
    exactly one candidate                     -> accept (no margin exists to check)
    top1 - top2 <= tie_epsilon                -> AMBIGUOUS_TIE
    top1 - top2 <  minimum_score_margin       -> AMBIGUOUS_TIE
    otherwise                                 -> accept

Order is the policy, not an implementation detail: threshold first means a weak lone candidate
is refused for weakness rather than mislabelled a tie, and the uniqueness branch before the
margin branches means a single candidate can never be reported as a tie. Nothing anywhere
below breaks a tie by candidate order, row number or MBID value.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config/scoring_rules.yml"

# The features the config may name, and which of them are booleans that need a cast in SQL.
# Column names in silver_candidate_features are identical to the config's feature names.
FEATURE_COLUMNS = (
    "artist_unicode_exact", "recording_unicode_exact",
    "artist_token_similarity", "recording_token_similarity",
    "artist_string_similarity", "recording_string_similarity",
    "release_lower_exact",
)
BOOL_FEATURES = frozenset({"artist_unicode_exact", "recording_unicode_exact",
                           "release_lower_exact"})

ACCEPTED = "ACCEPTED"
AMBIGUOUS_TIE = "AMBIGUOUS_TIE"
BELOW_THRESHOLD = "BELOW_THRESHOLD"
NO_BLOCK_CANDIDATES = "NO_BLOCK_CANDIDATES"


@dataclass(frozen=True)
class ScoringRules:
    scoring_version: str
    feature_version: str
    weights: dict[str, float]
    exact_threshold: float
    fallback_threshold: float
    minimum_score_margin: float
    tie_epsilon: float
    rules_digest: str = field(compare=False)

    @property
    def version(self) -> str:
        """Semantic version plus content digest, so two runs under different rules are
        distinguishable even if someone forgot to bump the semantic part."""
        return f"{self.scoring_version}+{self.rules_digest}"

    def score(self, features: dict[str, float | bool | None]) -> float | None:
        """Weighted mean over the features that are available for this pair.

        A NULL feature leaves both the numerator and the denominator: absence of a comparison
        is not a failed comparison. Returns None when no feature at all could be compared,
        which the caller must treat as "not scoreable", never as zero.
        """
        num = den = 0.0
        for name, weight in self.weights.items():
            value = features.get(name)
            if value is None:
                continue
            num += weight * float(value)
            den += weight
        if den == 0:
            return None
        return num / den

    def applied_threshold(self, block_method: str) -> float:
        return self.exact_threshold if block_method == "EXACT" else self.fallback_threshold

    def decide(self, block_method: str, candidate_count: int,
               top1: float | None, top2: float | None) -> str:
        """One outcome per listen. See the module docstring for why the order is the policy."""
        if candidate_count == 0 or top1 is None:
            return NO_BLOCK_CANDIDATES
        if top1 < self.applied_threshold(block_method):
            return BELOW_THRESHOLD
        if candidate_count == 1 or top2 is None:
            return ACCEPTED
        margin = top1 - top2
        if margin <= self.tie_epsilon or margin < self.minimum_score_margin:
            return AMBIGUOUS_TIE
        return ACCEPTED

    # --- the same rules as SQL ------------------------------------------------------------

    def sql_score(self, alias: str = "") -> str:
        p = f"{alias}." if alias else ""
        used = [(n, self.weights[n]) for n in FEATURE_COLUMNS if self.weights.get(n, 0.0)]
        def expr(name: str) -> str:
            col = f"{p}{name}"
            return f"CAST({col} AS INT64)" if name in BOOL_FEATURES else col
        num = " + ".join(f"IF({p}{n} IS NULL, 0, {w} * {expr(n)})" for n, w in used)
        den = " + ".join(f"IF({p}{n} IS NULL, 0, {w})" for n, w in used)
        return f"SAFE_DIVIDE({num}, NULLIF({den}, 0))"

    def sql_applied_threshold(self, block_method: str = "block_method") -> str:
        return (f"IF({block_method} = 'EXACT', {self.exact_threshold}, "
                f"{self.fallback_threshold})")

    def sql_decision(self, block_method: str = "block_method",
                     candidate_count: str = "candidate_count",
                     top1: str = "top1", top2: str = "top2") -> str:
        return f"""CASE
          WHEN {candidate_count} = 0 OR {top1} IS NULL THEN '{NO_BLOCK_CANDIDATES}'
          WHEN {top1} < {self.sql_applied_threshold(block_method)} THEN '{BELOW_THRESHOLD}'
          WHEN {candidate_count} = 1 OR {top2} IS NULL THEN '{ACCEPTED}'
          WHEN ({top1} - {top2}) <= {self.tie_epsilon} THEN '{AMBIGUOUS_TIE}'
          WHEN ({top1} - {top2}) < {self.minimum_score_margin} THEN '{AMBIGUOUS_TIE}'
          ELSE '{ACCEPTED}' END"""


def _semantic_subset(raw: dict) -> dict:
    """Everything that changes behaviour, and nothing that does not.

    Comments, prose and the recorded digest are excluded; weights, thresholds, margins and the
    declared versions are in. Sorted keys and compact separators so the digest depends on
    content rather than on formatting.
    """
    return {
        "scoring_version": raw["scoring_version"],
        "feature_version": raw["feature_version"],
        "weights": {k: float(v["weight"]) for k, v in sorted(raw["features"].items())},
        "thresholds": {k: float(v) for k, v in sorted(raw["thresholds"].items())},
        "minimum_score_margin": float(raw["minimum_score_margin"]),
        "tie_epsilon": float(raw["tie_epsilon"]),
        "low_information_policy": {
            k: raw["low_information_policy"][k]
            for k in ("suppress_candidates", "score_on_full_unicode_text",
                      "use_ascii_key_as_score_evidence", "separate_threshold")},
    }


def compute_digest(raw: dict) -> str:
    payload = json.dumps(_semantic_subset(raw), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_scoring_rules(path: str | pathlib.Path | None = None,
                       require_digest: bool = True) -> ScoringRules:
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p) as fh:
        raw = yaml.safe_load(fh)

    digest = compute_digest(raw)
    recorded = str(raw.get("rules_sha256", ""))
    if require_digest and recorded != digest:
        raise ValueError(
            f"config/scoring_rules.yml content digest is {digest} but the file records "
            f"{recorded!r}. A weight, threshold or margin changed without the digest being "
            f"updated: refusing to score under a version that no longer describes the rules.")

    lip = raw["low_information_policy"]
    if lip["suppress_candidates"] or lip["use_ascii_key_as_score_evidence"]:
        raise ValueError("low-information keys may not be suppressed or scored on the ASCII "
                         "key; the preflight measured 100% unique agreement for them")
    weights = {k: float(v["weight"]) for k, v in raw["features"].items()}
    unknown = set(weights) - set(FEATURE_COLUMNS)
    if unknown:
        raise ValueError(f"config names features that do not exist in the feature table: "
                         f"{sorted(unknown)}")
    if raw["tie_epsilon"] > raw["minimum_score_margin"]:
        raise ValueError("tie_epsilon above minimum_score_margin would make the margin rule "
                         "unreachable")
    return ScoringRules(
        scoring_version=str(raw["scoring_version"]),
        feature_version=str(raw["feature_version"]),
        weights=weights,
        exact_threshold=float(raw["thresholds"]["exact"]),
        fallback_threshold=float(raw["thresholds"]["fallback"]),
        minimum_score_margin=float(raw["minimum_score_margin"]),
        tie_epsilon=float(raw["tie_epsilon"]),
        rules_digest=digest,
    )
