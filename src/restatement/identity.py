"""Canonical identity for a restatement run. Deterministic, structured, and cohort-aware.

THE DEFECT THIS REPLACES. Phase 6 derived `restatement_run_id` from the versions alone:

    sha256(normalization_version | scoring_version | match_run_id | blocking_version)

which means the identity did not include WHICH RECORDS were restated. Two runs over completely
different cohorts collided, and that was not hypothetical -- during Phase 6 the rejected cohort of
1,377,862 listens and the published cohort of 982,322 both produced `restate:6b3923771883e860`. An
identifier that cannot distinguish those two runs cannot support an audit trail.

WHAT AN IDENTITY MUST COVER. Everything that could change which rows the restatement produces:

    period                     the window being restated
    prior_publication_id       what it is being restated FROM
    prior_normalization_version / new_normalization_version
    scoring_version            frozen, but part of the identity: a run under a different scorer is
                               a different run even if nothing else moved
    payout_policy_version      likewise for the financial gates
    rights_version             likewise for ownership and rates
    rule_version_id            the rate rule
    trigger_reason             why the restatement exists
    canonical_snapshot_date    which MusicBrainz snapshot was matched against
    cohort_digest              WHICH RECORDS -- the part that was missing

COHORT IDENTITY IS A STRUCTURED PREDICATE, NEVER PROSE OR SQL. Prose is not comparable: two
sentences can describe the same set, and one sentence can be edited without changing what it
selects. SQL is not an identity either: whitespace, aliases and formatting change the text without
changing the rows, so hashing it would make identity depend on how someone typed a query. So a
cohort is a small closed-vocabulary predicate in config/restatement_cohorts.yml, hashed over its
canonical form.

CANONICAL FORM. JSON with sorted keys, no whitespace, and every list sorted -- the lists here are
sets semantically (failure reasons, scripts, fields), so their order carries no meaning and must not
carry identity either. Human fields are excluded by construction: the hash is computed from a fixed
field list, so a note, a status or a measured count cannot reach it.

THE LEGACY IDENTIFIER IS KEPT, NOT REWRITTEN. `LEGACY_RUN_ID` records what the published rows
actually carry, why it is insufficient, and which canonical identity corresponds to it. Rewriting
5.9M published rows to carry a nicer id would violate the immutability the whole phase exists to
demonstrate.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass, field

DEFAULT_COHORT_CONFIG = pathlib.Path(__file__).parents[2] / "config/restatement_cohorts.yml"

#: Identity scheme version. It is IN the id string (`restate:v1:<hex>`) so an identifier says which
#: scheme produced it, and a future scheme cannot be mistaken for this one.
IDENTITY_SCHEME = "v1"
RUN_ID_PREFIX = "restate"
RUN_ID_HEX_CHARS = 16

#: The predicate vocabulary. A cohort naming anything outside this fails to load, which is what
#: keeps "structured" from decaying into "a dict someone put anything in".
PREDICATE_FIELDS = (
    "period_start",
    "period_end",
    "prior_failure_reasons",
    "string_fields_examined",
    "required_scripts_any",
)
SET_VALUED_PREDICATE_FIELDS = (
    "prior_failure_reasons",
    "string_fields_examined",
    "required_scripts_any",
)
KNOWN_SCRIPTS = ("hangul", "kana")

#: How each named script is detected in SQL. Kept next to the vocabulary so the predicate, the
#: reprocessing queries and the canonical-index query cannot disagree about what "hangul" means.
#: These are detection patterns, NOT identity: the hash is over the script NAMES, so improving a
#: pattern is a code change to review on its merits rather than a silent new run id.
SCRIPT_PATTERNS = {
    "hangul": r"[\x{AC00}-\x{D7A3}\x{1100}-\x{11FF}\x{3130}-\x{318F}]",
    "kana": r"[\x{3040}-\x{30FF}]",
}

#: Fields deliberately NOT hashed. Present so the exclusion is a declaration rather than an
#: accident of which keys the code happened to read.
COHORT_FIELDS_EXCLUDED_FROM_HASH = (
    "status",
    "measured_listens",
    "measured_distinct_pairs",
    "notes",
    "cohort_sha256",
    "cohort_key",
    "mart_run_id",
    "rows_in_delta_mart",
    "recorded_delta",
)

#: The canonical inputs, in the order they are documented. Hashing reads this tuple, so adding an
#: input is a visible change to identity rather than a silent one.
CANONICAL_INPUT_FIELDS = (
    "period_start",
    "period_end",
    "prior_publication_id",
    "prior_normalization_version",
    "new_normalization_version",
    "scoring_version",
    "payout_policy_version",
    "rights_version",
    "rule_version_id",
    "trigger_reason",
    "canonical_snapshot_date",
    "cohort_digest",
)


def _canonical_json(payload: dict) -> str:
    """Sorted keys, compact separators, ASCII-escaped. Formatting cannot reach the hash."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


