"""Load config/payout_policy.yml, seal it by digest, and emit the gate logic as SQL.

MATCHED != PAYABLE. This module is the boundary between a technical conclusion and a financial
decision, and it exists as its own versioned artifact so that changing what the pipeline is
willing to pay for never means touching the matcher.

Two properties, the same two the scoring config has:

  * the policy cannot drift from its version -- `rules_sha256` is recomputed on every load and a
    mismatch is a hard failure, so editing a gate without bumping the version stops the publisher;
  * the gates exist once and are emitted as SQL, so the warehouse applies the policy the unit
    tests pin rather than a second copy of it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config/payout_policy.yml"

ATTRIBUTABLE = "ATTRIBUTABLE"
UNMATCHED = "UNMATCHED"
MATCH_RISK_POLICY = "MATCH_RISK_POLICY"
DEFECTIVE_OWNERSHIP = "DEFECTIVE_OWNERSHIP"
RATE_CARD_GAP = "RATE_CARD_GAP"
RATE_CARD_AMBIGUOUS = "RATE_CARD_AMBIGUOUS"

RATE_RESOLVED = "RATE_RESOLVED"
RATE_GAP = "RATE_GAP"
RATE_AMBIGUOUS = "RATE_AMBIGUOUS"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class PayoutPolicy:
    policy_version: str
    match_run_id: str
    scoring_version: str
    rights_version: str
    rights_generation_run_id: str
    rule_version_id: str
    period_start: dt.date
    period_end: dt.date
    hold_methods: tuple[str, ...]
    expected_held_listens: int
    ownership_status_map: dict[str, str]
    ownership_payable_statuses: tuple[str, ...]
    attribution_states: tuple[str, ...]
    currency: str
    internal_scale: int
    published_scale: int
    tiebreak: str
    fact_grain: tuple[str, ...]
    rules_digest: str = field(compare=False)

    @property
    def version(self) -> str:
        """Semantic version plus content digest, so two publications under different policies are
        distinguishable even if the semantic part was not bumped."""
        return f"{self.policy_version}+{self.rules_digest}"

    # --- the gates, in Python ---------------------------------------------------------------

    def ownership_status(self, resolution_status: str | None) -> str:
        if resolution_status is None:
            return NOT_APPLICABLE
        if resolution_status not in self.ownership_status_map:
            # No UNKNOWN bucket: an unmapped status is a policy gap and must fail loudly rather
            # than quietly become unpayable or, worse, payable.
            raise ValueError(f"resolution_status {resolution_status!r} has no ownership mapping")
        return self.ownership_status_map[resolution_status]

    def rate_status(self, covering_rate_rows: int | None) -> str:
        if covering_rate_rows is None:
            return NOT_APPLICABLE
        if covering_rate_rows == 1:
            return RATE_RESOLVED
        return RATE_GAP if covering_rate_rows == 0 else RATE_AMBIGUOUS

    def disposition(
        self,
        match_status: str,
        match_method: str,
        resolution_status: str | None,
        covering_rate_rows: int | None,
    ) -> str:
        """One terminal state per listen. The order of these checks IS the policy."""
        if match_status != "MATCHED":
            return UNMATCHED
        if match_method in self.hold_methods:
            return MATCH_RISK_POLICY
        own = self.ownership_status(resolution_status)
        if own not in self.ownership_payable_statuses:
            return DEFECTIVE_OWNERSHIP
        rate = self.rate_status(covering_rate_rows)
        if rate == RATE_GAP:
            return RATE_CARD_GAP
        if rate == RATE_AMBIGUOUS:
            return RATE_CARD_AMBIGUOUS
        return ATTRIBUTABLE

    def is_payable(self, disposition: str) -> bool:
        return disposition == ATTRIBUTABLE

    # --- the same gates as SQL --------------------------------------------------------------

    def sql_hold_methods(self) -> str:
        return "(" + ", ".join(f"'{m}'" for m in self.hold_methods) + ")"

    def sql_ownership_status(self, resolution_col: str = "resolution_status") -> str:
        whens = "\n           ".join(
            f"WHEN '{k}' THEN '{v}'" for k, v in sorted(self.ownership_status_map.items())
        )
        # An unmapped status becomes NULL, and a validation refuses to publish when any row is
        # NULL here. Mapping it to a default would hide a policy gap.
        return f"CASE {resolution_col}\n           {whens}\n           ELSE NULL END"

    def sql_rate_status(self, rows_col: str = "covering_rate_rows") -> str:
        return (
            f"CASE WHEN {rows_col} IS NULL THEN '{NOT_APPLICABLE}' "
            f"WHEN {rows_col} = 1 THEN '{RATE_RESOLVED}' "
            f"WHEN {rows_col} = 0 THEN '{RATE_GAP}' "
            f"ELSE '{RATE_AMBIGUOUS}' END"
        )

    def sql_disposition(
        self,
        match_status: str = "match_status",
        match_method: str = "match_method",
        ownership_status: str = "ownership_status",
        rate_status: str = "rate_status",
    ) -> str:
        payable = ", ".join(f"'{s}'" for s in self.ownership_payable_statuses)
        return f"""CASE
          WHEN {match_status} != 'MATCHED' THEN '{UNMATCHED}'
          WHEN {match_method} IN {self.sql_hold_methods()} THEN '{MATCH_RISK_POLICY}'
          WHEN {ownership_status} NOT IN ({payable}) THEN '{DEFECTIVE_OWNERSHIP}'
          WHEN {rate_status} = '{RATE_GAP}' THEN '{RATE_CARD_GAP}'
          WHEN {rate_status} = '{RATE_AMBIGUOUS}' THEN '{RATE_CARD_AMBIGUOUS}'
          WHEN {rate_status} = '{RATE_RESOLVED}' THEN '{ATTRIBUTABLE}'
          ELSE NULL END"""


def _semantic_subset(raw: dict) -> dict:
    """Everything that changes a financial decision, and nothing that does not."""
    return {
        "payout_policy_version": raw["payout_policy_version"],
        "inputs": dict(sorted(raw["inputs"].items())),
        "match_risk": {
            k: raw["match_risk"][k] for k in ("hold_methods", "hold_reason", "keeps_match_status")
        },
        "ownership": {
            "terminal_state": raw["ownership"]["terminal_state"],
            "status_map": dict(sorted(raw["ownership"]["status_map"].items())),
            "payable_statuses": raw["ownership"]["payable_statuses"],
        },
        "rate": {
            k: raw["rate"][k]
            for k in ("imputation", "gap_state", "ambiguous_state", "payable_statuses")
        },
        "attribution_states": raw["attribution_states"],
        "payable_state": raw["payable_state"],
        "money": {
            "currency": raw["money"]["currency"],
            "internal_scale": raw["money"]["internal_scale"],
            "published_scale": raw["money"]["published_scale"],
            "rounding": dict(sorted(raw["money"]["rounding"].items())),
            "negative_payout": raw["money"]["negative_payout"],
        },
        "fact_grain": raw["fact_grain"],
        "publication": {
            k: raw["publication"][k] for k in ("strategy", "destructive_update", "statuses")
        },
    }


def compute_digest(raw: dict) -> str:
    payload = json.dumps(_semantic_subset(raw), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_payout_policy(
    path: str | pathlib.Path | None = None, require_digest: bool = True
) -> PayoutPolicy:
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p) as fh:
        raw = yaml.safe_load(fh)

    digest = compute_digest(raw)
    recorded = str(raw.get("rules_sha256", ""))
    if require_digest and recorded != digest:
        raise ValueError(
            f"config/payout_policy.yml content digest is {digest} but the file records "
            f"{recorded!r}. A gate, a rounding rule or an input binding changed without the "
            f"digest being updated: refusing to publish money under a version that no longer "
            f"describes the policy."
        )

    if raw["rate"]["imputation"] != "FORBIDDEN":
        raise ValueError(
            "rate imputation must be FORBIDDEN: a rate invented for an uncovered "
            "day is invented money"
        )
    if raw["money"]["negative_payout"] != "FORBIDDEN":
        raise ValueError("negative payouts must be FORBIDDEN")
    if not raw["money"]["rounding"]["single_rounding_point"]:
        raise ValueError("rounding each holder independently does not close in cents")
    if raw["money"]["rounding"]["method"] != "largest_remainder":
        raise ValueError(f"unsupported rounding method: {raw['money']['rounding']['method']}")
    if raw["publication"]["destructive_update"] != "FORBIDDEN":
        raise ValueError("a completed financial publication may not be updated destructively")
    if "UNKNOWN" in raw["attribution_states"]:
        raise ValueError("UNKNOWN is not a permitted terminal state")

    inputs = raw["inputs"]
    return PayoutPolicy(
        policy_version=str(raw["payout_policy_version"]),
        match_run_id=inputs["match_run_id"],
        scoring_version=inputs["scoring_version"],
        rights_version=inputs["rights_version"],
        rights_generation_run_id=inputs["rights_generation_run_id"],
        rule_version_id=inputs["rule_version_id"],
        period_start=dt.date.fromisoformat(str(inputs["period_start"])),
        period_end=dt.date.fromisoformat(str(inputs["period_end"])),
        hold_methods=tuple(raw["match_risk"]["hold_methods"]),
        expected_held_listens=int(raw["match_risk"]["expected_held_listens"]),
        ownership_status_map=dict(raw["ownership"]["status_map"]),
        ownership_payable_statuses=tuple(raw["ownership"]["payable_statuses"]),
        attribution_states=tuple(raw["attribution_states"]),
        currency=raw["money"]["currency"],
        internal_scale=int(raw["money"]["internal_scale"]),
        published_scale=int(raw["money"]["published_scale"]),
        tiebreak=raw["money"]["rounding"]["tiebreak"],
        fact_grain=tuple(raw["fact_grain"]),
        rules_digest=digest,
    )


def attribution_run_id(policy: PayoutPolicy, label: str = "PUBLISHED") -> str:
    """Deterministic from every input that could change a published amount, plus the publication
    label. No wall-clock: the same inputs must produce the same run id, which is what makes a
    re-publication detectable as a re-run instead of appending a second copy."""
    payload = "|".join(
        [
            policy.version,
            policy.match_run_id,
            policy.scoring_version,
            policy.rights_version,
            policy.rights_generation_run_id,
            policy.rule_version_id,
            str(policy.period_start),
            str(policy.period_end),
            label,
        ]
    )
    return "attr:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
