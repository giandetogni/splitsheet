"""Largest-remainder distribution of a published amount across holders. Pure Decimal, no floats.

THE PROBLEM THIS SOLVES. A gross amount of $1.00 split 1/3, 1/3, 1/3 gives three unrounded shares
of 0.3333... Rounding each one independently to cents gives 0.33 + 0.33 + 0.33 = 0.99, and the
published total no longer equals the sum of its parts. One cent has evaporated. Do that across
millions of recordings and the financial statement does not add up -- which, in a royalty pipeline,
is the difference between an auditable report and a plausible one.

THE POLICY, and it is one policy rather than a per-holder decision:

  1. ONE rounding point. The gross is rounded to cents once, at publication.
  2. Every holder's share is floored to cents.
  3. The leftover cents -- published gross minus the sum of the floors -- are handed out one each,
     to the holders with the largest discarded fraction.
  4. Ties in that fraction are broken by `rights_holder_id` ascending. Deterministic, and
     independent of row order, join order and engine.

Step 4 is the part that matters for reproducibility: without a total order, two runs over the same
data can hand the same cent to different holders, and the publication stops being idempotent.

WHY DECIMAL. `0.1 + 0.2 != 0.3` is not an acceptable property for money. Everything here is
Decimal, and the only place a value becomes an integer is the cent count.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class HolderAllocation:
    rights_holder_id: str
    share_pct: Decimal
    unrounded: Decimal          # full internal precision, before any rounding
    floor_cents: int            # unrounded floored to whole cents
    remainder_fraction: Decimal  # the part discarded by the floor, in cents
    got_remainder_cent: bool     # whether this holder received one of the leftover cents
    published: Decimal           # final published amount, exactly 2 decimals


def publish_gross(gross_unrounded: Decimal) -> Decimal:
    """The single rounding point. Half-up, because bankers' rounding on a published statement
    surprises humans reconciling it by hand."""
    return gross_unrounded.quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(gross_unrounded: Decimal,
             holders: list[tuple[str, Decimal]]) -> list[HolderAllocation]:
    """Distribute the published gross across holders by largest remainder.

    `holders` is [(rights_holder_id, share_pct)], shares summing to exactly 100 for a valid set.
    Returns one allocation per holder; the published amounts sum EXACTLY to publish_gross(gross).
    """
    if not holders:
        return []
    if gross_unrounded < 0:
        raise ValueError(f"negative gross royalty: {gross_unrounded}")

    published_total = publish_gross(gross_unrounded)
    target_cents = int((published_total / CENT).to_integral_value())

    rows = []
    for holder_id, share in holders:
        if share < 0:
            raise ValueError(f"negative share for {holder_id}: {share}")
        unrounded = gross_unrounded * share / HUNDRED
        in_cents = unrounded / CENT
        floor_cents = int(in_cents.to_integral_value(rounding=ROUND_FLOOR))
        rows.append({
            "holder_id": holder_id,
            "share": share,
            "unrounded": unrounded,
            "floor_cents": floor_cents,
            "fraction": in_cents - Decimal(floor_cents),
        })

    leftover = target_cents - sum(r["floor_cents"] for r in rows)
    if leftover < 0:
        # Cannot happen: each floor is <= its exact value and the shares sum to 100, so the sum of
        # floors never exceeds the exact gross in cents, which never exceeds the rounded gross.
        raise AssertionError(f"floors exceed the published gross by {-leftover} cents")
    if leftover > len(rows):
        raise AssertionError(f"{leftover} leftover cents for {len(rows)} holders")

    # THE DETERMINISTIC ORDER: biggest discarded fraction first, holder id as the tiebreak.
    order = sorted(range(len(rows)),
                   key=lambda i: (-rows[i]["fraction"], rows[i]["holder_id"]))
    winners = set(order[:leftover])

    out = []
    for i, r in enumerate(rows):
        cents = r["floor_cents"] + (1 if i in winners else 0)
        out.append(HolderAllocation(
            rights_holder_id=r["holder_id"],
            share_pct=r["share"],
            unrounded=r["unrounded"],
            floor_cents=r["floor_cents"],
            remainder_fraction=r["fraction"],
            got_remainder_cent=i in winners,
            published=(Decimal(cents) * CENT).quantize(CENT),
        ))
    # The invariant, asserted here rather than hoped for downstream.
    if sum(a.published for a in out) != published_total:
        raise AssertionError(
            f"allocation does not close: {sum(a.published for a in out)} != {published_total}")
    return out


# --- the same rules as SQL ------------------------------------------------------------------
#
# Generated from the constants above so the warehouse cannot drift from the Python the unit tests
# pin. An integration test recomputes published rows in Python and asserts they agree.

def sql_published_gross(gross_expr: str) -> str:
    """ROUND(x, 2) in BigQuery is half-away-from-zero, which matches ROUND_HALF_UP for the
    non-negative amounts this pipeline produces. Negative gross is refused upstream."""
    return f"ROUND({gross_expr}, 2)"


def sql_floor_cents(unrounded_expr: str) -> str:
    return f"CAST(FLOOR({unrounded_expr} / NUMERIC '0.01') AS INT64)"


def sql_remainder_fraction(unrounded_expr: str) -> str:
    return (f"({unrounded_expr} / NUMERIC '0.01' "
            f"- FLOOR({unrounded_expr} / NUMERIC '0.01'))")


def sql_remainder_rank(partition_by: str, fraction_col: str = "remainder_fraction",
                       holder_col: str = "rights_holder_id") -> str:
    """The deterministic order: largest discarded fraction first, holder id as the tiebreak.

    ROW_NUMBER appears here, and it is not an arbitrary pick: it is ordering by a measured
    quantity with a total tiebreak, which is the definition of the policy. Contrast with the
    matcher and the ownership join, where ROW_NUMBER() = 1 would have chosen a winner among
    candidates that no measurement separated.
    """
    return (f"ROW_NUMBER() OVER (PARTITION BY {partition_by} "
            f"ORDER BY {fraction_col} DESC, {holder_col} ASC)")