@dataclass(frozen=True)
class CohortDefinition:
    """A restatement cohort as a hashable predicate."""

    cohort_key: str
    period_start: str
    period_end: str
    prior_failure_reasons: tuple[str, ...]
    string_fields_examined: tuple[str, ...]
    required_scripts_any: tuple[str, ...]
    status: str = field(default="", compare=False)
    measured_listens: int = field(default=0, compare=False)
    measured_distinct_pairs: int = field(default=0, compare=False)
    notes: str = field(default="", compare=False)
    #: What the run wrote into the delta mart, if it ever ran. Empty for a cohort that was built
    #: and rejected before publication -- a run that produced no rows has no mart identifier.
    mart_run_id: str = field(default="", compare=False)
    rows_in_delta_mart: int = field(default=0, compare=False)
    recorded_delta: str = field(default="", compare=False)

    def canonical_predicate(self) -> dict:
        """Only the predicate, with set-valued fields sorted so order cannot carry identity."""
        return {
            "period_start": str(self.period_start),
            "period_end": str(self.period_end),
            "prior_failure_reasons": sorted(self.prior_failure_reasons),
            "string_fields_examined": sorted(self.string_fields_examined),
            "required_scripts_any": sorted(self.required_scripts_any),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical_predicate()).encode()).hexdigest()

    def sql_predicate(
        self,
        normalized_alias: str = "n",
        matches_alias: str = "m",
        script_patterns: dict[str, str] | None = None,
    ) -> str:
        """SQL GENERATED FROM the predicate -- never the source of its identity.

        The direction matters: the structured predicate is the identity and the SQL is derived from
        it, so reformatting the query cannot change what run this is, and changing the predicate
        cannot leave the query behind.
        """
        patterns = script_patterns or SCRIPT_PATTERNS
        script_tests = [
            f"REGEXP_CONTAINS({normalized_alias}.{f}, r'{patterns[s]}')"
            for s in sorted(self.required_scripts_any)
            for f in sorted(self.string_fields_examined)
        ]
        clauses = [f"({' OR '.join(script_tests)})"]
        if self.prior_failure_reasons:
            reasons = ", ".join(f"'{r}'" for r in sorted(self.prior_failure_reasons))
            clauses.append(f"{matches_alias}.failure_reason IN ({reasons})")
        return " AND ".join(clauses)


@dataclass(frozen=True)
class RestatementInputs:
    """The canonical inputs of a restatement run. `run_id` is a pure function of these."""

    period_start: str
    period_end: str
    prior_publication_id: str
    prior_normalization_version: str
    new_normalization_version: str
    scoring_version: str
    payout_policy_version: str
    rights_version: str
    rule_version_id: str
    trigger_reason: str
    canonical_snapshot_date: str
    cohort_digest: str

    def canonical_payload(self) -> dict:
        payload = asdict(self)
        missing = [f for f in CANONICAL_INPUT_FIELDS if not payload.get(f)]
        if missing:
            raise ValueError(f"canonical restatement inputs are incomplete: {missing}")
        unexpected = set(payload) - set(CANONICAL_INPUT_FIELDS)
        if unexpected:
            raise ValueError(f"unhashed fields present in the inputs: {sorted(unexpected)}")
        return {
            "identity_scheme": IDENTITY_SCHEME,
            **{f: str(payload[f]) for f in CANONICAL_INPUT_FIELDS},
        }

    @property
    def inputs_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical_payload()).encode()).hexdigest()

    @property
    def run_id(self) -> str:
        return f"{RUN_ID_PREFIX}:{IDENTITY_SCHEME}:{self.inputs_digest[:RUN_ID_HEX_CHARS]}"


