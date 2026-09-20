"""Deterministic generator for MODELED rights data. Pure: no GCP, no IO, no clock.

    rights holders    MODELED
    ownership splits  MODELED
    rate cards        MODELED
    royalty amounts   ILLUSTRATIVE MODELED AMOUNTS, never observed industry payouts

The real data in this project is ListenBrainz listens and MusicBrainz recordings. Nothing in
this module represents a real rights holder, a real agreement, a real share or a real rate, and
no generated string can coincide with a real organisation: display names are
"Modeled Rights Holder NNNNNN" by construction.

EVERY VALUE IS A HASH OF (seed, kind, key). No RNG object, no wall-clock, no read order, so:

  * the same seed over the same recording universe reproduces the same rows byte for byte;
  * `holder_id` and `split_version_id` are stable across runs and across machines;
  * two recordings' ownership can be generated independently and in any order, which is what
    lets the build stream 2.5M recordings without holding state.

TEMPORAL OWNERSHIP MODELING WITH VALIDITY INTERVALS, half-open [valid_from, valid_to). This is
deliberately NOT called SCD Type 2: nothing here detects a change and closes a row. The
intervals come straight out of the generator. SCD Type 2 is demonstrated separately, by a real
dbt snapshot over rights holders.

Shares are Decimal, never float. A share is money's denominator; 0.1 + 0.2 != 0.3 is not an
acceptable property for it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import pathlib
from dataclasses import dataclass, field
from decimal import Decimal

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config/rights_model.yml"

HUNDRED = Decimal("100.0000")

# Defect classes, in the fixed order they claim recordings. The order is part of the contract:
# it makes the assignment reproducible when two classes would otherwise compete for the same
# hash rank.
DEFECT_ORDER = (
    "shares_do_not_sum_to_100",
    "temporal_overlap",
    "temporal_gap",
    "invalid_interval",
    "missing_rights_holder",
)
# orphan_recording_mbid does not consume a real recording: it invents MBIDs that are not in the
# universe at all, so it is handled separately from the rank-based assignment above.


@dataclass(frozen=True)
class RightsModel:
    seed: str
    rights_version: str
    rule_version_id: str
    universe_match_run_id: str
    expected_recordings: int
    holder_id_prefix: str
    holder_id_digits: int
    split_version_id_prefix: str
    digest_chars: int
    holder_count: int
    assignment_power: float
    holder_types: tuple[str, ...]
    holder_type_weights: tuple[float, ...]
    payee_statuses: tuple[str, ...]
    payee_status_weights: tuple[float, ...]
    reserved_without_split: int
    holders_per_recording: tuple[int, ...]
    holders_per_recording_weights: tuple[float, ...]
    share_decimal_places: int
    minimum_share_pct: Decimal
    base_valid_from: dt.date
    open_ended_valid_to: dt.date
    change_date: dt.date
    change_share_of_recordings: float
    defect_counts: dict[str, int]
    overlap_start: dt.date
    overlap_end: dt.date
    gap_start: dt.date
    gap_end: dt.date
    invalid_from: dt.date
    invalid_to: dt.date
    share_sum_tolerance: Decimal
    rate_model_scope: str
    rate_currency: str
    rate_intervals: tuple[dict, ...]
    expected_rate_gap_days: int
    rules_digest: str = field(compare=False)

    @property
    def version(self) -> str:
        return f"{self.rights_version}+{self.rules_digest}"

    @property
    def assignable_holders(self) -> int:
        """Holder ranks the assignment may use. The reserved tail is excluded, which is what
        makes "exactly N holders have no ownership" a fact rather than a hope."""
        return self.holder_count - self.reserved_without_split


# --- deterministic primitives -------------------------------------------------------------


def digest_int(seed: str, *parts: object) -> int:
    payload = "\x1f".join([seed, *(str(p) for p in parts)])
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def unit(seed: str, *parts: object) -> float:
    """Uniform in [0, 1). 53 bits, so it is exactly representable as a float."""
    return (digest_int(seed, *parts) >> 11) / float(1 << 53)


def weighted_choice(values: tuple, weights: tuple[float, ...], u: float):
    acc = 0.0
    total = sum(weights)
    for value, weight in zip(values, weights, strict=True):
        acc += weight / total
        if u < acc:
            return value
    return values[-1]


# --- holders -------------------------------------------------------------------------------


def holder_id(model: RightsModel, index: int) -> str:
    return f"{model.holder_id_prefix}{index:0{model.holder_id_digits}d}"


def holder_row(model: RightsModel, index: int) -> dict:
    """One MODELED holder. The display name cannot collide with a real organisation."""
    return {
        "holder_id": holder_id(model, index),
        "display_name": f"Modeled Rights Holder {index:06d}",
        "holder_type": weighted_choice(
            model.holder_types, model.holder_type_weights, unit(model.seed, "holder_type", index)
        ),
        "payee_status": weighted_choice(
            model.payee_statuses,
            model.payee_status_weights,
            unit(model.seed, "payee_status", index),
        ),
        "model_scope": model.rate_model_scope,
        "is_modeled": True,
    }


def assign_holder_index(model: RightsModel, recording_mbid: str, slot: int) -> int:
    """Skewed assignment: rank = floor(assignable * u ** power).

    The power concentrates draws on low ranks, which is the shape catalogue ownership has. It is
    a formula rather than a table so that the choice is reproducible and inspectable.
    """
    u = unit(model.seed, "holder_slot", recording_mbid, slot)
    return int(model.assignable_holders * (u**model.assignment_power))


def holders_for(model: RightsModel, recording_mbid: str, count: int, salt: str = "") -> list[int]:
    """`count` DISTINCT holder indexes. Collisions are resolved by walking forward, which keeps
    the result deterministic without rejection sampling."""
    chosen: list[int] = []
    slot = 0
    while len(chosen) < count:
        index = assign_holder_index(model, f"{recording_mbid}{salt}", slot)
        while index in chosen:
            index = (index + 1) % model.assignable_holders
        chosen.append(index)
        slot += 1
    return chosen


# --- shares --------------------------------------------------------------------------------


def shares_for(
    model: RightsModel, recording_mbid: str, count: int, salt: str = ""
) -> list[Decimal]:
    """`count` shares summing to EXACTLY 100.0000.

    Weights come from the hash, are scaled to 100 at the configured precision, and the residue
    from rounding is added to the last share. Decimal throughout: the sum has to be exact, and
    a float sum of four shares is not.
    """
    quantum = Decimal(1).scaleb(-model.share_decimal_places)
    weights = [
        Decimal(str(unit(model.seed, "share", recording_mbid, salt, i) + 0.05))
        for i in range(count)
    ]
    total = sum(weights)
    minimum = model.minimum_share_pct
    parts = [max(minimum, (HUNDRED * w / total).quantize(quantum)) for w in weights[:-1]]
    last = HUNDRED - sum(parts)
    if last < minimum:
        # Rebalance rather than emit a share below the floor: take the shortfall off the largest
        # part, which keeps the sum exact and every share above the minimum.
        shortfall = minimum - last
        biggest = parts.index(max(parts))
        parts[biggest] -= shortfall
        last = minimum
    return [*parts, last.quantize(quantum)]


# --- defect assignment ---------------------------------------------------------------------


def defect_rank(model: RightsModel, recording_mbid: str) -> int:
    return digest_int(model.seed, "defect_rank", recording_mbid)


def select_defects(model: RightsModel, recording_mbids: list[str]) -> dict[str, str]:
    """Assign each defect class its exact quota of recordings, disjointly.

    Ranking by hash rather than by position means the selection does not depend on the order the
    recordings arrived in, which is the same property the evaluation split relies on.
    """
    ordered = sorted(recording_mbids, key=lambda m: defect_rank(model, m))
    out: dict[str, str] = {}
    cursor = 0
    for kind in DEFECT_ORDER:
        quota = model.defect_counts[kind]
        for mbid in ordered[cursor : cursor + quota]:
            out[mbid] = kind
        cursor += quota
    return out


def has_mid_period_change(model: RightsModel, recording_mbid: str) -> bool:
    return unit(model.seed, "mid_change", recording_mbid) < model.change_share_of_recordings


def split_version_id(model: RightsModel, recording_mbid: str, valid_from: dt.date) -> str:
    """Identifies one split SET: a recording plus the date its ownership took effect.

    A healthy recording-date therefore resolves to exactly one split_version_id, and an
    ownership change produces two distinct ids rather than a mutated row.
    """
    d = hashlib.sha256(
        f"{model.seed}\x1f{model.rights_version}\x1f{recording_mbid}\x1f{valid_from}".encode()
    ).hexdigest()[: model.digest_chars]
    return f"{model.split_version_id_prefix}{d}"


# --- ownership rows ------------------------------------------------------------------------


def _set_rows(
    model: RightsModel,
    recording_mbid: str,
    valid_from: dt.date,
    valid_to: dt.date,
    salt: str,
    *,
    break_sum: bool = False,
    break_holder: bool = False,
) -> list[dict]:
    count = weighted_choice(
        model.holders_per_recording,
        model.holders_per_recording_weights,
        unit(model.seed, "holder_count", recording_mbid, salt),
    )
    indexes = holders_for(model, recording_mbid, count, salt)
    shares = shares_for(model, recording_mbid, count, salt)
    if break_sum:
        # DEFECT: remove a chunk from the first share so the set sums to less than 100. Left in
        # the source on purpose; the quality models have to find it.
        shares[0] = (shares[0] - Decimal("7.5000")).max(Decimal("0.5000"))
    svid = split_version_id(model, recording_mbid, valid_from)
    rows = []
    for slot, (index, share) in enumerate(zip(indexes, shares, strict=True)):
        hid = holder_id(model, index)
        if break_holder and slot == 0:
            # DEFECT: reference a holder id that is not in rights_holders at all.
            hid = f"{model.holder_id_prefix}{'9' * model.holder_id_digits}"
        rows.append(
            {
                "recording_mbid": recording_mbid,
                "rights_holder_id": hid,
                "share_pct": share,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "split_version_id": svid,
            }
        )
    return rows


def ownership_rows(
    model: RightsModel, recording_mbid: str, defect: str | None = None
) -> list[dict]:
    """Every ownership row for one recording, healthy or deliberately defective."""
    base, open_end = model.base_valid_from, model.open_ended_valid_to

    if defect == "temporal_overlap":
        return _set_rows(model, recording_mbid, base, model.overlap_end, "a") + _set_rows(
            model, recording_mbid, model.overlap_start, open_end, "b"
        )
    if defect == "temporal_gap":
        return _set_rows(model, recording_mbid, base, model.gap_start, "a") + _set_rows(
            model, recording_mbid, model.gap_end, open_end, "b"
        )
    if defect == "invalid_interval":
        return _set_rows(model, recording_mbid, model.invalid_from, model.invalid_to, "a")
    if defect == "shares_do_not_sum_to_100":
        return _set_rows(model, recording_mbid, base, open_end, "a", break_sum=True)
    if defect == "missing_rights_holder":
        return _set_rows(model, recording_mbid, base, open_end, "a", break_holder=True)

    if has_mid_period_change(model, recording_mbid):
        # Healthy, and contiguous across the change: [base, change) then [change, open).
        return _set_rows(model, recording_mbid, base, model.change_date, "a") + _set_rows(
            model, recording_mbid, model.change_date, open_end, "b"
        )
    return _set_rows(model, recording_mbid, base, open_end, "a")


def orphan_recording_mbid(model: RightsModel, index: int) -> str:
    """A syntactically valid MBID that is deliberately NOT in the catalogue.

    Version nibble 8 keeps it clearly outside MusicBrainz's version-4 UUID space, so an orphan
    can never accidentally be a real recording.
    """
    d = hashlib.sha256(f"{model.seed}\x1forphan\x1f{index}".encode()).hexdigest()
    return f"{d[:8]}-{d[8:12]}-8{d[13:16]}-{d[16:20]}-{d[20:32]}"


def orphan_rows(model: RightsModel) -> list[dict]:
    rows = []
    for i in range(model.defect_counts["orphan_recording_mbid"]):
        rows.extend(ownership_rows(model, orphan_recording_mbid(model, i)))
    return rows


# --- rate card -----------------------------------------------------------------------------


def rate_card_rows(model: RightsModel) -> list[dict]:
    out = []
    for i, interval in enumerate(model.rate_intervals):
        out.append(
            {
                "rate_card_id": f"rate-{model.rule_version_id}-{i:02d}",
                "model_scope": model.rate_model_scope,
                "valid_from": dt.date.fromisoformat(interval["valid_from"]),
                "valid_to": dt.date.fromisoformat(interval["valid_to"]),
                "rate_per_stream": Decimal(interval["rate_per_stream"]),
                "currency": model.rate_currency,
                "rule_version_id": model.rule_version_id,
                "is_modeled": True,
            }
        )
    return out


def rate_gap_days(model: RightsModel) -> list[dt.date]:
    """The days no rate card covers. Computed from the intervals, not hard-coded, so a config
    edit cannot leave the expected gap and the actual gap disagreeing."""
    rows = sorted(rate_card_rows(model), key=lambda r: r["valid_from"])
    gaps: list[dt.date] = []
    for earlier, later in itertools.pairwise(rows):
        day = earlier["valid_to"]
        while day < later["valid_from"]:
            gaps.append(day)
            day += dt.timedelta(days=1)
    return gaps


# --- interval semantics, in one place ------------------------------------------------------


def covers(valid_from: dt.date, valid_to: dt.date, on: dt.date) -> bool:
    """Half-open membership: [valid_from, valid_to).

    The whole temporal model reduces to this. valid_from is inside, valid_to is outside, and an
    interval whose end is not after its start covers nothing at all -- which is why an invalid
    interval silently selects no rows instead of raising.
    """
    return valid_from <= on < valid_to


def sql_covers(from_col: str, to_col: str, on_col: str) -> str:
    """The same rule as SQL, so dbt and Python cannot disagree about the boundary."""
    return f"({on_col} >= {from_col} AND {on_col} < {to_col})"


# --- config loading ------------------------------------------------------------------------


def _semantic_subset(raw: dict) -> dict:
    """Everything that changes generated output, and nothing that does not."""
    return {
        "seed": raw["seed"],
        "rights_version": raw["rights_version"],
        "rule_version_id": raw["rule_version_id"],
        "universe": {k: raw["universe"][k] for k in ("match_run_id", "expected_recordings")},
        "identifiers": dict(sorted(raw["identifiers"].items())),
        "holders": {k: v for k, v in sorted(raw["holders"].items())},
        "ownership": {k: v for k, v in sorted(raw["ownership"].items())},
        "defects": dict(sorted(raw["defects"].items())),
        "share_sum_tolerance": raw["share_sum_tolerance"],
        "rate_card": {
            "model_scope": raw["rate_card"]["model_scope"],
            "currency": raw["rate_card"]["currency"],
            "intervals": raw["rate_card"]["intervals"],
            "expected_gap_days": raw["rate_card"]["expected_gap_days"],
        },
    }


def compute_digest(raw: dict) -> str:
    payload = json.dumps(_semantic_subset(raw), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_rights_model(
    path: str | pathlib.Path | None = None, require_digest: bool = True
) -> RightsModel:
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p) as fh:
        raw = yaml.safe_load(fh)

    digest = compute_digest(raw)
    recorded = str(raw.get("rules_sha256", ""))
    if require_digest and recorded != digest:
        raise ValueError(
            f"config/rights_model.yml content digest is {digest} but the file records "
            f"{recorded!r}. A seed, count, interval or defect quota changed without the digest "
            f"being updated: refusing to generate rights under a version that no longer "
            f"describes the model."
        )

    h, own, rc = raw["holders"], raw["ownership"], raw["rate_card"]
    if h["reserved_without_split"] >= h["count"]:
        raise ValueError("reserved_without_split cannot consume every holder")
    model = RightsModel(
        seed=raw["seed"],
        rights_version=str(raw["rights_version"]),
        rule_version_id=raw["rule_version_id"],
        universe_match_run_id=raw["universe"]["match_run_id"],
        expected_recordings=int(raw["universe"]["expected_recordings"]),
        holder_id_prefix=raw["identifiers"]["holder_id_prefix"],
        holder_id_digits=int(raw["identifiers"]["holder_id_digits"]),
        split_version_id_prefix=raw["identifiers"]["split_version_id_prefix"],
        digest_chars=int(raw["identifiers"]["digest_chars"]),
        holder_count=int(h["count"]),
        assignment_power=float(h["assignment_power"]),
        holder_types=tuple(h["types"]),
        holder_type_weights=tuple(float(w) for w in h["type_weights"]),
        payee_statuses=tuple(h["payee_statuses"]),
        payee_status_weights=tuple(float(w) for w in h["payee_status_weights"]),
        reserved_without_split=int(h["reserved_without_split"]),
        holders_per_recording=tuple(int(n) for n in own["holders_per_recording"]),
        holders_per_recording_weights=tuple(float(w) for w in own["holders_per_recording_weights"]),
        share_decimal_places=int(own["share_decimal_places"]),
        minimum_share_pct=Decimal(str(own["minimum_share_pct"])),
        base_valid_from=dt.date.fromisoformat(str(own["base_valid_from"])),
        open_ended_valid_to=dt.date.fromisoformat(str(own["open_ended_valid_to"])),
        change_date=dt.date.fromisoformat(str(own["mid_period_change"]["change_date"])),
        change_share_of_recordings=float(own["mid_period_change"]["share_of_recordings"]),
        defect_counts={k: int(v) for k, v in raw["defects"].items() if isinstance(v, int)},
        overlap_start=dt.date.fromisoformat(str(raw["defects"]["overlap_start"])),
        overlap_end=dt.date.fromisoformat(str(raw["defects"]["overlap_end"])),
        gap_start=dt.date.fromisoformat(str(raw["defects"]["gap_start"])),
        gap_end=dt.date.fromisoformat(str(raw["defects"]["gap_end"])),
        invalid_from=dt.date.fromisoformat(str(raw["defects"]["invalid_from"])),
        invalid_to=dt.date.fromisoformat(str(raw["defects"]["invalid_to"])),
        share_sum_tolerance=Decimal(str(raw["share_sum_tolerance"])),
        rate_model_scope=rc["model_scope"],
        rate_currency=rc["currency"],
        rate_intervals=tuple(rc["intervals"]),
        expected_rate_gap_days=int(rc["expected_gap_days"]),
        rules_digest=digest,
    )
    missing = set(DEFECT_ORDER) - set(model.defect_counts)
    if missing:
        raise ValueError(f"config is missing defect quotas: {sorted(missing)}")
    if len(rate_gap_days(model)) != model.expected_rate_gap_days:
        raise ValueError(
            f"rate card intervals leave {len(rate_gap_days(model))} uncovered days but the "
            f"config expects {model.expected_rate_gap_days}"
        )
    return model