# --- the historical identifier, preserved -------------------------------------------------

#: The inputs the legacy formula actually hashed, as the Phase 6 run used them. Kept so the
#: collision can be REPRODUCED rather than merely claimed: feed either cohort through
#: `legacy_run_id()` and the same string comes out, because no cohort term is present.
LEGACY_INPUTS = {
    "new_normalization_version": "1.1.0+b3253b155934",
    "scoring_version": "1.0.0+cb21f9704ff0",
    "prior_match_run_id": "match:101eef5c5b5c081e",
    "blocking_version": "staged-1.1.0",
}


def legacy_run_id(
    new_normalization_version: str,
    scoring_version: str,
    prior_match_run_id: str,
    blocking_version: str,
) -> str:
    """The superseded formula, kept executable so its defect is demonstrable.

    Note what is absent from the signature: the period, the prior publication, the payout policy,
    the rights version and -- the one that matters -- the cohort. Any two runs agreeing on these
    four strings are indistinguishable under this scheme no matter which rows they touched.
    """
    payload = (
        f"{new_normalization_version}|{scoring_version}|{prior_match_run_id}|{blocking_version}"
    )
    return "restate:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LegacyRunId:
    run_id: str
    scheme: str
    hashed_inputs: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    why_insufficient: str
    published_rows_carry_it: bool

    @property
    def canonical_equivalent(self) -> str:
        """The canonical identity of the run whose rows carry this legacy id.

        A mapping, not a replacement: the published rows keep the legacy string. Rewriting 5.9M
        rows to carry a better identifier would break the immutability this phase exists to prove,
        so the correction is additive -- the registry resolves one to the other.
        """
        return canonical_id_for_published_run()


LEGACY_RUN_ID = LegacyRunId(
    run_id="restate:6b3923771883e860",
    scheme="legacy-unversioned",
    hashed_inputs=(
        "new_normalization_version",
        "scoring_version",
        "prior_match_run_id",
        "blocking_version",
    ),
    missing_inputs=(
        "period",
        "prior_publication_id",
        "prior_normalization_version",
        "payout_policy_version",
        "rights_version",
        "rule_version_id",
        "trigger_reason",
        "canonical_snapshot_date",
        "cohort_definition",
    ),
    why_insufficient=(
        "the cohort was not part of the hash, so the rejected 1,377,862-listen cohort and the "
        "published 982,322-listen cohort produced the SAME identifier under the same versions"
    ),
    published_rows_carry_it=True,
)


@dataclass(frozen=True)
class LegacyRun:
    """A run present in the delta mart that is NOT a cohort restatement.

    It exists so the registry can explain every identifier the warehouse actually contains. The
    fields that were never recorded stay empty rather than being reconstructed: a rehearsal that
    compared a publication with itself had no cohort and no canonical inputs, and inventing them
    would make the registry a story instead of a record.
    """

    restatement_run_id: str
    run_type: str
    is_legacy: bool
    is_financially_effective: bool
    recorded_delta: str
    rows_in_delta_mart: int
    canonical_inputs_available: bool
    provenance: str


def load_legacy_runs(path: str | pathlib.Path | None = None) -> list[LegacyRun]:
    """Load the declared non-cohort runs. Absent section means none are declared."""
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_COHORT_CONFIG
    raw = yaml.safe_load(p.read_text()) or {}
    out = []
    for entry in raw.get("legacy_runs", []) or []:
        if entry.get("canonical_inputs_available"):
            raise ValueError(
                f"{entry['restatement_run_id']!r} claims canonical inputs are available; a run with "
                f"canonical inputs belongs in the cohort registry, not among the legacy runs"
            )
        out.append(
            LegacyRun(
                restatement_run_id=entry["restatement_run_id"],
                run_type=entry["run_type"],
                is_legacy=bool(entry["is_legacy"]),
                is_financially_effective=bool(entry["is_financially_effective"]),
                recorded_delta=str(entry["recorded_delta"]),
                rows_in_delta_mart=int(entry["rows_in_delta_mart"]),
                canonical_inputs_available=False,
                provenance=" ".join(str(entry["provenance"]).split()),
            )
        )
    return out


def load_cohorts(path: str | pathlib.Path | None = None) -> dict[str, CohortDefinition]:
    """Load the cohort registry and verify each recorded digest against the recomputed one."""
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_COHORT_CONFIG
    with open(p) as fh:
        raw = yaml.safe_load(fh)

    out: dict[str, CohortDefinition] = {}
    for entry in raw["cohorts"]:
        predicate = entry["predicate"]
        unknown = set(predicate) - set(PREDICATE_FIELDS)
        if unknown:
            raise ValueError(
                f"cohort {entry['cohort_key']!r} uses fields outside the predicate "
                f"vocabulary: {sorted(unknown)}"
            )
        bad_scripts = set(predicate["required_scripts_any"]) - set(KNOWN_SCRIPTS)
        if bad_scripts:
            raise ValueError(
                f"cohort {entry['cohort_key']!r} names scripts with no algorithmic "
                f"transliteration: {sorted(bad_scripts)}"
            )
        cohort = CohortDefinition(
            cohort_key=entry["cohort_key"],
            period_start=str(predicate["period_start"]),
            period_end=str(predicate["period_end"]),
            prior_failure_reasons=tuple(predicate["prior_failure_reasons"]),
            string_fields_examined=tuple(predicate["string_fields_examined"]),
            required_scripts_any=tuple(predicate["required_scripts_any"]),
            status=entry.get("status", ""),
            measured_listens=int(entry.get("measured_listens") or 0),
            measured_distinct_pairs=int(entry.get("measured_distinct_pairs") or 0),
            notes=entry.get("notes", ""),
            mart_run_id=entry.get("mart_run_id", ""),
            rows_in_delta_mart=int(entry.get("rows_in_delta_mart") or 0),
            recorded_delta=str(entry.get("recorded_delta", "")),
        )
        recorded = str(entry.get("cohort_sha256", ""))
        if recorded not in ("", "PLACEHOLDER") and recorded != cohort.digest:
            raise ValueError(
                f"cohort {cohort.cohort_key!r} records digest {recorded} but its predicate hashes "
                f"to {cohort.digest}. The predicate changed without the digest being updated: "
                f"refusing to identify a restatement by a stale cohort id."
            )
        out[cohort.cohort_key] = cohort
    return out


PUBLISHED_COHORT_KEY = "partial-empty-hangul-kana"
REJECTED_COHORT_KEY = "script-only-no-failure-reason-restriction"


def inputs_for_published_restatement(
    cohorts: dict[str, CohortDefinition] | None = None, cohort_key: str = PUBLISHED_COHORT_KEY
) -> RestatementInputs:
    """The canonical inputs of the restatement that produced pub:v2.

    Values come from the frozen manifests, not from the warehouse: identity must be derivable from
    configuration alone, or it could not be recomputed after the fact.
    """
    registry = cohorts if cohorts is not None else load_cohorts()
    cohort = registry[cohort_key]
    return RestatementInputs(
        period_start="2026-06-01",
        period_end="2026-07-01",
        prior_publication_id="pub:v1",
        prior_normalization_version="1.0.0+0bc0dd643e06",
        new_normalization_version="1.1.0+b3253b155934",
        scoring_version="1.0.0+cb21f9704ff0",
        payout_policy_version="1.0.0+84b4b68a37c9",
        rights_version="1.0.0+47f801102e17",
        rule_version_id="rc-1.0.0",
        trigger_reason="TRANSLITERATION_KOREAN_JAPANESE_TITLES",
        canonical_snapshot_date="2026-07-17",
        cohort_digest=cohort.digest,
    )


def canonical_id_for_published_run() -> str:
    return inputs_for_published_restatement().run_id
